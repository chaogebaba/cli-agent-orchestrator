"""RESUME HOT-FIX slice — the deployed resume verb (issue: F829 precursor).

This module is the ONE place the ``assign(resume_from=…)`` verb resolves what
to resume, independent of the fork-base registry. The full F829 redesign
(``conversation_identity`` root, capability matrix, unified locator) is
separate; this slice removes only the four refusals that made resume unusable
on the deployed base (evidence pack §1 items 6-9, §5):

1. resume no longer needs a ``provider_sessions`` (fork-base) row — it resolves
   from the F631 ``terminal_identity`` row, which survives reap.
2. kiro is resumable via a ``supports_resume`` capability (kiro can resume via
   ``--resume-id`` even though it cannot fork).
3. reap captures kiro's session id from the on-disk session store and keeps the
   worktree directory when the branch has unmerged commits.
4. every resume precondition failure is ONE typed ``resume_refused`` answer
   naming the single missing fact and how to supply it — not four opaque
   fork-path strings.

Kept deliberately separate from ``fork_context_service`` so the F829 build
(which rewrites that module) does not collide with this hot-fix structurally.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# The closed set of missing-fact tokens the ONE resume refusal can name
# (brief deliverable 4). Every resume precondition failure maps to exactly one.
RESUME_MISSING_TOKENS = frozenset(
    {"identity", "session_id", "artifact", "cwd", "provider_capability", "profile"}
)


class ResumeRefused(Exception):
    """The single typed refusal on the resume path (brief deliverable 4).

    Carries the closed-vocabulary ``missing`` token and a ``how`` string: the
    exact command or fact that fixes it. Serialized by the assign handler as
    ``{"error": "resume_refused", "missing": <token>, "how": <str>}`` — the ONE
    shape that replaces base_not_registered / base_name_unknown /
    anchor_not_forkable / provider_lacks_fork_capability / resume_profile_mismatch
    on the resume path. The fork path keeps those strings unchanged.
    """

    def __init__(self, missing: str, how: str):
        if missing not in RESUME_MISSING_TOKENS:
            raise ValueError(f"unknown resume-missing token: {missing!r}")
        self.missing = missing
        self.how = how
        super().__init__(f"resume_refused:{missing}")

    def as_dict(self) -> dict[str, str]:
        return {"error": "resume_refused", "missing": self.missing, "how": self.how}


# --------------------------------------------------------------------------
# Provider resume capability (brief deliverable 2)
# --------------------------------------------------------------------------
def provider_supports_resume(provider: str) -> bool:
    """Whether ``provider`` can RESUME a prior session (distinct from FORK).

    Capability query, not a class flag on the fork axis: the default is the
    provider's ``supports_resume`` attribute, which itself defaults to
    ``supports_fork_context`` (base.py) so codex/grok keep resuming as today.
    kiro overrides ``supports_resume = True`` while ``supports_fork_context``
    stays False — it can re-attach an existing session via ``--resume-id`` but
    cannot fork one. An unknown provider is not resumable.
    """
    from cli_agent_orchestrator.providers.manager import get_provider_class

    try:
        cls = get_provider_class(provider)
    except ValueError:
        return False
    supports = getattr(cls, "supports_resume", None)
    if supports is None:
        # Provider predates the capability; fall back to the fork axis.
        return bool(getattr(cls, "supports_fork_context", False))
    return bool(supports)


# --------------------------------------------------------------------------
# Reap-time kiro session-id capture from the on-disk store (brief deliverable 3a)
# --------------------------------------------------------------------------
def _kiro_sessions_root() -> Path:
    """Root of the kiro on-disk session store.

    kiro-cli keys sessions by a hash of the workspace path under
    ``~/.kiro/sessions/<sha256(cwd)[:16]>/sess_<uuid>/session.json``. Honors
    ``KIRO_HOME`` when set (test/isolation), else ``~/.kiro``.
    """
    home = os.environ.get("KIRO_HOME")
    base = Path(home) if home else Path.home() / ".kiro"
    return base / "sessions"


def _cwd_hash(cwd: str) -> str:
    """kiro's session-directory key for a workspace path: sha256(path)[:16].

    Empirically verified against a live store (kiro-cli 2.20.x): the 16-hex
    subdirectory name equals ``hashlib.sha256(path.encode()).hexdigest()[:16]``.
    """
    return hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]


def _parse_kiro_ts(value: Any) -> Optional[float]:
    """Parse a kiro ISO8601 timestamp (``…Z``) to a POSIX epoch, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def capture_kiro_session_id_from_store(
    cwd: str,
    launch_epoch: float,
    *,
    sessions_root: Optional[Path] = None,
) -> Optional[str]:
    """Resolve a reaped kiro terminal's session id from the on-disk store.

    Unlike ``fork_context_service.capture_kiro_uuid`` (which shells out to the
    kiro CLI's list-sessions surface), this reads ONLY the filesystem — the
    account may be dead at reap time (the exact incident in the evidence pack),
    so nothing here may depend on the live provider process.

    Rule (brief §3a): under ``~/.kiro/sessions/<sha256(cwd)[:16]>/`` find each
    ``sess_*/session.json`` whose recorded workspace path equals ``cwd``, keep
    only those CREATED AFTER ``launch_epoch`` (so an unrelated older session in
    a reused cwd is never captured), and return the id ONLY when EXACTLY ONE
    such candidate exists (newest by mtime is used to break nothing — the
    exactly-one rule is the safety gate). Any ambiguity → None (the caller then
    records ``resume_hint`` rather than a wrong id).

    The workspace path lives in ``rootPaths``/``workspacePaths`` (NOT a ``cwd``
    key — verified against the live store); the resume id is the ``id`` field
    verbatim (``sess_<uuid>``), which is exactly the ``--resume-id`` value.
    """
    root = sessions_root if sessions_root is not None else _kiro_sessions_root()
    hash_dir = root / _cwd_hash(cwd)
    if not hash_dir.is_dir():
        return None
    target = os.path.realpath(cwd)
    candidates: list[tuple[float, str]] = []
    for sess_dir in hash_dir.iterdir():
        if not sess_dir.is_dir() or not sess_dir.name.startswith("sess_"):
            continue
        meta_path = sess_dir / "session.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not _session_matches_cwd(meta, target):
            continue
        created = _parse_kiro_ts(meta.get("createdAt"))
        if created is None:
            # Fall back to the file's own mtime when kiro omitted the field.
            try:
                created = meta_path.stat().st_mtime
            except OSError:
                continue
        if created < launch_epoch:
            continue
        session_id = meta.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        try:
            mtime = meta_path.stat().st_mtime
        except OSError:
            mtime = created
        candidates.append((mtime, session_id))
    if len(candidates) != 1:
        # Zero (no session persisted for this cwd yet) or ambiguous (more than
        # one post-launch session) — both refuse to guess.
        return None
    return candidates[0][1]


def _session_matches_cwd(meta: dict[str, Any], target_realpath: str) -> bool:
    """True iff a kiro session.json names ``target_realpath`` as its workspace.

    Checks ``rootPaths`` and ``workspacePaths`` (the fields that actually hold
    the workspace directory) plus a legacy ``cwd`` key for forward safety.
    """
    for key in ("rootPaths", "workspacePaths"):
        value = meta.get(key)
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and os.path.realpath(entry) == target_realpath:
                    return True
    legacy = meta.get("cwd")
    if isinstance(legacy, str) and os.path.realpath(legacy) == target_realpath:
        return True
    return False


def kiro_capture_hint(cwd: str, *, sessions_root: Optional[Path] = None) -> str:
    """Human-facing reason a kiro reap could not resolve a resume_key.

    Distinguishes "no session dir for this cwd" from "ambiguous / none matched"
    so the operator knows whether the worker ever persisted a turn.
    """
    root = sessions_root if sessions_root is not None else _kiro_sessions_root()
    hash_dir = root / _cwd_hash(cwd)
    if not hash_dir.is_dir():
        return (
            f"no kiro session store for cwd {cwd} "
            f"(expected {hash_dir}); the worker persisted no conversation turn"
        )
    return (
        f"kiro session id unresolved for cwd {cwd}: zero or ambiguous "
        f"post-launch sessions under {hash_dir}"
    )


# --------------------------------------------------------------------------
# The resume-target resolver (brief deliverable 1)
# --------------------------------------------------------------------------
def resolve_resume_target(value: str) -> dict[str, Any]:
    """Resolve ``assign(resume_from=<value>)`` to the facts a resume needs.

    NO fork-base (``provider_sessions``) row is required and NO base name is
    involved (evidence §1.6-8, §5). Resolution order (brief deliverable 1):

    (a) a live OR reaped terminal id → its ``terminal_identity`` row (F631; it
        survives reap) → provider, cwd, agent_profile, provider_session_id,
        worktree_path, session_name.
    (b) a bare uuid → search ``terminal_identity.provider_session_id``, then
        fall back to the fork-base ``provider_sessions.session_uuid`` registry.

    Returns a row-like dict with the keys the assign resume path consumes:
    ``name`` (the durable handle — the old terminal id or uuid),
    ``session_uuid`` (the resume key), ``provider``, ``cwd``,
    ``agent_profile``, ``worktree_path``, ``source_terminal_id``.

    Raises ``ResumeRefused`` — never a fork-path string:
    * unknown value → missing="identity"
    * identity found but no captured session id → missing="session_id"
    """
    from cli_agent_orchestrator.clients.database import (
        get_provider_session_by_uuid,
        get_terminal_identity,
        get_terminal_identity_by_provider_session_id,
    )

    # (a) terminal id — live or reaped identity row.
    identity = get_terminal_identity(value)
    if identity is not None:
        return _row_from_identity(identity, handle=value)

    # (b) bare uuid: first the identity registry (survives reap), then the
    # fork-base registry (a codex resume_key that was hand-registered, §5).
    identity = get_terminal_identity_by_provider_session_id(value)
    if identity is not None:
        return _row_from_identity(identity, handle=identity.get("terminal_id") or value)

    base_row = get_provider_session_by_uuid(value)
    if base_row is not None:
        return {
            "name": base_row.get("name") or value,
            "session_uuid": base_row.get("session_uuid") or value,
            "provider": base_row.get("provider"),
            "cwd": base_row.get("cwd"),
            "agent_profile": base_row.get("agent_profile"),
            "worktree_path": None,
            "source_terminal_id": base_row.get("source_terminal_id"),
        }

    raise ResumeRefused(
        missing="identity",
        how=(
            f"no live or reaped terminal, and no provider session, matches "
            f"{value!r}; pass a terminal id you remember (dead or alive) or a "
            f"provider session uuid returned as a reap resume_key"
        ),
    )


def _row_from_identity(identity: dict[str, Any], *, handle: str) -> dict[str, Any]:
    """Shape a ``terminal_identity`` row into the assign resume path's row dict.

    Refuses with missing="session_id" when the row has no captured provider
    session id — resume cannot re-attach a conversation whose id was never
    recorded (for kiro this is what the reap-time capture, deliverable 3, fills
    in; if it is still NULL the worker never persisted a turn).
    """
    session_uuid = identity.get("provider_session_id")
    terminal_id = identity.get("terminal_id") or handle
    if not session_uuid:
        raise ResumeRefused(
            missing="session_id",
            how=(
                f"terminal {terminal_id!r} has no captured provider session id; "
                f"it was reaped before persisting a resumable session (for kiro, "
                f"before any conversation turn was written to disk)"
            ),
        )
    return {
        "name": terminal_id,
        "session_uuid": session_uuid,
        "provider": identity.get("provider"),
        "cwd": identity.get("cwd"),
        "agent_profile": identity.get("agent_profile"),
        "worktree_path": identity.get("worktree_path"),
        "source_terminal_id": terminal_id,
    }


# --------------------------------------------------------------------------
# assign(resume_from=…) orchestration (brief deliverable 1 + 2 + 4)
# --------------------------------------------------------------------------
def _ensure_resume_cwd(row_cwd: Optional[str], worktree_path: Optional[str], handle: str) -> str:
    """Resolve (and, if necessary, re-create) the cwd a resume must run in.

    cwd defaults to the identity row's cwd. When that directory is gone
    (evidence §1.5 — reap deleted a kiro worktree keyed by cwd hash), re-create
    it as a git worktree on the branch recorded for it (``cao/<old-terminal-id>``
    convention) when that branch exists; otherwise refuse with missing="cwd".

    Returns the live cwd on success. Raises ``ResumeRefused(missing="cwd")``.
    """
    cwd = worktree_path or row_cwd
    if not cwd:
        raise ResumeRefused(
            missing="cwd",
            how=(
                f"the reaped identity for {handle!r} recorded no working "
                f"directory; pass working_directory= explicitly"
            ),
        )
    if os.path.isdir(cwd):
        return cwd
    # The directory is gone. Try to re-create it as a worktree on the branch
    # the reaped terminal owned (cao/<old-terminal-id>), so a kiro session keyed
    # by this exact path resolves again.
    recreated = _recreate_worktree(cwd, handle)
    if recreated is not None:
        return recreated
    raise ResumeRefused(
        missing="cwd",
        how=(
            f"working directory {cwd!r} for {handle!r} no longer exists and no "
            f"branch cao/{handle} was found to re-create it; re-create the "
            f"checkout at that exact path (kiro keys the session by it) or pass "
            f"working_directory="
        ),
    )


def _recreate_worktree(cwd: str, handle: str) -> Optional[str]:
    """Best-effort: re-create the worktree at ``cwd`` on branch ``cao/<handle>``.

    Returns the path on success, else None (no repo, no such branch, or the
    add failed). The path is preserved exactly because kiro's session store is
    keyed by ``sha256(cwd)[:16]`` — a different path is a different session.
    """
    import subprocess

    branch = f"cao/{handle}"
    # Find a repo to anchor the worktree add: the parent chain of cwd, or the
    # repo the branch lives in as seen from any existing checkout is not known
    # here, so anchor on the nearest existing ancestor directory that is a repo.
    anchor = _nearest_repo_ancestor(cwd)
    if anchor is None:
        return None
    # Does the branch exist?
    show = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=anchor,
        capture_output=True,
        text=True,
    )
    if show.returncode != 0:
        return None
    try:
        os.makedirs(os.path.dirname(cwd), exist_ok=True)
    except OSError:
        return None
    add = subprocess.run(
        ["git", "worktree", "add", cwd, branch],
        cwd=anchor,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        logger.warning(
            "resume: could not re-create worktree %s on %s: %s",
            cwd,
            branch,
            add.stderr.strip(),
        )
        return None
    return cwd


def _nearest_repo_ancestor(path: str) -> Optional[str]:
    """The nearest existing ancestor of ``path`` that is inside a git repo."""
    import subprocess

    current = path
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if os.path.isdir(parent):
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=parent,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        current = parent


def prepare_resume(
    *,
    resume_from: str,
    requested_agent_profile: Optional[str],
    requested_working_directory: Optional[str],
    inherit_pins: bool,
) -> dict[str, Any]:
    """Resolve everything an ``assign(resume_from=…)`` needs, or raise ResumeRefused.

    Returns a dict:
      * ``fork_context`` — a ForkContext(mode="resume") to hand to the create path
      * ``provider`` — the resolved provider
      * ``agent_profile`` — requested override, else the identity's
      * ``working_directory`` — the live (possibly re-created) cwd
      * ``forked_from_info`` — {name, cwd, resumed_from} surfaced to the operator
      * ``authority_files`` — inherited frozen pins when inherit_pins, else None

    Every precondition failure raises ``ResumeRefused`` (deliverable 4): the ONE
    typed answer, never a fork-path string.
    """
    from cli_agent_orchestrator.clients.database import get_frozen_pins
    from cli_agent_orchestrator.models.terminal import ForkContext

    row = resolve_resume_target(resume_from)
    provider = row.get("provider")
    if not provider:
        raise ResumeRefused(
            missing="identity",
            how=(
                f"the identity for {resume_from!r} recorded no provider; it is "
                f"too old to resume — re-dispatch cold"
            ),
        )
    # Capability: kiro resumes (supports_resume True) though it cannot fork.
    if not provider_supports_resume(provider):
        raise ResumeRefused(
            missing="provider_capability",
            how=(
                f"provider {provider!r} cannot resume a session; re-dispatch the "
                f"worker cold with its task"
            ),
        )
    agent_profile = requested_agent_profile or row.get("agent_profile")
    if not agent_profile:
        raise ResumeRefused(
            missing="profile",
            how=(
                f"no agent_profile recorded for {resume_from!r} and none passed; "
                f"pass agent_profile= for the resumed worker"
            ),
        )
    handle = row.get("source_terminal_id") or row.get("name") or resume_from
    working_directory = requested_working_directory or _ensure_resume_cwd(
        row.get("cwd"), row.get("worktree_path"), str(handle)
    )
    fork_context = ForkContext(
        mode="resume",
        session_uuid=row["session_uuid"],
        base_name=str(row.get("name") or handle),
        provider=provider,
        initial_preamble=(
            f"[RESUMED] Re-attached to your prior conversation (resumed from "
            f"{handle}). Continue the task where you left off."
        ),
    )
    authority_files: Optional[list[dict[str, str]]] = None
    if inherit_pins and row.get("source_terminal_id"):
        pins = get_frozen_pins(str(row["source_terminal_id"]))
        authority_files = pins or None
    return {
        "fork_context": fork_context,
        "provider": provider,
        "agent_profile": agent_profile,
        "working_directory": working_directory,
        "forked_from_info": {
            "name": str(row.get("name") or handle),
            "cwd": working_directory,
            "resumed_from": str(handle),
            "provider": provider,
        },
        "authority_files": authority_files,
    }
