"""Git worktree provisioning for per-terminal isolation (issue #100, Phase 1).

When a supervisor spawns multiple workers via ``handoff``/``assign``, they
share the same git branch and working directory by default -- the exact
"merge conflicts, overwritten files, race conditions" gap issue #100 names.
Passing ``use_worktree=True`` on a spawn gives that one worker an isolated
``git worktree`` checkout on its own branch instead.

Scoped strictly to the maintainer's own suggested Phase 1 (this module +
``use_worktree`` on ``handoff``/``assign``) -- the ``--enable-worktrees``
global launch flag and the ``cao worktrees clean`` CLI command are Phase 2/3,
intentionally not built here to keep this PR reviewable-sized.

No new CAO-side persistence: a worktree's path and branch are both derived
deterministically from the terminal_id CAO already generates for every
terminal (``generate_terminal_id()``, unique and server-controlled, never
user-supplied), so ``create_terminal``/``delete_terminal`` can locate a
worktree at teardown time from the terminal_id alone -- git's own
``.git/worktrees`` bookkeeping is the single source of truth, matching how
this project already treats git as authoritative elsewhere.
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from cli_agent_orchestrator.services.fork_context_service import assert_cwd_live

logger = logging.getLogger(__name__)

# Kept out of the repo's own working tree root and namespaced under one
# directory so a single `git worktree list`/`rm -rf` scopes cleanly to
# everything CAO has ever provisioned here.
WORKTREE_SUBDIR = ".cao/worktrees"
BRANCH_PREFIX = "cao/"

# F620 (#476): where lane checkouts (and any venv they build) physically land.
# The incident: reviewer/dev lanes provisioned worktrees under the repo on `/`,
# built .venvs inside them, and filled `/`. Doctrine puts scratch on
# `/data/cao-scratch/`; this makes the worktree root honour that by default.
#
# Precedence (highest first):
#   1. env  CAO_WORKTREE_ROOT
#   2. providers.toml  [worktrees] root
#   3. default  /data/cao-scratch/worktrees/<repo-basename>  -- only when
#      /data/cao-scratch exists AND is writable
#   4. in-repo fallback  <repo_root>/.cao/worktrees  -- today's behaviour,
#      used whenever /data/cao-scratch is absent/unwritable and nothing is
#      configured.
#
# git's own `.git/worktrees` bookkeeping remains the single source of truth for
# list/cleanup/teardown regardless of where the checkout physically lives, so
# moving the root off-repo does not change how a worktree is found at teardown
# (teardown resolves repo_root from CAO's stored worktree_info, then runs
# `git worktree remove` from that real repo root -- see terminal_service).
_DATA_SCRATCH_WORKTREES = "/data/cao-scratch/worktrees"

# Local-only git operations (add/remove/list); generous but bounded so a
# hung git process cannot hang terminal creation/deletion indefinitely.
_GIT_TIMEOUT_SECONDS = 30

_WORKTREE_PATH_MARKER = f"{os.sep}{WORKTREE_SUBDIR}{os.sep}"


class WorktreeError(Exception):
    """A git-worktree operation failed (repo resolution, add, or list)."""


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd``, never raising -- a nonexistent ``cwd``
    (``OSError``/``FileNotFoundError``) or a hung git process
    (``subprocess.TimeoutExpired``) is reported the SAME way a nonzero exit
    code is: a synthetic failed ``CompletedProcess`` with the exception text
    in ``stderr``. Every caller below already branches on ``returncode != 0``
    for a normal git failure -- routing infra failures through that same
    path (instead of letting them escape as a raw, uncaught exception) is
    what makes ``remove_worktree``'s "never raises" contract, and
    ``find_repo_root``/``create_worktree``/``list_worktrees``'s own
    ``WorktreeError`` contract, both actually hold rather than being
    docstring claims that a missing/unreadable directory quietly breaks."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1, stdout="", stderr=str(e)
        )


def find_repo_root(start_path: str) -> str:
    """The git repository root containing ``start_path``.

    ``git worktree add`` must run from inside a real repo's own working
    tree; ``start_path`` may be any subdirectory of it (a supervisor's own
    working directory is not necessarily the repo root).

    Raises:
        WorktreeError: ``start_path`` is not inside a git repository.
    """
    assert_cwd_live(start_path, error_factory=WorktreeError)
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=start_path)
    if result.returncode != 0:
        raise WorktreeError(
            f"{start_path!r} is not inside a git repository -- use_worktree requires "
            f"a real git repo ('git rev-parse --show-toplevel' failed: {result.stderr.strip()})"
        )
    stdout: str = result.stdout
    return stdout.strip()


def _providers_toml_worktree_root() -> Optional[str]:
    """Read ``[worktrees] root`` from providers.toml, or ``None``.

    Lazy import + broad tolerance: a missing file, invalid TOML, a missing
    ``[worktrees]`` table, or a non-string/empty ``root`` all resolve to
    ``None`` (fall through to the next precedence tier). Never raises -- root
    resolution runs on the terminal-creation hot path and must not fail a
    spawn over a malformed config file.
    """
    try:
        import tomllib

        from cli_agent_orchestrator.services.settings_service import (
            PROVIDER_DEFAULTS_FILE,
        )

        if not PROVIDER_DEFAULTS_FILE.exists():
            return None
        data = tomllib.loads(PROVIDER_DEFAULTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    section = data.get("worktrees")
    if not isinstance(section, dict):
        return None
    root = section.get("root")
    if isinstance(root, str) and root.strip():
        return root.strip()
    return None


def _data_scratch_writable() -> bool:
    """True iff ``/data/cao-scratch`` exists as a directory and is writable.

    The default off-repo tier is only chosen when this holds; otherwise root
    resolution falls back to the in-repo ``.cao/worktrees`` (today's
    behaviour) so a machine without a ``/data`` mount is unaffected.
    """
    base = os.path.dirname(_DATA_SCRATCH_WORKTREES)  # /data/cao-scratch
    return os.path.isdir(base) and os.access(base, os.W_OK)


def resolve_worktree_root(repo_root: str) -> tuple[str, bool]:
    """Resolve the directory under which this repo's worktrees are provisioned.

    Returns ``(root, in_repo)`` where ``root`` is the parent directory a new
    worktree checkout is created under and ``in_repo`` is True only for the
    in-repo ``<repo_root>/.cao/worktrees`` fallback (the sole case that also
    writes a ``.gitignore``). Precedence, highest first:

    1. env ``CAO_WORKTREE_ROOT``
    2. providers.toml ``[worktrees] root``
    3. ``/data/cao-scratch/worktrees/<repo-basename>`` when ``/data/cao-scratch``
       exists and is writable
    4. ``<repo_root>/.cao/worktrees`` (in-repo fallback)

    The configured tiers (1, 2) are used verbatim, not per-repo namespaced:
    an operator who points ``CAO_WORKTREE_ROOT`` somewhere has chosen that
    directory. The default tier (3) namespaces by ``<repo-basename>`` so
    multiple repos sharing ``/data/cao-scratch`` do not collide.
    """
    env_root = os.environ.get("CAO_WORKTREE_ROOT")
    if env_root and env_root.strip():
        return env_root.strip(), False

    toml_root = _providers_toml_worktree_root()
    if toml_root:
        return toml_root, False

    if _data_scratch_writable():
        repo_basename = os.path.basename(os.path.normpath(repo_root)) or "repo"
        return os.path.join(_DATA_SCRATCH_WORKTREES, repo_basename), False

    return os.path.join(repo_root, WORKTREE_SUBDIR), True


def worktree_path_for(repo_root: str, terminal_id: str) -> str:
    """The physical checkout path for ``terminal_id`` under the resolved root.

    Honours F620's configurable root. For the in-repo fallback this is
    ``<repo_root>/.cao/worktrees/<terminal_id>`` (unchanged); for an off-repo
    root it is ``<root>/<terminal_id>``.
    """
    root, _in_repo = resolve_worktree_root(repo_root)
    return os.path.join(root, terminal_id)


def branch_for(terminal_id: str) -> str:
    return f"{BRANCH_PREFIX}{terminal_id}"


def create_worktree(repo_root: str, terminal_id: str) -> str:
    """``git worktree add`` a fresh checkout on its own branch, based on the
    repo's current HEAD. Returns the new worktree's absolute path.

    ``terminal_id`` is server-generated (never user-supplied), so the
    derived path/branch need no additional sanitization beyond what CAO's
    own terminal-id generator already guarantees (a fixed-alphabet,
    fixed-length id -- see ``generate_terminal_id``).

    Raises:
        WorktreeError: ``git worktree add`` failed (e.g. a stale directory
            or branch from an earlier crashed attempt under the same id --
            unreachable in practice since terminal_id is always fresh, but
            surfaced as a clear error rather than a confusing git failure).
    """
    path = worktree_path_for(repo_root, terminal_id)
    branch = branch_for(terminal_id)
    root, in_repo = resolve_worktree_root(repo_root)
    # `git worktree add` creates the leaf checkout directory itself, but will
    # NOT create missing PARENT directories of an off-repo root (e.g. the
    # first worktree under /data/cao-scratch/worktrees/<repo>). Create the
    # root up front; best-effort, git surfaces a clear error if it truly
    # cannot be made. The in-repo case's parent (.cao/) is created the same
    # way for symmetry (git already tolerates it existing).
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as e:
        raise WorktreeError(
            f"could not create worktree root {root!r} for terminal {terminal_id}: {e}"
        ) from e
    result = _run_git(["worktree", "add", "-b", branch, path], cwd=repo_root)
    if result.returncode != 0:
        raise WorktreeError(
            f"'git worktree add' failed for terminal {terminal_id}: {result.stderr.strip()}"
        )
    # Gitignore write is only meaningful (and only correct) for the in-repo
    # fallback: an off-repo root is not tracked by the repo at all, so there is
    # nothing for the main checkout's `git status`/`git add -A` to see.
    if in_repo:
        _ensure_worktree_subdir_gitignored(repo_root)
    return path


def _ensure_worktree_subdir_gitignored(repo_root: str) -> None:
    """Best-effort: write ``<repo_root>/.cao/worktrees/.gitignore`` (``*``) the
    first time a worktree is created there, so the main checkout's own
    ``git status``/``git add -A`` never sees -- and never stages as an
    embedded-repo gitlink -- the worktrees CAO provisions inside it. The
    target repo is arbitrary user code; we cannot assume its own
    ``.gitignore`` already excludes ``.cao/worktrees`` (this repo's own
    ``.gitignore`` only excludes its unrelated ``.worktrees``), so CAO must
    write its own ignore file rather than rely on one being present.

    Never raises: a failure here (e.g. read-only filesystem) must not fail
    the worktree creation that already succeeded.
    """
    gitignore_path = os.path.join(repo_root, WORKTREE_SUBDIR, ".gitignore")
    try:
        if not os.path.exists(gitignore_path):
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write("*\n")
    except OSError as e:
        logger.warning("worktree setup: failed to write %s: %s", gitignore_path, e)


def remove_worktree(repo_root: str, terminal_id: str, worktree_path: Optional[str] = None) -> None:
    """Best-effort teardown: ``git worktree remove --force`` (agents commonly
    leave modified/untracked files behind, so a plain ``remove`` would
    refuse) followed by a SAFE branch delete (``git branch -d``, not
    ``-D``).

    Deliberately never force-deletes the branch: a worker that committed
    its results to ``cao/<terminal_id>`` before completing (exactly what
    agents are usually instructed to do) must not have that history
    destroyed just because Phase 1 has no merge-back story yet. ``-d``
    only succeeds when the branch has no commits that would be lost (i.e.
    it is unchanged, or already merged); a worker that committed real work
    fails the safe delete and the branch is left behind -- a leak for
    Phase 3's ``cao worktrees clean`` to sweep up later, not silent data
    loss. Only the (uncommitted/untracked) working-tree contents of the
    worktree itself are ever force-discarded.

    Never raises -- called from terminal-teardown paths (``delete_terminal``,
    and the failure-cleanup path in ``create_terminal``) that must not fail
    the terminal's own deletion/rollback over a worktree cleanup issue.
    Failures are logged, not swallowed silently.

    ``worktree_path`` (F620 #476): when the caller knows the exact checkout
    path (from CAO's stored ``worktree_info``), pass it so removal targets the
    physical checkout git recorded at creation time -- robust even if the
    configurable root changed between create and teardown. Omitted, the path
    is recomputed from the current root resolution (the in-repo/unchanged
    case).
    """
    path = worktree_path if worktree_path else worktree_path_for(repo_root, terminal_id)
    branch = branch_for(terminal_id)
    result = _run_git(["worktree", "remove", "--force", path], cwd=repo_root)
    if result.returncode != 0:
        logger.warning(
            "worktree cleanup: 'git worktree remove --force %s' failed: %s",
            path,
            result.stderr.strip(),
        )
    result = _run_git(["branch", "-d", branch], cwd=repo_root)
    if result.returncode != 0:
        logger.warning(
            "worktree cleanup: 'git branch -d %s' failed (left in place -- likely has "
            "unmerged commits; merge/push the work, then delete it manually): %s",
            branch,
            result.stderr.strip(),
        )


def list_worktrees(repo_root: str) -> list[dict[str, str | bool]]:
    """Parsed ``git worktree list --porcelain`` for ``repo_root`` -- the AC's
    'list' operation. No CAO-side persistence to query: git's own
    bookkeeping is authoritative, so this always reflects reality even if a
    worktree was added/removed outside CAO.

    Raises:
        WorktreeError: ``repo_root`` is not a git repository, or the list
            command otherwise failed.
    """
    result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        raise WorktreeError(f"'git worktree list' failed: {result.stderr.strip()}")
    worktrees: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for line in result.stdout.splitlines():
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value if value else True
    if current:
        worktrees.append(current)
    return worktrees


def parse_worktree_path(path: object) -> tuple[str, str] | None:
    """If ``path`` looks like a CAO-managed worktree, or a subdirectory of
    one (``<repo_root>/.cao/worktrees/<terminal_id>[/...]``), return
    ``(repo_root, terminal_id)``; otherwise ``None``.

    Used at teardown time to recognize a worktree-provisioned terminal from
    its own live pane working directory alone -- no separate CAO-side
    tracking of "which terminals are worktree-backed" is needed, since the
    path shape itself is the marker.

    Two deliberate choices beyond a naive split:

    - Subdirectories of the worktree are accepted, not just the worktree
      root exactly. tmux reports the pane's CURRENT directory
      (``pane_current_path``): a worker that ``cd``s into a subdirectory
      (very likely) would otherwise no longer be recognized as
      worktree-backed at teardown, leaking the worktree/branch.
    - ``rfind`` (last occurrence), not ``find`` (first), so a
      worktree-backed supervisor spawning a worktree-backed worker --
      nesting ``<repo_root>/.cao/worktrees/A/.cao/worktrees/B`` -- resolves
      to B's own ``(repo_root, terminal_id)`` (repo_root = A's worktree
      root, terminal_id = B) instead of failing to parse and leaking B.

    Accepts ``object`` (not just ``str | None``) and returns ``None`` for
    anything that isn't a real string, deliberately: the caller
    (``delete_terminal``) reads this from a backend call whose real contract
    is ``str | None``, but its actual value at any given call site can be
    something else entirely under test doubles/mocks -- this must degrade to
    "not a worktree" rather than raise, since it feeds a real ``git``
    subprocess call two steps downstream.
    """
    if not isinstance(path, str) or not path:
        return None
    idx = path.rfind(_WORKTREE_PATH_MARKER)
    if idx == -1:
        return None
    repo_root = path[:idx]
    remainder = path[idx + len(_WORKTREE_PATH_MARKER) :]
    terminal_id = remainder.split(os.sep, 1)[0]
    if not repo_root or not terminal_id:
        return None
    return repo_root, terminal_id


# --------------------------------------------------------------------------
# F121: Worktree Branch Integrity — verification at settlement checkpoints
# --------------------------------------------------------------------------

# Total monotonic deadline for the 3-signal verification. Independent of
# the 30-second _GIT_TIMEOUT_SECONDS used for provisioning/teardown.
_VERIFY_DEADLINE_SECONDS = 5.0


@dataclass
class WorktreeIntegrityResult:
    """Result of a worktree branch-integrity check (F121).

    Every field is populated on every path; ``ok=True`` requires ALL three
    signals to match.  ``ok=False`` with ``error`` set means git commands
    failed or timed out (fail-closed).
    """

    ok: bool
    expected_branch: str
    expected_worktree_path: str
    actual_toplevel: Optional[str] = None
    actual_common_dir: Optional[str] = None
    actual_branch: Optional[str] = None
    cwd_escaped: bool = False
    branch_escaped: bool = False
    error: Optional[str] = None


def _run_git_bounded(args: list[str], cwd: str, remaining: float) -> subprocess.CompletedProcess:
    """Run git with at most ``remaining`` seconds budget. Never raises."""
    if remaining <= 0:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1, stdout="", stderr="verification_timeout"
        )
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1, stdout="", stderr="verification_timeout"
        )
    except OSError as e:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1, stdout="", stderr=str(e)
        )


def verify_worktree_integrity(
    live_cwd: str,
    worktree_info: dict[str, str],
) -> WorktreeIntegrityResult:
    """Compare actual git state at live_cwd against CAO-owned expected state.

    Runs three git queries in live_cwd under one 5.0-second monotonic deadline.
    Any mismatch → ok=False. Git failure or deadline exhaustion → ok=False
    with error populated (fail-closed).
    """
    expected_branch = worktree_info["expected_branch"]
    expected_worktree_path = worktree_info["worktree_path"]
    expected_repo_root = worktree_info["repo_root"]

    deadline = time.monotonic() + _VERIFY_DEADLINE_SECONDS

    # Signal 1: --show-toplevel (Class A detection)
    remaining = deadline - time.monotonic()
    result = _run_git_bounded(["rev-parse", "--show-toplevel"], live_cwd, remaining)
    if result.returncode != 0:
        error_text = result.stderr.strip() or "toplevel_query_failed"
        if "verification_timeout" in error_text:
            error_text = "verification_timeout"
        return WorktreeIntegrityResult(
            ok=False,
            expected_branch=expected_branch,
            expected_worktree_path=expected_worktree_path,
            error=error_text,
        )
    actual_toplevel = result.stdout.strip()

    # Signal 2: --git-common-dir (structural sanity)
    remaining = deadline - time.monotonic()
    result = _run_git_bounded(["rev-parse", "--git-common-dir"], live_cwd, remaining)
    if result.returncode != 0:
        error_text = result.stderr.strip() or "common_dir_query_failed"
        if "verification_timeout" in error_text:
            error_text = "verification_timeout"
        return WorktreeIntegrityResult(
            ok=False,
            expected_branch=expected_branch,
            expected_worktree_path=expected_worktree_path,
            actual_toplevel=actual_toplevel,
            error=error_text,
        )
    raw_common_dir = result.stdout.strip()
    # Resolve relative common-dir against live_cwd
    if not os.path.isabs(raw_common_dir):
        actual_common_dir = os.path.realpath(os.path.join(live_cwd, raw_common_dir))
    else:
        actual_common_dir = os.path.realpath(raw_common_dir)

    # Signal 3: symbolic-ref (Class B detection)
    remaining = deadline - time.monotonic()
    result = _run_git_bounded(["symbolic-ref", "--short", "HEAD"], live_cwd, remaining)
    if result.returncode != 0:
        # Detached HEAD or timeout
        error_text = result.stderr.strip()
        if "verification_timeout" in error_text:
            return WorktreeIntegrityResult(
                ok=False,
                expected_branch=expected_branch,
                expected_worktree_path=expected_worktree_path,
                actual_toplevel=actual_toplevel,
                actual_common_dir=actual_common_dir,
                actual_branch=None,
                branch_escaped=True,
                cwd_escaped=(
                    os.path.realpath(actual_toplevel) != os.path.realpath(expected_worktree_path)
                ),
                error="verification_timeout",
            )
        # Detached HEAD → branch_escaped
        actual_branch: Optional[str] = None
    else:
        actual_branch = result.stdout.strip()

    # Evaluate
    cwd_escaped = os.path.realpath(actual_toplevel) != os.path.realpath(expected_worktree_path)
    expected_common = os.path.realpath(os.path.join(expected_repo_root, ".git"))
    common_dir_ok = os.path.realpath(actual_common_dir) == expected_common
    branch_escaped = actual_branch != expected_branch

    ok = not cwd_escaped and common_dir_ok and not branch_escaped

    return WorktreeIntegrityResult(
        ok=ok,
        expected_branch=expected_branch,
        expected_worktree_path=expected_worktree_path,
        actual_toplevel=actual_toplevel,
        actual_common_dir=actual_common_dir,
        actual_branch=actual_branch,
        cwd_escaped=cwd_escaped,
        branch_escaped=branch_escaped,
        error=None if ok else (None if not cwd_escaped and not branch_escaped else None),
    )
