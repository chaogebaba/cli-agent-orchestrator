"""F620 (#476): laptop shim PATH injection for WORKER terminals.

Incident (2026-08-30): reviewer/dev lanes ran ``pytest``/``mypy``/``uv sync``
directly on the laptop, building hundreds of MB of ``.venv`` under ``/`` and
driving local load to 38 — doctrine puts those on a grok box via
``scripts/box-run.sh`` but nothing enforced it.

This module composes the PATH prefix that puts ``scripts/laptop-shims`` (the
deny/passthrough wrappers) ahead of the real binaries for a worker terminal —
but ONLY when the offload fleet is actually in use for this repo:

* the terminal is a WORKER (supervisor/operator terminals are never shimmed —
  a human at the supervisor must keep unrestricted local tooling);
* ``scripts/boxes.tsv`` exists AND has at least one ACTIVE box row
  (an all-frozen or absent fleet means there is nowhere to offload to, so the
  laptop is the only option and the shim would be pure obstruction);
* ``LAPTOP_OK`` is unset in the composed environment (explicit operator
  override in the brief).

F636 (#491) — nested-checkout resolution. The shim wrappers
(``scripts/laptop-shims``) ship in the CAO FORK, but ``scripts/boxes.tsv`` (the
live offload-fleet roster) lives only in the ROOT repo that the fork is nested
inside. ``repo_root`` is ``git rev-parse --show-toplevel`` from the worker's
cwd, which stops at the FORK's own ``.git`` — so a single-root lookup found the
shims but never the roster, and the F620 guard was inert for every worker
everywhere. ``boxes.tsv`` is therefore resolved against ``repo_root`` AND its
immediate parent directory (walk up ONE level max, explicit); the shim
directory stays fork-relative (that is where the wrappers physically are). The
non-nested case (both files under one root) is unchanged: the ``repo_root``
lookup matches first.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

# Resolved relative to the repo root the terminal runs in.
_SHIM_SUBDIR = os.path.join("scripts", "laptop-shims")
_BOXES_TSV_SUBDIR = os.path.join("scripts", "boxes.tsv")


def _boxes_tsv_has_active_row(boxes_tsv_path: str) -> bool:
    """True iff ``boxes_tsv_path`` exists and names at least one active box.

    boxes.tsv format (see scripts/box-freeze.sh): tab-separated
    ``host  state  since  reason``; ``state`` is ``active`` or ``frozen``.
    Comment (``#``) and blank lines are ignored. Never raises — an unreadable
    or malformed file resolves to False (no shim), matching the "absent → no
    shim" rule.
    """
    try:
        with open(boxes_tsv_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                fields = stripped.split("\t")
                if len(fields) >= 2 and fields[1].strip() == "active":
                    return True
    except OSError:
        return False
    return False


def _active_boxes_tsv_for(repo_root: str) -> Optional[str]:
    """Path to an active ``scripts/boxes.tsv`` for ``repo_root``, else ``None``.

    F636 (#491): checks ``<repo_root>/scripts/boxes.tsv`` first, then — because
    the roster lives only in the ROOT repo the CAO fork is nested inside, while
    ``repo_root`` resolves to the fork's own toplevel — the SAME relative path
    under ``repo_root``'s immediate parent directory. The walk-up is exactly ONE
    level (a directly-nested fork is a child of its root); it never ascends
    further, so an unrelated grandparent repo can never satisfy the guard.

    Returns the first path whose ``boxes.tsv`` has an active row; ``None`` if
    neither does. ``os.path.dirname`` on a normalized root yields the parent;
    at a filesystem root ``dirname`` is idempotent (``"/"`` -> ``"/"``), so the
    parent check simply re-probes the same nonexistent path and still returns
    ``None`` — no infinite ascent, no special-casing.
    """
    candidates = [repo_root]
    parent = os.path.dirname(os.path.normpath(repo_root))
    if parent and parent != os.path.normpath(repo_root):
        candidates.append(parent)
    for root in candidates:
        boxes_tsv = os.path.join(root, _BOXES_TSV_SUBDIR)
        if _boxes_tsv_has_active_row(boxes_tsv):
            return boxes_tsv
    return None


def should_inject_shim(
    *,
    is_worker: bool,
    repo_root: Optional[str],
    env: Mapping[str, str],
    is_box_hosted: bool = False,
) -> bool:
    """Decide whether the laptop-shim PATH prefix applies for this terminal.

    All four conditions must hold: worker terminal, a resolvable repo root, an
    active box row in ``scripts/boxes.tsv`` (resolved against ``repo_root`` or
    its immediate parent — see ``_active_boxes_tsv_for``, F636 #491), and
    ``LAPTOP_OK`` unset in ``env``.

    F634 (D16) — ``is_box_hosted`` makes the predicate HOST-AWARE. The four
    conditions above key on repo CONTENTS, never on where the lane runs, and
    ``box-setup.sh`` rsyncs the root repo (active-row ``boxes.tsv``) together
    with the nested fork (``scripts/laptop-shims``) onto every box. So a
    box-hosted worker satisfies every one of them and would be hard-denied
    ``pytest``/``mypy``/``uv`` (exit 97) on the very machine the work was
    relocated to. A box lane is therefore never shimmed; the laptop's own
    workers keep the F620 guard unchanged. The default is False, so every
    existing caller composes exactly as before.
    """
    if is_box_hosted:
        return False
    if not is_worker:
        return False
    if not repo_root:
        return False
    if env.get("LAPTOP_OK", "") != "":
        return False
    if _active_boxes_tsv_for(repo_root) is None:
        return False
    shim_dir = os.path.join(repo_root, _SHIM_SUBDIR)
    if not os.path.isdir(shim_dir):
        return False
    return True


def shim_dir_for(repo_root: str) -> str:
    """Absolute path to the laptop-shims directory under ``repo_root``."""
    return os.path.join(repo_root, _SHIM_SUBDIR)


def compose_shim_path(shim_dir: str, base_path: Optional[str]) -> str:
    """Prepend ``shim_dir`` to ``base_path`` (idempotent).

    ``base_path`` is the PATH the worker would otherwise inherit (the server's
    own ``PATH`` at compose time). If ``shim_dir`` is already the leading entry
    the value is returned unchanged so re-composition never stacks duplicates.
    """
    if not base_path:
        return shim_dir
    entries = base_path.split(os.pathsep)
    if entries and entries[0] == shim_dir:
        return base_path
    return shim_dir + os.pathsep + base_path


def maybe_shim_env(
    extra_env: dict[str, str],
    *,
    is_worker: bool,
    repo_root: Optional[str],
    base_path: Optional[str] = None,
    is_box_hosted: bool = False,
) -> dict[str, str]:
    """Return ``extra_env`` with a shim-prefixed ``PATH`` when applicable.

    Mutates and returns ``extra_env`` for caller convenience. ``base_path``
    defaults to the server's own ``PATH`` (what a worker inherits). When the
    shim does not apply, ``extra_env`` is returned unchanged (no ``PATH`` key
    added), so a non-worker/absent-fleet terminal composes exactly as before.

    ``is_box_hosted`` (F634 D16) is forwarded to ``should_inject_shim``; this
    is the wiring call the create path uses, so the flag must be threaded here
    or the predicate never sees it.
    """
    # LAPTOP_OK is read from the composed env first, else the process env — a
    # worker inherits the server env, so an operator export reaches here even
    # when it is not threaded through extra_env explicitly.
    effective_env = dict(os.environ)
    effective_env.update(extra_env)
    if not should_inject_shim(
        is_worker=is_worker,
        repo_root=repo_root,
        env=effective_env,
        is_box_hosted=is_box_hosted,
    ):
        return extra_env
    assert repo_root is not None  # narrowed by should_inject_shim
    shim_dir = shim_dir_for(repo_root)
    resolved_base = base_path if base_path is not None else os.environ.get("PATH", "")
    extra_env["PATH"] = compose_shim_path(shim_dir, resolved_base)
    logger.info(
        "F620: prepended laptop-shim dir %s to worker PATH (active offload fleet)", shim_dir
    )
    return extra_env
