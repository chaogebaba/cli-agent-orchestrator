"""F829 D6: provider-specific recoverable-artifact resolution and validation.

Artifact truth is provider-specific and VALIDATED, never inferred from a bare
filename (co-design Q6). Each resolver targets the exact on-disk shape the D9
crash-resume probe confirmed for its provider, and returns one of four typed
outcomes:

* ``valid``        — a recoverable artifact exists AND validates.
* ``missing``      — the artifact is genuinely absent (never captured, or a pi
                     mid-turn crash that wrote nothing). Not retryable by itself.
* ``inaccessible`` — the store dir/home could not be read (mount/permission).
                     RETRYABLE: the record must NOT be expired on this (D6).
* ``invalid``      — a file exists but does not validate as this identity
                     (wrong id inside, truncated, wrong shape).

Expiry is NEVER decided here (D6): a resolver that returns ``inaccessible`` or
``missing`` must not cause a lifecycle change on its own. Only an explicit
configured retention policy (``identity.retention_days``) expires a record, and
that lives in the settings-driven caller, not this module.

Probe facts folded in (see ``/data/cao-scratch/briefs/f829-d9-probe.md``):
* claude_code — ``~/.claude/projects/<slug>/<uuid>.jsonl``; the hook-resolved id
  is authoritative (never the requested id); resume resolves globally by uuid.
* codex — ``$CODEX_HOME/sessions/Y/M/D/rollout-*-<uuid>.jsonl``; delegated to the
  provider's own ``validate_session_artifact`` when a live provider is available,
  else a filesystem check under the resolved home.
* kiro — the v3 NESTED store ``~/.kiro/sessions/<cwd-hash>/<sess_uuid>/`` with
  BOTH ``session.json`` AND ``messages.jsonl`` required; the id is ``sess_<uuid>``
  INCLUDING the prefix. The old flat ``sessions/cli/<uuid>.jsonl`` (v1/v2) is NOT
  consulted. Resolved by globbing the uuid dir (uuid is globally unique across
  cwd-hashes), never by reimplementing KAS's cwd hash.
* pi — the recorded ``<timestamp>_<uuid>.jsonl`` path in the session dir (not a
  ``<uuid>*`` glob); pi writes it ATOMICALLY at turn completion, so a pi identity
  with no file is a mid-turn crash and is ``missing`` (unrecoverable), never
  cold-started (D8).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class ArtifactState(str, Enum):
    VALID = "valid"
    MISSING = "missing"
    INACCESSIBLE = "inaccessible"
    INVALID = "invalid"


@dataclass(frozen=True)
class ArtifactStatus:
    """The typed outcome of resolving one conversation's recoverable artifact."""

    state: ArtifactState
    locator: Optional[str] = None  # the resolved artifact path, when known
    detail: Optional[str] = None  # human/diagnostic reason

    @property
    def is_valid(self) -> bool:
        return self.state is ArtifactState.VALID

    @property
    def is_retryable(self) -> bool:
        # Only an inaccessible store is retryable — the artifact may return when
        # a mount/permission issue clears. Missing/invalid are terminal facts.
        return self.state is ArtifactState.INACCESSIBLE


def _provider_home(provider: str) -> Optional[Path]:
    try:
        from cli_agent_orchestrator.utils.provider_plane import provider_home

        return provider_home(provider).home
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-provider resolvers
# ---------------------------------------------------------------------------


def _resolve_claude(uuid: str, namespace: Optional[str]) -> ArtifactStatus:
    """claude_code: the hook-resolved ``<uuid>.jsonl`` under projects/.

    Resume resolves globally by uuid, so we glob every project slug rather than
    reconstruct the cwd slug. The hook-resolved id is authoritative; a divergence
    between the requested id and the on-disk id is the caller's concern (D3), not
    this resolver's.
    """
    home = _provider_home("claude_code")
    if home is None:
        return ArtifactStatus(ArtifactState.INACCESSIBLE, detail="claude home unresolved")
    projects = home / "projects"
    try:
        if not projects.is_dir():
            return ArtifactStatus(ArtifactState.MISSING, detail="no projects dir")
        matches = list(projects.glob(f"*/{uuid}.jsonl"))
    except OSError as exc:
        return ArtifactStatus(ArtifactState.INACCESSIBLE, detail=f"projects unreadable: {exc}")
    if not matches:
        return ArtifactStatus(ArtifactState.MISSING, detail="no transcript for uuid")
    path = matches[0]
    try:
        if path.stat().st_size == 0:
            return ArtifactStatus(ArtifactState.INVALID, str(path), "empty transcript")
    except OSError as exc:
        return ArtifactStatus(ArtifactState.INACCESSIBLE, str(path), f"stat failed: {exc}")
    return ArtifactStatus(ArtifactState.VALID, str(path))


def _resolve_codex(uuid: str, namespace: Optional[str], cwd: Optional[str]) -> ArtifactStatus:
    """codex: rollout under the effective CODEX_HOME.

    ``namespace`` is the recorded ``provider_namespace`` (the CODEX_HOME the
    artifact lives in). Falls back to the default provider home when unset. Uses
    the same rollout shape the provider's ``validate_session_artifact`` checks
    (``session_meta`` first line with ``payload.id == uuid``).
    """
    home: Optional[Path]
    if namespace and not namespace.startswith("default:"):
        home = Path(namespace)
    else:
        home = _provider_home("codex")
    if home is None:
        return ArtifactStatus(ArtifactState.INACCESSIBLE, detail="codex home unresolved")
    sessions = home / "sessions"
    try:
        if not sessions.is_dir():
            return ArtifactStatus(ArtifactState.MISSING, detail="no sessions dir")
        matches = list(sessions.glob(f"**/rollout-*{uuid}*.jsonl"))
    except OSError as exc:
        return ArtifactStatus(ArtifactState.INACCESSIBLE, detail=f"sessions unreadable: {exc}")
    if not matches:
        return ArtifactStatus(ArtifactState.MISSING, detail="no rollout for uuid")
    if len(matches) > 1:
        return ArtifactStatus(ArtifactState.INVALID, detail="ambiguous rollout (>1 match)")
    path = matches[0]
    try:
        with path.open(encoding="utf-8") as stream:
            first = json.loads(stream.readline() or "{}")
    except OSError as exc:
        return ArtifactStatus(ArtifactState.INACCESSIBLE, str(path), f"read failed: {exc}")
    except (json.JSONDecodeError, ValueError):
        return ArtifactStatus(ArtifactState.INVALID, str(path), "unparseable first line")
    if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != uuid:
        return ArtifactStatus(ArtifactState.INVALID, str(path), "rollout identity mismatch")
    return ArtifactStatus(ArtifactState.VALID, str(path))


def _normalise_kiro_id(uuid: str) -> str:
    """The kiro id is ``sess_<uuid>`` INCLUDING the prefix (probe)."""
    return uuid if uuid.startswith("sess_") else f"sess_{uuid}"


def _resolve_kiro(uuid: str, namespace: Optional[str]) -> ArtifactStatus:
    """kiro recoverable artifact — supports BOTH store layouts (kiro-cli versions
    differ across the laptop and boxes; see the arm report's `kiro-cli --version`):

    * v3 NESTED: ``sessions/<cwd-hash>/<sess_uuid>/`` with BOTH ``session.json``
      AND ``messages.jsonl`` required. The uuid dir is globally unique across
      cwd-hashes, so we glob the ``sess_<uuid>`` dir rather than reimplement KAS's
      cwd hash.
    * FLAT (kiro-cli 2.20.1): ``sessions/cli/<uuid>.json`` (meta) + ``<uuid>.jsonl``
      (transcript). A valid recoverable artifact when the transcript is present
      and non-empty. (F829 kiro-harness: the flat store is what 2.20.1 actually
      writes; treating it as "not consulted" wrongly refused hibernate with
      session_artifact_unavailable.)
    """
    # kiro has no provider-plane "native-home" object (provider_home raises), so
    # resolve the kiro home the SAME way the capture path does — KIRO_HOME env
    # else ~/.kiro — rather than via _provider_home (which returns None for kiro
    # and wrongly yielded "kiro home unresolved" / session_artifact_unavailable).
    from cli_agent_orchestrator.services.resume_service import _kiro_sessions_root

    try:
        sessions = _kiro_sessions_root()
    except Exception:
        return ArtifactStatus(ArtifactState.INACCESSIBLE, detail="kiro home unresolved")
    sess_id = _normalise_kiro_id(uuid)
    try:
        if not sessions.is_dir():
            return ArtifactStatus(ArtifactState.MISSING, detail="no sessions dir")
        dirs = [p for p in sessions.glob(f"*/{sess_id}") if p.is_dir()]
    except OSError as exc:
        return ArtifactStatus(ArtifactState.INACCESSIBLE, detail=f"sessions unreadable: {exc}")
    if dirs:
        session_dir = dirs[0]
        session_json = session_dir / "session.json"
        messages = session_dir / "messages.jsonl"
        try:
            have_json = session_json.is_file()
            have_msgs = messages.is_file()
        except OSError as exc:
            return ArtifactStatus(
                ArtifactState.INACCESSIBLE, str(session_dir), f"stat failed: {exc}"
            )
        if not (have_json and have_msgs):
            return ArtifactStatus(
                ArtifactState.INVALID,
                str(session_dir),
                f"required pair incomplete (session.json={have_json}, messages.jsonl={have_msgs})",
            )
        return ArtifactStatus(ArtifactState.VALID, str(messages))
    # FLAT layout fallback: sessions/cli/<uuid>.jsonl (+ <uuid>.json). Accept both
    # the bare-uuid and the sess_-prefixed forms for the filename stem.
    cli_dir = sessions / "cli"
    for stem in (
        uuid,
        sess_id,
        sess_id[len("sess_") :] if sess_id.startswith("sess_") else sess_id,
    ):
        transcript = cli_dir / f"{stem}.jsonl"
        try:
            if transcript.is_file():
                if transcript.stat().st_size == 0:
                    return ArtifactStatus(
                        ArtifactState.INVALID, str(transcript), "empty kiro flat session"
                    )
                return ArtifactStatus(ArtifactState.VALID, str(transcript))
        except OSError as exc:
            return ArtifactStatus(
                ArtifactState.INACCESSIBLE, str(transcript), f"stat failed: {exc}"
            )
    return ArtifactStatus(ArtifactState.MISSING, detail="no v3 session dir or flat cli/ session")


def _resolve_pi(
    uuid: str, namespace: Optional[str], artifact_locator: Optional[str]
) -> ArtifactStatus:
    """pi: the recorded ``<timestamp>_<uuid>.jsonl`` in the session dir.

    pi writes the file atomically at turn completion, so a pi identity with no
    file is a mid-turn crash: ``missing`` (unrecoverable), never cold-started
    (D8). We prefer the recorded ``artifact_locator`` (the exact path), and fall
    back to a ``*_<uuid>.jsonl`` glob under the session dir/namespace.
    """
    if artifact_locator:
        path = Path(artifact_locator)
        try:
            if path.is_file():
                if path.stat().st_size == 0:
                    return ArtifactStatus(ArtifactState.INVALID, str(path), "empty pi session")
                return ArtifactStatus(ArtifactState.VALID, str(path))
        except OSError as exc:
            return ArtifactStatus(ArtifactState.INACCESSIBLE, str(path), f"stat failed: {exc}")
        # recorded path gone → missing (mid-turn crash or deleted)
        return ArtifactStatus(ArtifactState.MISSING, detail="recorded pi session absent")
    # No recorded locator: search the namespace/session dir if we have one.
    if namespace and not namespace.startswith("default:"):
        session_dir = Path(namespace)
    else:
        home = _provider_home("pi_cli")
        if home is None:
            return ArtifactStatus(ArtifactState.INACCESSIBLE, detail="pi home unresolved")
        session_dir = home / "agent" / "sessions"
    try:
        if not session_dir.exists():
            return ArtifactStatus(ArtifactState.MISSING, detail="no pi session dir")
        matches = list(session_dir.glob(f"**/*_{uuid}.jsonl"))
    except OSError as exc:
        return ArtifactStatus(ArtifactState.INACCESSIBLE, detail=f"pi dir unreadable: {exc}")
    if not matches:
        return ArtifactStatus(ArtifactState.MISSING, detail="no pi session file for uuid")
    return ArtifactStatus(ArtifactState.VALID, str(matches[0]))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_artifact(
    provider: str,
    *,
    provider_session_id: Optional[str],
    provider_namespace: Optional[str] = None,
    artifact_locator: Optional[str] = None,
    cwd: Optional[str] = None,
) -> ArtifactStatus:
    """F829 D6: resolve+validate a conversation's recoverable artifact.

    A NULL ``provider_session_id`` is ``missing`` for every provider — nothing
    has been captured yet (a kiro capture_unknown root, a fresh spawn). An
    unknown provider is ``missing`` (conservative: never claim recoverable).
    """
    if not provider_session_id:
        return ArtifactStatus(ArtifactState.MISSING, detail="no provider_session_id captured")
    if provider == "claude_code":
        return _resolve_claude(provider_session_id, provider_namespace)
    if provider == "codex":
        return _resolve_codex(provider_session_id, provider_namespace, cwd)
    if provider == "kiro_cli":
        return _resolve_kiro(provider_session_id, provider_namespace)
    if provider == "pi_cli":
        return _resolve_pi(provider_session_id, provider_namespace, artifact_locator)
    return ArtifactStatus(ArtifactState.MISSING, detail=f"no resolver for provider {provider!r}")
