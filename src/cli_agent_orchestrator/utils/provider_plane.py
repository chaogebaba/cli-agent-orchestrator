"""Native provider-home isolation and crash-safe credential seeding."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from cli_agent_orchestrator.utils.sandbox_guard import SandboxProviderUnsafe

PlaneClass = Literal["production", "shared-auth-read-only", "unsafe"]
CLAUDE_SANDBOX_MARKER = "# G7 sandbox CLAUDE.md"


class NativeHomeIsolationUnavailable(RuntimeError):
    """Claude's sandbox native-home mount plane could not be proven usable."""

    code = "provider_native_home_isolation_unavailable"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{self.code}:{detail}")


@dataclass(frozen=True)
class ProviderHome:
    provider: str
    classification: PlaneClass
    home: Path
    credential_source: Path | None = None
    credential_path: Path | None = None
    home_env: str | None = None
    native_home: Path | None = None

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
        native_home=(Path(row["native_home"]) if row.get("native_home") else None),
    )


def _claude_bwrap_prefix(plane: ProviderHome, *, executable: str = "bwrap") -> list[str]:
    native_home = plane.native_home
    if plane.provider != "claude_code" or native_home is None:
        raise NativeHomeIsolationUnavailable("claude native home is not configured")
    marker = native_home / "CLAUDE.md"
    try:
        if marker.read_text(encoding="utf-8").splitlines()[:1] != [CLAUDE_SANDBOX_MARKER]:
            raise NativeHomeIsolationUnavailable("sandbox native-home marker is invalid")
    except OSError as exc:
        raise NativeHomeIsolationUnavailable(
            f"sandbox native-home marker is inaccessible: {exc}"
        ) from exc
    return [
        executable,
        "--bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--unshare-pid",
        "--bind",
        str(native_home),
        str(Path.home() / ".claude"),
        "--die-with-parent",
    ]


def _selinux_avc_detail() -> str:
    ausearch = shutil.which("ausearch")
    if ausearch is None:
        return ""
    try:
        result = subprocess.run(
            [ausearch, "-m", "AVC", "-ts", "recent"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return f"; recent SELinux AVC: {' | '.join(lines[-3:])}" if lines else ""


def preflight_claude_native_home(plane: ProviderHome) -> None:
    """Prove the exact pane-scoped bwrap mount before terminal side effects."""
    executable = shutil.which("bwrap")
    if executable is None:
        raise NativeHomeIsolationUnavailable("bwrap is not installed")
    command = [
        *_claude_bwrap_prefix(plane, executable=executable),
        "--",
        "head",
        "-1",
        str(Path.home() / ".claude" / "CLAUDE.md"),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NativeHomeIsolationUnavailable(
            f"bwrap native-home preflight could not run: {exc}{_selinux_avc_detail()}"
        ) from exc
    if result.returncode != 0 or result.stdout.rstrip("\n") != CLAUDE_SANDBOX_MARKER:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise NativeHomeIsolationUnavailable(
            f"bwrap native-home preflight failed: {detail}{_selinux_avc_detail()}"
        )


def wrap_claude_command(plane: ProviderHome, command_parts: Sequence[str]) -> list[str]:
    """Return the frozen bwrap+env argv for the Claude compound command."""
    if shutil.which("bwrap") is None:
        raise NativeHomeIsolationUnavailable("bwrap is not installed")
    try:
        return [
            *_claude_bwrap_prefix(plane),
            "--",
            "env",
            f"CLAUDE_CONFIG_DIR={plane.home}",
            *command_parts,
        ]
    except NativeHomeIsolationUnavailable:
        raise
    except Exception as exc:
        raise NativeHomeIsolationUnavailable(f"bwrap command construction failed: {exc}") from exc


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
    if provider == "claude_code":
        preflight_claude_native_home(plane)
