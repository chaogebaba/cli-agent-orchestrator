"""Native provider-home isolation and crash-safe credential seeding."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cli_agent_orchestrator.utils.sandbox_guard import SandboxProviderUnsafe

PlaneClass = Literal["production", "shared-auth-read-only", "unsafe"]


@dataclass(frozen=True)
class ProviderHome:
    provider: str
    classification: PlaneClass
    home: Path
    credential_source: Path | None = None
    credential_path: Path | None = None
    home_env: str | None = None

    @property
    def sessions(self) -> Path:
        return self.home / "sessions"

    @property
    def projects(self) -> Path:
        return self.home / "projects"


def _production_home(provider: str) -> Path:
    if provider == "codex":
        return Path.home() / ".codex"
    if provider == "claude_code":
        return Path.home() / ".claude"
    raise ValueError(f"provider has no native-home object: {provider}")


def provider_home(provider: str) -> ProviderHome:
    """Resolve the single injected home object for a supported native provider."""
    instance_id = os.environ.get("CAO_INSTANCE_ID", "").strip()
    if not instance_id:
        return ProviderHome(provider, "production", _production_home(provider))

    from cli_agent_orchestrator.sandbox_bootstrap import validate_active_sandbox

    manifest = validate_active_sandbox()
    if manifest is None:
        raise SandboxProviderUnsafe(f"sandbox_provider_unsafe:{provider}")
    row = manifest["providers"].get(provider)
    if not isinstance(row, dict) or row.get("classification") != "shared-auth-read-only":
        raise SandboxProviderUnsafe(f"sandbox_provider_unsafe:{provider}")
    home = Path(row["home"])
    home_env = str(row["home_env"])
    if os.environ.get(home_env) != str(home):
        raise SandboxProviderUnsafe(f"sandbox_provider_home_mismatch:{provider}")
    return ProviderHome(
        provider=provider,
        classification="shared-auth-read-only",
        home=home,
        credential_source=Path(row["credential_source"]),
        credential_path=Path(row["credential_path"]),
        home_env=home_env,
    )


def provider_plane_environment() -> dict[str, str]:
    if not os.environ.get("CAO_INSTANCE_ID", "").strip():
        return {}
    result: dict[str, str] = {}
    for provider in ("codex", "claude_code"):
        plane = provider_home(provider)
        if plane.home_env is not None:
            result[plane.home_env] = str(plane.home)
    return result


def _process_start_time(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(text[text.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _record_attempt(home: Path, attempt_id: str, reason: str, status: str) -> None:
    record = home / "seed-attempts.jsonl"
    payload = json.dumps(
        {
            "attempt_id": attempt_id,
            "provider": home.name,
            "reason": reason,
            "status": status,
            "pid": os.getpid(),
            "process_start_time": _process_start_time(os.getpid()),
            "timestamp_ns": time.time_ns(),
        },
        sort_keys=True,
    )
    fd = os.open(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, payload.encode("utf-8") + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)


def _credential_is_valid(path: Path) -> bool:
    try:
        if path.stat().st_size <= 0:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and bool(value)


def _lock_owner_is_live(lock_path: Path) -> tuple[bool, str | None]:
    try:
        stamp = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(stamp["pid"])
        start = int(stamp["process_start_time"])
        temp_name = str(stamp["temp_name"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False, None
    return _process_start_time(pid) == start, temp_name


def seed_provider_credential(
    plane: ProviderHome, *, deadline_s: float = 5.0, poll_s: float = 0.02
) -> None:
    """Install exactly one mutable credential copy without overwriting refreshes."""
    if plane.classification != "shared-auth-read-only":
        return
    source = plane.credential_source
    destination = plane.credential_path
    if source is None or destination is None:
        raise SandboxProviderUnsafe(f"sandbox_provider_plane_invalid:{plane.provider}")
    plane.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists():
        if not _credential_is_valid(destination):
            raise SandboxProviderUnsafe(f"sandbox_provider_credential_invalid:{plane.provider}")
        return

    lock_path = destination.with_name(f".{destination.name}.init.lock")
    deadline = time.monotonic() + deadline_s
    reason = "initial_seed"
    while True:
        if destination.exists():
            if not _credential_is_valid(destination):
                raise SandboxProviderUnsafe(f"sandbox_provider_credential_invalid:{plane.provider}")
            return
        attempt_id = uuid.uuid4().hex
        temp_name = f".{destination.name}.{attempt_id}.tmp"
        stamp = json.dumps(
            {
                "pid": os.getpid(),
                "process_start_time": _process_start_time(os.getpid()),
                "temp_name": temp_name,
            },
            sort_keys=True,
        ).encode("utf-8")
        try:
            lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if destination.exists():
                if not _credential_is_valid(destination):
                    raise SandboxProviderUnsafe(
                        f"sandbox_provider_credential_invalid:{plane.provider}"
                    )
                return
            if time.monotonic() < deadline:
                time.sleep(poll_s)
                continue
            live, orphan_temp = _lock_owner_is_live(lock_path)
            if live:
                raise SandboxProviderUnsafe(f"sandbox_provider_seed_timeout:{plane.provider}")
            if orphan_temp:
                (plane.home / orphan_temp).unlink(missing_ok=True)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            reason = "dead_owner_recovery"
            deadline = time.monotonic() + deadline_s
            continue

        temp = plane.home / temp_name
        try:
            os.write(lock_fd, stamp)
            os.fsync(lock_fd)
            os.close(lock_fd)
            lock_fd = -1
            _record_attempt(plane.home, attempt_id, reason, "started")
            source_fd = os.open(source, os.O_RDONLY)
            try:
                target_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    while chunk := os.read(source_fd, 65536):
                        os.write(target_fd, chunk)
                    os.fsync(target_fd)
                finally:
                    os.close(target_fd)
            finally:
                os.close(source_fd)
            if not _credential_is_valid(temp):
                raise SandboxProviderUnsafe(f"sandbox_provider_credential_invalid:{plane.provider}")
            os.link(temp, destination)
            destination.chmod(0o600)
            temp.unlink()
            directory_fd = os.open(plane.home, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            _record_attempt(plane.home, attempt_id, reason, "installed")
            return
        except FileExistsError:
            if not _credential_is_valid(destination):
                raise SandboxProviderUnsafe(f"sandbox_provider_credential_invalid:{plane.provider}")
            return
        except Exception:
            _record_attempt(plane.home, attempt_id, reason, "failed")
            raise
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)
            temp.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)


def admit_provider(provider: str) -> None:
    if not os.environ.get("CAO_INSTANCE_ID", "").strip():
        return
    if provider not in {"codex", "claude_code"}:
        raise SandboxProviderUnsafe(f"sandbox_provider_unsafe:{provider}")
    plane = provider_home(provider)
    seed_provider_credential(plane)
