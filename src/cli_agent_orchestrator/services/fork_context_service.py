"""Provider-session registry, capture, and deterministic staleness helpers."""
import hashlib
import json
import os
import subprocess
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any, Literal, NoReturn, Optional
from urllib.parse import quote

from cli_agent_orchestrator.clients.database import (
    get_provider_session_by_uuid, get_ready_provider_session, get_terminal_metadata,
    list_ready_provider_sessions, list_terminals_by_provider_session_id,
    register_provider_session, retire_provider_session, update_terminal_provider_session_id,
)


class ForkContextError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class OfflineBaseRegistrationError(ValueError):
    """Stable domain rejection for the offline base registration surface."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


_FORK_ROLE_NOTICE = (
    "ROLE NOTICE: You are a newly forked worker, distinct from the base session whose "
    "transcript you inherit. Any role framing, read-only/do-not-edit constraints, or "
    "base-ready declarations inside the inherited transcript applied only to the "
    "original base. Your role, permissions, and constraints come solely from the "
    "dispatch message below."
)


def _with_role_notice(preamble: str) -> str:
    return f"{preamble}\n\n{_FORK_ROLE_NOTICE}"


def _run_git(cwd: str, *args: str) -> str:
    return subprocess.run(["git", "-C", cwd, *args], check=True, text=True,
                          capture_output=True).stdout


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(cwd: str) -> tuple[Optional[str], str]:
    try:
        sha = _run_git(cwd, "rev-parse", "HEAD").strip()
        dirty = set(_run_git(cwd, "diff", "--name-only", "HEAD", "--").splitlines())
        dirty.update(_run_git(cwd, "ls-files", "--others", "--exclude-standard").splitlines())
        hashes: dict[str, Optional[str]] = {}
        for p in sorted(dirty):
            path = Path(cwd) / p
            try:
                path.lstat()
            except FileNotFoundError:
                hashes[p] = None
            else:
                hashes[p] = _hash(path)
        return sha, json.dumps(hashes, sort_keys=True, separators=(",", ":"))
    except (OSError, subprocess.CalledProcessError):
        return None, "{}"


def staleness(row: dict[str, Any]) -> tuple[Optional[list[str]], str]:
    cwd, sha = row["cwd"], row.get("git_sha")
    if not sha:
        return None, _with_role_notice(
            "[STALE-UNKNOWN] base snapshot is not a git worktree. Revalidate inherited context."
        )
    manifest = json.loads(row.get("dirty_hashes") or "{}")
    try:
        candidates = set(_run_git(cwd, "diff", "--name-only", sha, "--").splitlines())
        candidates.update(_run_git(cwd, "ls-files", "--others", "--exclude-standard").splitlines())
        for p in manifest:
            if not (Path(cwd) / p).is_file():
                candidates.add(p)
        changed = []
        for p in sorted(candidates):
            path = Path(cwd) / p
            expected = manifest.get(p)
            try:
                path.lstat()
            except FileNotFoundError:
                absent = True
            except OSError:
                absent = False
            else:
                absent = False
            if expected is None:
                if not absent:
                    changed.append(p)
                continue
            if absent:
                changed.append(p)
                continue
            try:
                current = _hash(path)
            except OSError:
                changed.append(p)
                continue
            if current != expected:
                changed.append(p)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None, _with_role_notice(
            "[STALE-UNKNOWN] base snapshot could not be compared. Revalidate inherited context."
        )
    if not changed:
        return [], _with_role_notice(f"[FRESH] base '{row['name']}' snapshot current.")
    shown = ", ".join(changed[:50])
    return changed, _with_role_notice(
        f"[STALE] {len(changed)} files changed since base '{row['name']}' "
        f"({sha[:8]}): {shown}. Re-read these before relying on inherited context."
    )


def resolve_base(value: str) -> dict[str, Any]:
    row = get_ready_provider_session(value)
    if row:
        return _require_forkable(row)
    terminal = get_terminal_metadata(value)
    if terminal:
        uuid = terminal.get("provider_session_id")
        if not uuid:
            raise ForkContextError("base_session_unset")
        row = get_provider_session_by_uuid(uuid)
        if not row:
            raise ForkContextError("base_not_registered")
        return _require_forkable(row)
    row = get_provider_session_by_uuid(value)
    if row:
        return _require_forkable(row)
    # UUID-looking input is a registry miss; other text is an unknown name.
    import uuid as uuidlib
    try:
        uuidlib.UUID(value)
        raise ForkContextError("base_not_registered")
    except ValueError as exc:
        if isinstance(exc, ForkContextError):
            raise
        raise ForkContextError("base_name_unknown")


def _require_forkable(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("kind", "base") == "anchor":
        raise ForkContextError(f"anchor_not_forkable:{row['name']}")
    return row


def pane_pid(session: str, window: str) -> int:
    out = subprocess.run(["tmux", "display-message", "-p", "-t", f"{session}:{window}",
                          "#{pane_pid}"], check=True, capture_output=True, text=True).stdout
    return int(out.strip())


def _descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            tail = stat[stat.rfind(")") + 2:].split()
            children.setdefault(int(tail[1]), []).append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    result, queue = [], [root]
    while queue:
        p = queue.pop(0)
        result.append(p)
        queue.extend(children.get(p, []))
    return result


def pane_launch_epoch(pid: int) -> float:
    stat = Path(f"/proc/{pid}/stat").read_text()
    start_ticks = int(stat[stat.rfind(")") + 2:].split()[19])
    btime = next(int(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines()
                 if x.startswith("btime "))
    return btime + start_ticks / os.sysconf("SC_CLK_TCK")


def capture_codex_uuid(root_pid: int, launch_time: float, cwd: str) -> str:
    for attempt in range(3):
        candidates: set[Path] = set()
        try:
            for pid in _descendants(root_pid):
                for fd in Path(f"/proc/{pid}/fd").iterdir():
                    try:
                        p = Path(os.readlink(fd)).resolve()
                        if "/.codex/sessions/" in str(p) and p.name.startswith("rollout-") and p.suffix == ".jsonl":
                            candidates.add(p)
                    except OSError:
                        pass
            if len(candidates) == 1:
                p = candidates.pop()
                first = json.loads(p.open().readline())
                sid = first["payload"]["id"]
                if first["type"] == "session_meta" and sid in p.name:
                    return sid
                raise ForkContextError("session_capture_mismatch")
        except OSError:
            pass
        if attempt < 2:
            time.sleep(1)
    matches = []
    now = time.time()
    for p in (Path.home() / ".codex" / "sessions").glob("**/rollout-*.jsonl"):
        try:
            meta = json.loads(p.open().readline())["payload"]
            if meta.get("cwd") == cwd and launch_time <= p.stat().st_mtime <= now:
                matches.append((p, meta["id"]))
        except (OSError, KeyError, json.JSONDecodeError):
            pass
    if len(matches) != 1:
        raise ForkContextError("session_capture_ambiguous")
    p, sid = matches[0]
    if sid not in p.name:
        raise ForkContextError("session_capture_mismatch")
    return sid


def _registration_error(code: str, message: str) -> NoReturn:
    raise OfflineBaseRegistrationError(code, message)


def _grok_artifact_mismatch(session_uuid: str, cwd: str) -> str | None:
    """Classify a missing expected Grok artifact against artifacts in other cwd roots."""
    root = Path.home() / ".grok" / "sessions"
    expected = root / quote(cwd, safe="") / session_uuid / "chat_history.jsonl"
    matches = [
        path
        for path in root.glob(f"*/{session_uuid}/chat_history.jsonl")
        if path.is_file() and path.stat().st_size > 0
    ]
    if expected in matches:
        return None
    if len(matches) > 1:
        return "artifact_ambiguous"
    if matches:
        return "artifact_cwd_mismatch"
    return None


def validate_base_source(
    *,
    mode: Literal["registration", "compatibility"],
    provider: str,
    session_uuid: str,
    cwd: str,
    name: str | None = None,
    agent_profile: str | None = None,
) -> dict[str, str]:
    """Validate stored provider history under an explicit strict or legacy mode.

    ``compatibility`` intentionally preserves assign's historical loose artifact
    predicate. ``registration`` is the operator-authority boundary and validates
    every fact before the caller performs the superseding registry transaction.
    """
    if mode == "compatibility":
        if provider == "codex":
            found = any(
                session_uuid in path.name
                for path in (Path.home() / ".codex" / "sessions").glob(
                    "**/rollout-*.jsonl"
                )
            )
        else:
            found = (
                Path.home()
                / ".grok"
                / "sessions"
                / quote(cwd, safe="")
                / session_uuid
            ).exists()
        if not found:
            raise ForkContextError("session_file_missing")
        return {}

    if mode != "registration":
        raise ValueError(f"unknown validation mode: {mode}")
    if name == "cold":
        _registration_error("name_reserved", "base name 'cold' is reserved")
    if not name:
        _registration_error("name_reserved", "base name must be non-empty")

    from cli_agent_orchestrator.providers.manager import get_provider_class

    try:
        provider_class = get_provider_class(provider)
    except ValueError:
        _registration_error("provider_unknown", f"unknown provider: {provider}")
    if not provider_class.supports_fork_context:
        _registration_error(
            "fork_unsupported", f"provider does not support fork context: {provider}"
        )

    if not cwd or not Path(cwd).is_absolute():
        _registration_error("cwd_not_absolute", "cwd must be an absolute path")
    canonical_cwd = str(Path(cwd).resolve())
    try:
        uuidlib.UUID(session_uuid)
    except (ValueError, AttributeError, TypeError):
        _registration_error("uuid_malformed", "session_uuid must be a valid UUID")

    from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

    try:
        profile = load_agent_profile(agent_profile or "")
    except (FileNotFoundError, ValueError):
        _registration_error(
            "profile_unknown", f"unknown agent profile: {agent_profile or ''}"
        )
    if profile.provider and profile.provider != provider:
        _registration_error(
            "profile_provider_mismatch",
            f"profile {agent_profile} uses provider {profile.provider}, not {provider}",
        )

    from cli_agent_orchestrator.providers.base import (
        RetryableArtifactValidation,
        TerminalArtifactValidation,
    )

    from cli_agent_orchestrator.providers.manager import provider_manager

    validator = provider_manager.construct_provider(
        provider,
        "offline-base",
        "offline-base",
        "offline-base",
        agent_profile=agent_profile,
    )
    try:
        validator.validate_session_artifact(session_uuid, canonical_cwd)
    except RetryableArtifactValidation:
        if provider == "grok_cli":
            mismatch = _grok_artifact_mismatch(session_uuid, canonical_cwd)
            if mismatch == "artifact_ambiguous":
                _registration_error(
                    "artifact_ambiguous", "multiple provider artifacts match the UUID"
                )
            if mismatch == "artifact_cwd_mismatch":
                _registration_error(
                    "artifact_cwd_mismatch", "provider artifact belongs to a different cwd"
                )
        _registration_error("artifact_not_found", "provider artifact was not found")
    except TerminalArtifactValidation as exc:
        if "ambiguous" in exc.code:
            _registration_error(
                "artifact_ambiguous", "multiple provider artifacts match the UUID"
            )
        _registration_error(
            "artifact_identity_mismatch", "provider artifact identity does not match the UUID"
        )
    except (OSError, KeyError, json.JSONDecodeError):
        _registration_error(
            "artifact_identity_mismatch", "provider artifact identity could not be validated"
        )

    if provider == "codex":
        matches = list(
            (Path.home() / ".codex" / "sessions").glob(
                f"**/rollout-*{session_uuid}*.jsonl"
            )
        )
        try:
            with matches[0].open(encoding="utf-8") as stream:
                payload_cwd = json.loads(stream.readline()).get("payload", {}).get("cwd")
        except (IndexError, OSError, json.JSONDecodeError):
            _registration_error(
                "artifact_identity_mismatch", "provider artifact metadata is invalid"
            )
        if not isinstance(payload_cwd, str) or str(Path(payload_cwd).resolve()) != canonical_cwd:
            _registration_error(
                "artifact_cwd_mismatch", "provider artifact belongs to a different cwd"
            )

    inside_worktree = False
    try:
        inside_worktree = _run_git(
            canonical_cwd, "rev-parse", "--is-inside-work-tree"
        ).strip() == "true"
    except (OSError, subprocess.CalledProcessError):
        pass
    git_sha, dirty_hashes = snapshot(canonical_cwd)
    if not inside_worktree or not git_sha:
        _registration_error(
            "cwd_not_git_worktree", "cwd must be a git worktree with a resolvable HEAD"
        )
    return {
        "cwd": canonical_cwd,
        "git_sha": git_sha,
        "dirty_hashes": dirty_hashes,
    }


def register_offline_base(
    *,
    name: str,
    provider: str,
    session_uuid: str,
    cwd: str,
    agent_profile: str,
    summary: str | None = None,
) -> dict[str, Any]:
    """Register a global base from validated provider history, without a live terminal."""
    validated = validate_base_source(
        mode="registration",
        name=name,
        provider=provider,
        session_uuid=session_uuid,
        cwd=cwd,
        agent_profile=agent_profile,
    )
    row = register_provider_session(
        name=name,
        provider=provider,
        session_uuid=session_uuid,
        cwd=validated["cwd"],
        agent_profile=agent_profile,
        git_sha=validated["git_sha"],
        dirty_hashes=validated["dirty_hashes"],
        summary=summary,
        kind="base",
        source_terminal_id=None,
        session_name=None,
        include_superseded=True,
    )
    return {
        "name": row["name"],
        "provider": row["provider"],
        "profile": row["agent_profile"],
        "cwd": row["cwd"],
        "session_uuid": row["session_uuid"],
        "kind": row["kind"],
        "session_name": row["session_name"],
        "source_terminal_id": row["source_terminal_id"],
        "git_sha": row["git_sha"],
        "dirty_hashes": row["dirty_hashes"],
        "superseded": row["superseded"],
    }


def mark_ready(
    terminal_id: str,
    name: str,
    summary: Optional[str],
    kind: str = "base",
) -> dict[str, Any]:
    if name == "cold":
        raise ForkContextError("base_name_reserved:cold")
    if kind not in {"base", "anchor"}:
        raise ForkContextError("invalid_provider_session_kind")
    terminal = get_terminal_metadata(terminal_id)
    if not terminal:
        raise ForkContextError("terminal_not_found")
    cwd = terminal.get("working_directory") or terminal.get("cwd")
    if not cwd:
        from cli_agent_orchestrator.backends.registry import get_backend
        cwd = get_backend().get_pane_working_directory(terminal["tmux_session"], terminal["tmux_window"])
    provider = terminal["provider"]
    if provider == "codex":
        pid = pane_pid(terminal["tmux_session"], terminal["tmux_window"])
        session_uuid = capture_codex_uuid(pid, pane_launch_epoch(pid), cwd)
    elif provider == "grok_cli":
        session_uuid = terminal.get("provider_session_id")
        if not session_uuid:
            raise ForkContextError("base_session_unset")
    else:
        raise ForkContextError("provider_lacks_fork_capability")
    sha, hashes = snapshot(cwd)
    row = register_provider_session(name=name, provider=provider, session_uuid=session_uuid,
                                    cwd=cwd, agent_profile=terminal["agent_profile"], git_sha=sha,
                                    dirty_hashes=hashes, summary=summary,
                                    kind=kind,
                                    source_terminal_id=terminal_id,
                                    session_name=terminal["tmux_session"])
    update_terminal_provider_session_id(terminal_id, session_uuid)
    return row


def list_bases() -> list[dict[str, Any]]:
    result = []
    for row in list_ready_provider_sessions():
        if row.get("kind", "base") != "base":
            continue
        changed, _ = staleness(row)
        row["staleness_count"] = None if changed is None else len(changed)
        result.append(row)
    return result


def retire(name: str) -> Optional[dict[str, Any]]:
    """Retire the current ready base registration without touching its terminal."""
    return retire_provider_session(name)
