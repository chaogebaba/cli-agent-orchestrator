"""Provider-session registry, capture, and deterministic staleness helpers."""
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.clients.database import (
    get_provider_session_by_uuid, get_ready_provider_session, get_terminal_metadata,
    list_ready_provider_sessions, list_terminals_by_provider_session_id,
    register_provider_session, retire_provider_session, update_terminal_provider_session_id,
)


class ForkContextError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


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
        return None, "[STALE-UNKNOWN] base snapshot is not a git worktree. Revalidate inherited context."
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
        return None, "[STALE-UNKNOWN] base snapshot could not be compared. Revalidate inherited context."
    if not changed:
        return [], f"[FRESH] base '{row['name']}' snapshot current."
    shown = ", ".join(changed[:50])
    return changed, (f"[STALE] {len(changed)} files changed since base '{row['name']}' "
                     f"({sha[:8]}): {shown}. Re-read these before relying on inherited context.")


def resolve_base(value: str) -> dict[str, Any]:
    row = get_ready_provider_session(value)
    if row:
        return row
    terminal = get_terminal_metadata(value)
    if terminal:
        uuid = terminal.get("provider_session_id")
        if not uuid:
            raise ForkContextError("base_session_unset")
        row = get_provider_session_by_uuid(uuid)
        if not row:
            raise ForkContextError("base_not_registered")
        return row
    row = get_provider_session_by_uuid(value)
    if row:
        return row
    # UUID-looking input is a registry miss; other text is an unknown name.
    import uuid as uuidlib
    try:
        uuidlib.UUID(value)
        raise ForkContextError("base_not_registered")
    except ValueError as exc:
        if isinstance(exc, ForkContextError):
            raise
        raise ForkContextError("base_name_unknown")


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


def mark_ready(terminal_id: str, name: str, summary: Optional[str]) -> dict[str, Any]:
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
                                    source_terminal_id=terminal_id)
    update_terminal_provider_session_id(terminal_id, session_uuid)
    return row


def list_bases() -> list[dict[str, Any]]:
    result = []
    for row in list_ready_provider_sessions():
        changed, _ = staleness(row)
        row["staleness_count"] = None if changed is None else len(changed)
        result.append(row)
    return result


def retire(name: str) -> Optional[dict[str, Any]]:
    """Retire the current ready base registration without touching its terminal."""
    return retire_provider_session(name)
