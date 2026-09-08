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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ResumeLaunchSpec:
    """F829 A1 (D3): the per-provider resume input the create path consumes.

    Built from the conversation ROOT + its recovery MANIFEST (never from a
    filename), it carries EXACTLY the resume input each adapter's initialization
    needs, so MCP/HTTP/CLI can hand the same spec end to end:

    * codex / kiro — a resume-mode ``ForkContext`` carrying the stored
      ``provider_session_id`` (kiro's is the ``sess_<uuid>`` form, prefix
      included). ``fork_context`` is populated.
    * claude_code — ``resume_session_id`` (threaded to the claude constructor,
      ``--resume <sid>``). ``resume_session_id`` is populated.
    * pi — the recorded artifact path, launched as ``--session <artifact_locator>``
      (NEVER a fresh ``--session-id``). ``session_artifact_path`` is populated.

    ``identity_key`` and the resolved workspace/model/effort/namespace come from
    the root; ``capability_unverified`` names an unmeasured D10 capability key
    when the resume proceeds without passing evidence (D10). Exactly one of the
    three provider-input fields is set for a given provider.
    """

    identity_key: str
    provider: str
    provider_session_id: str
    provider_namespace: Optional[str] = None
    artifact_locator: Optional[str] = None
    working_directory: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    # Per-provider resume input (exactly one set per provider):
    fork_context: Any = None  # ForkContext(mode="resume") for codex/kiro
    resume_session_id: Optional[str] = None  # claude_code --resume
    session_artifact_path: Optional[str] = None  # pi --session <path>
    # D10: set to the unmeasured capability key when resume proceeds on an
    # unverified (missing/stale) capability row rather than a failed one.
    capability_unverified: Optional[str] = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


# The closed set of missing-fact tokens the ONE resume refusal can name
# (brief deliverable 4). Every resume precondition failure maps to exactly one.
RESUME_MISSING_TOKENS = frozenset(
    {"identity", "session_id", "artifact", "cwd", "provider_capability", "profile"}
)


class ResumeRefused(Exception):
    """The single typed refusal on the resume path (deliverable 4, addendum r1 #7).

    Serialized by the assign handler as the exact envelope::

        {"error": "resume_refused",
         "missing": <identity|session_id|artifact|cwd|provider_capability|profile>,
         "how": "<real command or fact>",
         "reason": "<snake_case detail>",
         "retryable": <bool>}

    This ONE shape replaces base_not_registered / base_name_unknown /
    anchor_not_forkable / provider_lacks_fork_capability / resume_profile_mismatch
    on the resume path. The fork path keeps those strings unchanged.
    """

    def __init__(
        self,
        missing: str,
        how: str,
        *,
        reason: str,
        retryable: bool = False,
        identity_key: Optional[str] = None,
        evidence_ref: Optional[str] = None,
    ):
        if missing not in RESUME_MISSING_TOKENS:
            raise ValueError(f"unknown resume-missing token: {missing!r}")
        self.missing = missing
        self.how = how
        self.reason = reason
        self.retryable = retryable
        # A1 D3: an AUTHORIZED refusal names the identity_key it concerns; an
        # optional evidence_ref points at a diag/event record. Both are omitted
        # from the envelope when None (unauthorized callers learn no identity).
        self.identity_key = identity_key
        self.evidence_ref = evidence_ref
        super().__init__(f"resume_refused:{missing}:{reason}")

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "error": "resume_refused",
            "missing": self.missing,
            "how": self.how,
            "reason": self.reason,
            "retryable": self.retryable,
        }
        # A1 D3: additive — present only when known, so the hot-fix envelope
        # shape is unchanged for a pre-authorization refusal.
        if self.identity_key is not None:
            out["identity_key"] = self.identity_key
        if self.evidence_ref is not None:
            out["evidence_ref"] = self.evidence_ref
        return out


# --------------------------------------------------------------------------
# Provider resume capability (brief deliverable 2)
# --------------------------------------------------------------------------
def provider_supports_resume(provider: str) -> bool:
    """Whether ``provider`` can RESUME a prior session (distinct from FORK).

    Addendum r1 #6 / r2 #2: this checks ONLY the explicit ``supports_resume``
    class flag — it does NOT fall back to ``supports_fork_context``. In this
    slice ``supports_resume`` is True only for providers with a real, exercised
    resume input path: codex (resume-mode fork_context) and kiro
    (``--resume-id sess_<uuid>``). grok/claude/pi declare False here (claude
    needs resume_session_id threaded through the MCP wrapper; pi needs
    ``--session <path>`` — both are F829 proper). An unknown provider, or one
    that never opts in, is not resumable.
    """
    from cli_agent_orchestrator.providers.manager import get_provider_class

    try:
        cls = get_provider_class(provider)
    except ValueError:
        return False
    return bool(getattr(cls, "supports_resume", False))


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


def capture_kiro_session_id_from_store(
    cwd: str,
    terminal_id: str,
    *,
    capture_nonce: Optional[str] = None,
    recorded_locator: Optional[str] = None,
    sessions_root: Optional[Path] = None,
) -> tuple[Optional[str], Optional[str], int]:
    """Positively attribute a reaped kiro terminal's session id from the store.

    Reads ONLY the filesystem (the account may be dead at reap time). Returns
    ``(session_id, reason, candidate_count)``:
    * ``session_id`` — the ``sess_<uuid>`` id, or None when it cannot be
      POSITIVELY attributed.
    * ``reason`` — None on success, else a snake_case detail
      (``capture_unknown``) surfaced to the operator.
    * ``candidate_count`` — how many cwd-matching sessions were seen (for the
      hint).

    Addendum r1 #2 / verdict B4 — PER-ATTEMPT POSITIVE ATTRIBUTION ONLY. The
    former "newest by mtime" rule is STRUCK. A session id binds only when:
      (1) ``recorded_locator`` is set (a locator CAO already recorded) — used
          verbatim; OR
      (2) EXACTLY ONE session under ``~/.kiro/sessions/<sha256(cwd)[:16]>/``
          whose ``session.json`` names ``cwd`` (rootPaths/workspacePaths) AND
          whose ``messages.jsonl`` carries THIS launch attempt's marker.

    The attempt marker is the per-launch ``capture_nonce`` when one was minted
    and injected (verdict B4: a per-attempt nonce, not a copyable per-terminal
    string). When no nonce was recorded (legacy rows / a spawn before this
    slice) it falls back to the per-terminal ``[Assigned by terminal <id>``
    assign-trailer marker. Never binds on cwd+mtime alone. Zero or >1 attributed
    matches → (None, "capture_unknown", count)."""
    if recorded_locator:
        return recorded_locator, None, 1
    root = sessions_root if sessions_root is not None else _kiro_sessions_root()
    hash_dir = root / _cwd_hash(cwd)
    if not hash_dir.is_dir():
        return None, "capture_unknown", 0
    target = os.path.realpath(cwd)
    # Prefer the per-attempt nonce marker (positive, non-copyable); fall back to
    # the per-terminal assign-trailer only when no nonce was minted.
    marker = capture_nonce if capture_nonce else f"[Assigned by terminal {terminal_id}"
    cwd_matches = 0
    attributed: list[str] = []
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
        cwd_matches += 1
        session_id = meta.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if _transcript_contains_marker(sess_dir / "messages.jsonl", marker):
            attributed.append(session_id)
    if len(attributed) == 1:
        return attributed[0], None, cwd_matches
    # Zero attributed, or ambiguous (>1) — refuse to guess.
    return None, "capture_unknown", cwd_matches


def _transcript_contains_marker(transcript: Path, marker: str) -> bool:
    """True iff ``marker`` appears anywhere in the kiro messages.jsonl.

    The marker is the per-terminal ``[Assigned by terminal <id>`` assign-trailer
    text, which is unique per terminal, so a hit is positive attribution.
    Read as bytes and substring-matched to avoid per-line JSON parsing of a
    multi-MB transcript.
    """
    try:
        data = transcript.read_bytes()
    except OSError:
        return False
    return marker.encode("utf-8") in data


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
    """Human-facing reason a kiro reap could not positively attribute a session.

    Distinguishes "no session dir for this cwd" from "no / ambiguous attributed
    match" so the operator knows whether the worker ever persisted a turn.
    """
    root = sessions_root if sessions_root is not None else _kiro_sessions_root()
    hash_dir = root / _cwd_hash(cwd)
    if not hash_dir.is_dir():
        return (
            f"no kiro session store for cwd {cwd} "
            f"(expected {hash_dir}); the worker persisted no conversation turn"
        )
    return (
        f"kiro session id could not be positively attributed for cwd {cwd}: no "
        f"(or ambiguous) session under {hash_dir} carried this terminal's own "
        f"assign-trailer marker"
    )


# --------------------------------------------------------------------------
# The resume-target resolver (brief deliverable 1)
# --------------------------------------------------------------------------
def resolve_resume_target(value: str) -> dict[str, Any]:
    """Resolve ``assign(resume_from=<value>)`` to the facts a resume needs.

    NO fork-base (``provider_sessions``) row is consulted (addendum r2 #1) and NO
    base name is involved. Resolution order:

    (a) a live OR reaped terminal id → its ``terminal_identity`` row (F631; it
        survives reap) → provider, cwd, agent_profile, provider_session_id,
        worktree_path, git_sha, session_name.
    (b) a bare uuid → ``terminal_identity.provider_session_id`` ONLY (for kiro,
        the ``sess_``-prefixed form is what is stored). The fork-base catalog is
        never consulted by resume.

    Returns a row-like dict with the keys the assign resume path consumes:
    ``name`` (the durable handle — the historical terminal id), ``session_uuid``
    (the resume key), ``provider``, ``cwd``, ``agent_profile``,
    ``worktree_path``, ``git_sha``, ``source_terminal_id``.

    Raises ``ResumeRefused`` — never a fork-path string:
    * unknown value → missing="identity"
    * identity found but no captured session id → missing="session_id"
    """
    from cli_agent_orchestrator.clients.database import (
        get_terminal_identity,
        get_terminal_identity_by_provider_session_id,
    )

    # (a) terminal id — live or reaped identity row.
    identity = get_terminal_identity(value)
    if identity is not None:
        return _row_from_identity(identity, handle=value)

    # (b) bare uuid: the identity registry ONLY (survives reap). r2 #1 drops the
    # former provider_sessions fallback — resume never reads the fork-base catalog.
    identity = get_terminal_identity_by_provider_session_id(value)
    if identity is not None:
        return _row_from_identity(identity, handle=identity.get("terminal_id") or value)

    raise ResumeRefused(
        missing="identity",
        how=(
            f"no live or reaped terminal, and no captured provider session, "
            f"matches {value!r}; pass a terminal id you remember (dead or alive) "
            f"or a provider session uuid a reap returned as provider_session_id"
        ),
        reason="no_identity_match",
    )


def _row_from_identity(identity: dict[str, Any], *, handle: str) -> dict[str, Any]:
    """Shape a ``terminal_identity`` row into the assign resume path's row dict.

    Refuses with missing="session_id" when the row has no captured provider
    session id — resume cannot re-attach a conversation whose id was never
    recorded (for kiro this is what the reap-time capture, deliverable 3, fills
    in; if it is still NULL the worker never persisted a resumable turn).
    """
    session_uuid = identity.get("provider_session_id")
    terminal_id = identity.get("terminal_id") or handle
    if not session_uuid:
        raise ResumeRefused(
            missing="session_id",
            how=(
                f"terminal {terminal_id!r} has no captured provider session id; "
                f"it was reaped before a resumable session was recorded (for kiro, "
                f"before any conversation turn was attributably written to disk)"
            ),
            reason="provider_session_id_null",
        )
    return {
        "name": terminal_id,
        "session_uuid": session_uuid,
        "provider": identity.get("provider"),
        "cwd": identity.get("cwd"),
        "agent_profile": identity.get("agent_profile"),
        "worktree_path": identity.get("worktree_path"),
        "worktree_branch": identity.get("worktree_branch"),
        "worktree_repo_root": identity.get("worktree_repo_root"),
        "git_sha": identity.get("git_sha"),
        "source_terminal_id": terminal_id,
    }


# --------------------------------------------------------------------------
# assign(resume_from=…) orchestration (brief deliverable 1 + 2 + 4)
# --------------------------------------------------------------------------
def _ensure_resume_cwd(
    row_cwd: Optional[str],
    worktree_path: Optional[str],
    worktree_branch: Optional[str],
    worktree_repo_root: Optional[str],
    git_sha: Optional[str],
    handle: str,
) -> str:
    """Resolve (and, if necessary, reconstruct) the cwd a resume must run in.

    cwd defaults to the identity row's cwd. When that directory is gone
    (evidence §1.5 — reap-time abandon or GC removed a kiro worktree keyed by
    cwd hash), reconstruct it ONLY from a fully recorded worktree provenance
    (addendum r1 #7): the recorded worktree path + branch + commit (+ the repo
    it belongs to), with the branch tip verified to exist. A ``cao/<id>``
    branch-name GUESS is NOT sufficient on its own.

    Returns the live cwd on success. Raises ``ResumeRefused(missing="cwd")``.
    """
    cwd = worktree_path or row_cwd
    if not cwd:
        raise ResumeRefused(
            missing="cwd",
            how=f"pass working_directory= for {handle!r} (no cwd was recorded)",
            reason="cwd_unrecorded",
        )
    if os.path.isdir(cwd):
        return cwd
    # Directory gone — reconstruct ONLY from full recorded provenance.
    if worktree_path and worktree_branch and git_sha:
        recreated = _reconstruct_worktree(
            worktree_path, worktree_branch, git_sha, worktree_repo_root
        )
        if recreated is not None:
            return recreated
        raise ResumeRefused(
            missing="cwd",
            how=(
                f"recorded worktree {worktree_path!r} for {handle!r} is gone and "
                f"branch {worktree_branch!r}@{git_sha[:8]} could not be checked "
                f"out there; re-create that checkout at that exact path (kiro "
                f"keys the session by it) or pass working_directory="
            ),
            reason="worktree_reconstruct_failed",
        )
    raise ResumeRefused(
        missing="cwd",
        how=(
            f"working directory {cwd!r} for {handle!r} no longer exists and its "
            f"worktree provenance (path+branch+commit) was not fully recorded, "
            f"so it cannot be safely reconstructed; pass working_directory="
        ),
        reason="cwd_missing_no_provenance",
    )


def _reconstruct_worktree(
    worktree_path: str, branch: str, commit: str, repo_root: Optional[str]
) -> Optional[str]:
    """Re-create the worktree at ``worktree_path`` on ``branch`` @ ``commit``.

    Anchors the ``git worktree add`` on the RECORDED ``repo_root`` (an off-repo
    checkout's own parent chain is not inside the repo, so an ancestor-walk
    cannot find it); falls back to the ancestor-walk only when repo_root was not
    recorded. Verifies the branch tip exists AND the recorded commit is
    reachable from it before adding — a bare branch-name guess is refused by the
    caller, and a branch whose tip cannot be verified returns None. The path is
    preserved exactly because kiro's session store is keyed by
    ``sha256(cwd)[:16]``. Returns the path on success, else None.
    """
    import subprocess

    anchor = repo_root if (repo_root and os.path.isdir(repo_root)) else None
    if anchor is None:
        anchor = _nearest_repo_ancestor(worktree_path)
    if anchor is None:
        return None
    tip = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=anchor,
        capture_output=True,
        text=True,
    )
    if tip.returncode != 0 or not tip.stdout.strip():
        return None
    reachable = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, branch],
        cwd=anchor,
        capture_output=True,
        text=True,
    )
    if reachable.returncode != 0 and tip.stdout.strip() != commit:
        return None
    try:
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    except OSError:
        return None
    add = subprocess.run(
        ["git", "worktree", "add", worktree_path, branch],
        cwd=anchor,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        logger.warning(
            "resume: could not reconstruct worktree %s on %s@%s: %s",
            worktree_path,
            branch,
            commit[:8],
            add.stderr.strip(),
        )
        return None
    return worktree_path


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


# --------------------------------------------------------------------------
# A1 D3: identity-root resolution + authorization → ResumeLaunchSpec
# --------------------------------------------------------------------------

# D3 blocker order maps each classify/authorize/claim token to exactly one of
# the six public ``missing`` categories. State/claim/ownership refusals are all
# ``identity``; an artifact-state refusal is ``artifact``.
_RESUME_TOKEN_TO_MISSING = {
    "resume_not_owner": "identity",
    "session_live_owned": "identity",
    "session_abandoned": "identity",
    "session_expired": "identity",
    "session_resume_in_progress": "identity",
    "session_ambiguous": "identity",
    "session_artifact_missing": "artifact",
    "session_artifact_unavailable": "artifact",
    "session_artifact_invalid": "artifact",
    "session_identity_mismatch": "artifact",
    "session_identity_conflict": "artifact",
}

_RESUME_TOKEN_RETRYABLE = {
    "session_resume_in_progress": True,
    "session_artifact_unavailable": True,
}


def _build_launch_spec(
    root: dict[str, Any],
    manifest: Optional[dict[str, Any]],
    working_directory: Optional[str],
) -> "ResumeLaunchSpec":
    """A1 D3 step 5: build the per-provider ``ResumeLaunchSpec`` from root+manifest.

    The provider arm is gated by the D10 runtime admission (``admit_capability``):
    a FAILED ``resume`` evidence row refuses here with
    ``missing=provider_capability``; a missing/stale key admits and the spec
    carries ``capability_unverified``. Exactly one provider-input field is set:
    codex/kiro → ``fork_context`` (resume mode, stored id incl. kiro ``sess_``
    prefix); claude → ``resume_session_id``; pi → ``session_artifact_path``
    (the recorded artifact locator, launched as ``--session <path>``).
    """
    from cli_agent_orchestrator.models.terminal import ForkContext
    from cli_agent_orchestrator.services.capability_evidence import admit_resume_capability

    provider = root["provider"]
    identity_key = root["identity_key"]
    session_uuid = root.get("provider_session_id")
    namespace = root.get("provider_namespace")
    artifact_locator = root.get("artifact_locator") or (
        manifest.get("worktree_path") if manifest else None
    )

    # D10 runtime admission for the resume operation. This is the PRODUCTION
    # seam (blueprint D10): it enforces declaration (provider_declares) AND reads
    # the persisted exact-key evidence — a FAILED row refuses here with
    # missing=provider_capability; a missing/stale key admits and the spec
    # carries capability_unverified; a provider that does not DECLARE resume is
    # refused before any evidence read.
    verdict = admit_resume_capability(provider)
    if not verdict.admitted:
        raise ResumeRefused(
            missing="provider_capability",
            how=(
                f"provider {provider!r} resume capability is not admitted "
                f"({verdict.reason}); re-probe with `cao providers probe "
                f"--provider {provider}` or resume a provider whose resume "
                f"capability is declared and not failed"
            ),
            reason=verdict.reason or f"provider_{provider}_resume_capability_failed",
            retryable=True,
            identity_key=identity_key,
        )

    if not session_uuid:
        raise ResumeRefused(
            missing="session_id",
            how=(
                f"conversation {identity_key!r} has no captured provider session id "
                f"(capture_unknown); it has nothing to resume by uuid"
            ),
            reason="provider_session_id_null",
            identity_key=identity_key,
        )

    spec = ResumeLaunchSpec(
        identity_key=identity_key,
        provider=provider,
        provider_session_id=session_uuid,
        provider_namespace=namespace,
        artifact_locator=artifact_locator,
        working_directory=working_directory,
        model=root.get("model"),
        reasoning_effort=root.get("reasoning_effort"),
        capability_unverified=verdict.unverified_key,
    )
    # Per-provider resume input (exactly one). All arms carry a resume-mode
    # ForkContext so the single create-path fork_context thread reaches every
    # adapter; codex/kiro use session_uuid, claude uses resume_session_id, pi
    # uses session_artifact_path (the extra fields are ignored by adapters that
    # do not read them).
    if provider in ("codex", "kiro_cli", "grok_cli"):
        spec.fork_context = ForkContext(
            mode="resume",
            session_uuid=session_uuid,
            base_name=str(identity_key),
            provider=provider,
            identity_key=identity_key,
            capability_unverified=verdict.unverified_key,
            initial_preamble=(
                f"[RESUMED] Re-attached to your prior conversation "
                f"(identity {identity_key}). Continue where you left off."
            ),
        )
    elif provider == "claude_code":
        spec.resume_session_id = session_uuid
        spec.fork_context = ForkContext(
            mode="resume",
            session_uuid=session_uuid,
            base_name=str(identity_key),
            provider=provider,
            resume_session_id=session_uuid,
            identity_key=identity_key,
            capability_unverified=verdict.unverified_key,
            initial_preamble=(
                f"[RESUMED] Re-attached to your prior conversation "
                f"(identity {identity_key}). Continue where you left off."
            ),
        )
    elif provider == "pi_cli":
        if not artifact_locator:
            raise ResumeRefused(
                missing="artifact",
                how=(
                    f"pi conversation {identity_key!r} recorded no artifact path; "
                    f"a mid-turn crash leaves no durable artifact (session_artifact_missing)"
                ),
                reason="pi_artifact_locator_null",
                identity_key=identity_key,
            )
        spec.session_artifact_path = artifact_locator
        spec.fork_context = ForkContext(
            mode="resume",
            session_uuid=session_uuid,
            base_name=str(identity_key),
            provider=provider,
            session_artifact_path=artifact_locator,
            identity_key=identity_key,
            capability_unverified=verdict.unverified_key,
            initial_preamble=(
                f"[RESUMED] Re-attached to your prior conversation "
                f"(identity {identity_key}). Continue where you left off."
            ),
        )
    else:
        raise ResumeRefused(
            missing="provider_capability",
            how=f"provider {provider!r} has no resume launch arm in this build",
            reason=f"provider_{provider}_no_resume_arm",
            identity_key=identity_key,
        )
    return spec


def _prepare_resume_via_identity(
    *,
    resume_from: str,
    requested_agent_profile: Optional[str],
    requested_working_directory: Optional[str],
    caller_principal: Optional[str],
    inherit_pins: bool,
) -> Optional[dict[str, Any]]:
    """A1 D3: resolve through the conversation ROOT + recovery MANIFEST, authorize
    against ``owner_principal``, and return the enriched prepared dict — or None
    when there is NO F829 root for ``resume_from`` (the caller then falls back to
    the hot-fix terminal_identity path).

    On an authorization/classify/artifact refusal raises ``ResumeRefused`` with
    the mapped ``missing`` category and ``identity_key`` populated (D3 six-category
    envelope). The CAS CLAIM is NOT taken here — it is taken at spawn time by the
    server entrance (claim_resume_admission), preserving the existing claim seam.
    """
    from cli_agent_orchestrator.clients.database import (
        get_frozen_pins,
        get_recovery_manifest,
        resolve_conversation_identity,
    )
    from cli_agent_orchestrator.services.conversation_transition import (
        authorize_and_classify_resume,
    )

    try:
        root = resolve_conversation_identity(resume_from)
    except ValueError as exc:
        # resolve raises session_ambiguous for >1 canonical match.
        token = str(exc) or "session_ambiguous"
        missing = _RESUME_TOKEN_TO_MISSING.get(token, "identity")
        raise ResumeRefused(
            missing=missing,
            how="multiple conversations match; pass the identity_key",
            reason=token,
        )
    if root is None:
        return None  # no F829 root — fall back to the hot-fix path

    identity_key = root["identity_key"]
    # AUTHORIZE + CLASSIFY (owner_principal; resumable set {hibernated,detached}).
    admission = authorize_and_classify_resume(root, caller_principal)
    if not admission.ok:
        token = admission.error or "resume_not_owner"
        raise ResumeRefused(
            missing=_RESUME_TOKEN_TO_MISSING.get(token, "identity"),
            how=_resume_how_for_token(token, identity_key),
            reason=token,
            retryable=_RESUME_TOKEN_RETRYABLE.get(token, False),
            identity_key=identity_key,
        )

    provider = root["provider"]
    agent_profile = requested_agent_profile or root.get("agent_profile")
    if not agent_profile:
        raise ResumeRefused(
            missing="profile",
            how=f"pass agent_profile= for the resumed worker (none recorded for {identity_key!r})",
            reason="agent_profile_unrecorded",
            identity_key=identity_key,
        )

    manifest = get_recovery_manifest(identity_key)
    # cwd from the manifest (authoritative), else the root has none recorded.
    row_cwd = manifest.get("cwd") if manifest else None
    wt_path = manifest.get("worktree_path") if manifest else None
    wt_branch = manifest.get("worktree_branch") if manifest else None
    wt_repo = manifest.get("repo_root") if manifest else None
    wt_commit = manifest.get("worktree_commit") if manifest else None
    working_directory = requested_working_directory or _ensure_resume_cwd(
        row_cwd, wt_path, wt_branch, wt_repo, wt_commit, str(identity_key)
    )

    spec = _build_launch_spec(root, manifest, working_directory)

    # Frozen pins inherited from the CURRENT incarnation (D2/A1 inherit_pins).
    current_terminal = root.get("current_terminal_id")
    known_pins = get_frozen_pins(str(current_terminal)) if current_terminal else []
    authority_files: Optional[list[dict[str, str]]] = None
    pins_inherited = 0
    if inherit_pins:
        authority_files = known_pins or None
        pins_inherited = len(known_pins)

    fork_context = spec.fork_context
    if fork_context is None:
        # claude/pi have no ForkContext; the create path uses resume_session_id /
        # session_artifact_path from the spec. Provide a minimal resume-mode
        # ForkContext only for the providers that consume it; others pass None.
        pass

    return {
        "via_identity": True,
        "identity_key": identity_key,
        "launch_spec": spec,
        "fork_context": fork_context,
        "resume_session_id": spec.resume_session_id,
        "session_artifact_path": spec.session_artifact_path,
        "capability_unverified": spec.capability_unverified,
        "provider": provider,
        "agent_profile": agent_profile,
        "working_directory": working_directory,
        "admission": admission,
        "forked_from_info": {
            "name": str(current_terminal or identity_key),
            "cwd": working_directory,
            "resumed_from": str(resume_from),
            "provider": provider,
            "identity_key": identity_key,
        },
        "authority_files": authority_files,
        "pins_inherited": pins_inherited,
        "known_pins": known_pins,
    }


def _resume_how_for_token(token: str, identity_key: str) -> str:
    """A human ``how`` remedy for an authorize/classify refusal token."""
    return {
        "resume_not_owner": (
            "you are not the recorded owner of this conversation; the owner must "
            "claim it with `cao identity claim` before it can be resumed"
        ),
        "session_live_owned": "the conversation is still live and owned; interrupt it, do not resume",
        "session_abandoned": "the conversation was explicitly reaped (abandoned); it is not resumable",
        "session_expired": "the conversation expired under the configured retention policy",
        "session_artifact_missing": (
            f"conversation {identity_key!r} has no captured/recoverable artifact"
        ),
    }.get(token, f"resume of {identity_key!r} refused: {token}")


def prepare_resume(
    *,
    resume_from: str,
    requested_agent_profile: Optional[str],
    requested_working_directory: Optional[str],
    inherit_pins: bool = True,
    caller_principal: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve everything an ``assign(resume_from=…)`` needs, or raise ResumeRefused.

    Returns a dict:
      * ``fork_context`` — a ForkContext(mode="resume") to hand to the create path
      * ``provider`` — the resolved provider
      * ``agent_profile`` — requested override, else the identity's
      * ``working_directory`` — the live (possibly reconstructed) cwd
      * ``forked_from_info`` — {name, cwd, resumed_from, provider}
      * ``authority_files`` — inherited frozen pins when inherit_pins, else None
      * ``pins_inherited`` — count of inherited pins (for the success line)

    Every precondition failure raises ``ResumeRefused`` (deliverable 4): the ONE
    typed answer, never a fork-path string.

    ``inherit_pins`` defaults True (addendum r1 #5): the new terminal re-declares
    the reaped pin set (same shas), which the create path re-verifies before
    continuation. ``inherit_pins=False`` when the reaped terminal HAD frozen pins
    requires the caller to pass equivalent explicit ``authority_files`` (checked
    in the assign handler); otherwise this refuses with missing="profile".
    """
    from cli_agent_orchestrator.clients.database import get_frozen_pins
    from cli_agent_orchestrator.models.terminal import ForkContext

    # A1 D3: resolve through the conversation ROOT + recovery MANIFEST first,
    # authorizing against owner_principal and building a ResumeLaunchSpec that
    # carries the correct per-provider arm (codex/kiro fork_context, claude
    # resume_session_id, pi --session <artifact>). Returns None only when there
    # is NO F829 root for this handle, in which case we fall back to the hot-fix
    # terminal_identity path below (backward compatibility for pre-F829 rows).
    via_identity = _prepare_resume_via_identity(
        resume_from=resume_from,
        requested_agent_profile=requested_agent_profile,
        requested_working_directory=requested_working_directory,
        caller_principal=caller_principal,
        inherit_pins=inherit_pins,
    )
    if via_identity is not None:
        return via_identity

    row = resolve_resume_target(resume_from)
    provider = row.get("provider")
    if not provider:
        raise ResumeRefused(
            missing="identity",
            how=f"re-dispatch {resume_from!r} cold — its identity recorded no provider",
            reason="provider_unrecorded",
        )
    # Capability: kiro/codex resume; grok/claude/pi refuse here (r1 #6 / r2 #2).
    if not provider_supports_resume(provider):
        raise ResumeRefused(
            missing="provider_capability",
            how="not resumable in this build; F829 build 2",
            reason=f"provider_{provider}_not_resumable",
        )
    agent_profile = requested_agent_profile or row.get("agent_profile")
    if not agent_profile:
        raise ResumeRefused(
            missing="profile",
            how=f"pass agent_profile= for the resumed worker (none recorded for {resume_from!r})",
            reason="agent_profile_unrecorded",
        )
    handle = row.get("source_terminal_id") or row.get("name") or resume_from
    working_directory = requested_working_directory or _ensure_resume_cwd(
        row.get("cwd"),
        row.get("worktree_path"),
        row.get("worktree_branch"),
        row.get("worktree_repo_root"),
        row.get("git_sha"),
        str(handle),
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
    known_pins = (
        get_frozen_pins(str(row["source_terminal_id"])) if row.get("source_terminal_id") else []
    )
    authority_files: Optional[list[dict[str, str]]] = None
    pins_inherited = 0
    if inherit_pins:
        authority_files = known_pins or None
        pins_inherited = len(known_pins)
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
        "pins_inherited": pins_inherited,
        "known_pins": known_pins,
    }
