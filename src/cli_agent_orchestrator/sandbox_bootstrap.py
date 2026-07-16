"""Import-clean G7 sandbox bootstrap and lifecycle authority.

This module is intentionally stdlib-only and must not import any other CAO
module before the manifest and environment fences have been validated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

MANIFEST_NAME = "instance-manifest.toml"
OWNER_LOCK_NAME = "owner.lock"
PRODUCTION_PORT = int("98" "89")
PRODUCTION_ROOT = Path.home() / ".aws" / "cli-agent-orchestrator"
PROVIDER_NATIVE_HOMES = (
    Path.home() / ".codex",
    Path.home() / ".claude",
    Path.home() / ".grok",
    Path.home() / ".kimi",
    Path.home() / ".gemini",
    Path.home() / ".kiro",
    Path.home() / ".copilot",
    Path.home() / ".aws" / "opencode",
)
MUTABLE_PATHS = {
    "db_path": "db/cli-agent-orchestrator.db",
    "settings_path": "settings.json",
    "providers_path": "providers.toml",
    "env_path": ".env",
    "logs_dir": "logs",
    "snapshots_dir": "logs/terminal",
    "fifos_dir": "fifos",
    "memory_dir": "memory",
    "workflows_dir": "workflows",
    "scratch_dir": "scratch",
    "graph_exports_dir": "graph-exports",
    "pidfile": "sandbox.pid",
}
PROVIDERS = (
    "kiro_cli",
    "grok_cli",
    "claude_code",
    "codex",
    "kimi_cli",
    "copilot_cli",
    "opencode_cli",
    "hermes",
    "cursor_cli",
    "antigravity_cli",
)
SHARED_AUTH_PROVIDERS = {
    "codex": {
        "home_relative": "provider-homes/codex",
        "credential_source": Path.home() / ".codex" / "auth.json",
        "credential_name": "auth.json",
        "home_env": "CODEX_HOME",
    },
    "claude_code": {
        "home_relative": "provider-homes/claude",
        "credential_source": Path.home() / ".claude" / ".credentials.json",
        "credential_name": ".credentials.json",
        "home_env": "CLAUDE_CONFIG_DIR",
    },
}


class SandboxError(RuntimeError):
    """A sandbox fence or lifecycle operation failed closed."""


def _canonical(path: Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(path)))


def _related(left: Path, right: Path) -> bool:
    left = _canonical(left)
    right = _canonical(right)
    return left == right or left in right.parents or right in left.parents


def _process_start_time(pid: int) -> int:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(text[text.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError) as exc:
        raise SandboxError(f"cannot identify process {pid}") from exc


def _assert_clean_components(path: Path, *, terminal_symlink: bool = False) -> None:
    absolute = Path(os.path.abspath(path))
    parts = absolute.parts
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], start=1):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        final = index == len(parts) - 1
        if stat.S_ISLNK(info.st_mode):
            if not (terminal_symlink and final):
                raise SandboxError(f"symlink component forbidden: {current}")
        elif final and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise SandboxError(f"hard-linked file forbidden: {current}")


def _git(fork_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(fork_root), *args],
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    return result.stdout


def source_identity(fork_root: Path) -> dict[str, Any]:
    """Compute the frozen source identity without writing to the fork."""
    fork_root = _canonical(fork_root)
    candidates = _git(
        fork_root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        "src",
        "pyproject.toml",
        "uv.lock",
    ).splitlines()
    selected = sorted(
        path
        for path in candidates
        if path in {"pyproject.toml", "uv.lock"} or path.startswith("src/")
    )
    digest = hashlib.sha256()
    for relative in selected:
        file_path = fork_root / relative
        if not file_path.is_file():
            continue
        content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\n")
    interpreter_path = fork_root / ".venv" / "bin" / "python"
    venv_prefix = _canonical(Path(sys.prefix))
    return {
        "fork_root": str(fork_root),
        "interpreter_identity": {
            "interpreter_path": str(interpreter_path),
            "venv_prefix": str(venv_prefix),
            "base_interpreter_realpath": str(_canonical(interpreter_path)),
        },
        "commit_sha": _git(fork_root, "rev-parse", "HEAD").strip(),
        "source_merkle": digest.hexdigest(),
        "dirty": bool(_git(fork_root, "status", "--porcelain", "--untracked-files=all")),
    }


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_manifest(manifest: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in (
        "instance_id",
        "created_at",
        "root",
        *MUTABLE_PATHS,
        "endpoint",
        "tmux_socket",
        "owner_nonce",
    ):
        lines.append(f"{key} = {_toml_string(str(manifest[key]))}\n")
    lines.append(f"root_device = {int(manifest['root_device'])}\n")
    lines.append(f"root_inode = {int(manifest['root_inode'])}\n")
    source = manifest["source"]
    lines.append("\n[source]\n")
    for key in ("fork_root", "commit_sha", "source_merkle"):
        lines.append(f"{key} = {_toml_string(str(source[key]))}\n")
    lines.append(f"dirty = {'true' if source['dirty'] else 'false'}\n")
    lines.append("\n[source.interpreter_identity]\n")
    identity = source["interpreter_identity"]
    for key in ("interpreter_path", "venv_prefix", "base_interpreter_realpath"):
        lines.append(f"{key} = {_toml_string(str(identity[key]))}\n")
    for provider in PROVIDERS:
        lines.append(f"\n[providers.{provider}]\n")
        row = manifest["providers"][provider]
        for key, value in row.items():
            lines.append(f"{key} = {_toml_string(str(value))}\n")
    return "".join(lines)


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SandboxError(f"invalid sandbox manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise SandboxError("sandbox manifest must be a table")
    return value


def _validate_endpoint(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise SandboxError("manifest endpoint must be a string")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SandboxError("manifest endpoint has invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or port == PRODUCTION_PORT
    ):
        raise SandboxError("manifest endpoint is not an isolated loopback origin")
    return value.rstrip("/"), port


def validate_manifest(manifest: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    """Validate the closed manifest schema without creating anything."""
    required = {
        "instance_id",
        "created_at",
        "root",
        *MUTABLE_PATHS,
        "endpoint",
        "tmux_socket",
        "owner_nonce",
        "root_device",
        "root_inode",
        "source",
        "providers",
    }
    missing = required - manifest.keys()
    if missing:
        raise SandboxError(f"manifest missing fields: {sorted(missing)}")
    extra = manifest.keys() - required
    if extra:
        raise SandboxError(f"manifest has unknown fields: {sorted(extra)}")
    instance_id = manifest["instance_id"]
    if not isinstance(instance_id, str) or not instance_id.isalnum() or len(instance_id) != 8:
        raise SandboxError("invalid sandbox instance_id")
    root = _canonical(Path(str(manifest["root"])))
    if manifest_path != root / MANIFEST_NAME:
        raise SandboxError("manifest path does not match its root")
    _assert_clean_components(manifest_path)
    _assert_clean_components(root)
    if not root.is_dir():
        raise SandboxError("sandbox root is absent")
    root_stat = root.stat()
    if root_stat.st_dev != int(manifest["root_device"]) or root_stat.st_ino != int(
        manifest["root_inode"]
    ):
        raise SandboxError("sandbox root inode changed")
    forbidden = (PRODUCTION_ROOT, *PROVIDER_NATIVE_HOMES)
    if any(_related(root, item) for item in forbidden):
        raise SandboxError("sandbox root aliases a production path")
    for field, relative in MUTABLE_PATHS.items():
        candidate = Path(str(manifest[field]))
        expected = root / relative
        if candidate != expected:
            raise SandboxError(f"manifest {field} is outside the closed schema")
        _assert_clean_components(candidate)
        if any(_related(candidate, item) for item in forbidden):
            raise SandboxError(f"manifest {field} aliases production")
    endpoint, _ = _validate_endpoint(manifest["endpoint"])
    socket_name = manifest["tmux_socket"]
    if socket_name != f"cao-sbx-{instance_id}":
        raise SandboxError("tmux socket is not instance-bound")
    source = manifest["source"]
    if not isinstance(source, dict) or set(source) != {
        "fork_root",
        "interpreter_identity",
        "commit_sha",
        "source_merkle",
        "dirty",
    }:
        raise SandboxError("manifest source must be a table")
    fork_root = _canonical(Path(str(source.get("fork_root", ""))))
    identity = source.get("interpreter_identity")
    if not isinstance(identity, dict) or set(identity) != {
        "interpreter_path",
        "venv_prefix",
        "base_interpreter_realpath",
    }:
        raise SandboxError("manifest interpreter_identity must be a table")
    interpreter_path = Path(str(identity.get("interpreter_path", "")))
    venv_prefix = _canonical(Path(str(identity.get("venv_prefix", ""))))
    base_realpath = _canonical(Path(str(identity.get("base_interpreter_realpath", ""))))
    if interpreter_path != fork_root / ".venv" / "bin" / "python":
        raise SandboxError("interpreter_path is not the fork venv entry")
    if venv_prefix != fork_root / ".venv":
        raise SandboxError("venv_prefix is not the fork venv")
    _assert_clean_components(interpreter_path, terminal_symlink=True)
    if not interpreter_path.is_symlink():
        raise SandboxError("interpreter_path must be the pinned terminal symlink")
    if _canonical(interpreter_path) != base_realpath:
        raise SandboxError("interpreter symlink target does not match manifest")
    try:
        if interpreter_path.stat().st_ino != base_realpath.stat().st_ino:
            raise SandboxError("interpreter identity inode mismatch")
    except OSError as exc:
        raise SandboxError("interpreter identity is inaccessible") from exc
    providers = manifest["providers"]
    if not isinstance(providers, dict) or set(providers) != set(PROVIDERS):
        raise SandboxError("provider plane table is not the closed ten-provider set")
    for provider, row in providers.items():
        if provider not in SHARED_AUTH_PROVIDERS:
            if not isinstance(row, dict) or row != {"classification": "unsafe"}:
                raise SandboxError(f"unsafe provider plane is invalid: {provider}")
            continue
        pin = SHARED_AUTH_PROVIDERS[provider]
        expected_home = root / str(pin["home_relative"])
        source_pin = pin["credential_source"]
        if not isinstance(source_pin, Path):
            raise SandboxError(f"credential source pin is invalid: {provider}")
        expected_source = source_pin
        expected_credential = expected_home / str(pin["credential_name"])
        expected_row = {
            "classification": "shared-auth-read-only",
            "home": str(expected_home),
            "home_env": str(pin["home_env"]),
            "credential_source": str(expected_source),
            "credential_path": str(expected_credential),
        }
        if not isinstance(row, dict) or row != expected_row:
            raise SandboxError(f"shared-auth provider plane is invalid: {provider}")
        _assert_clean_components(expected_home)
        _assert_clean_components(expected_credential)
        if not expected_home.is_relative_to(root):
            raise SandboxError(f"provider home is outside sandbox root: {provider}")
    result = dict(manifest)
    result["root"] = str(root)
    result["endpoint"] = endpoint
    return result


def _manifest_env(manifest: dict[str, Any], manifest_path: Path) -> dict[str, str]:
    result = {
        "CAO_HOME": str(manifest["root"]),
        "CAO_ENDPOINT": str(manifest["endpoint"]),
        "CAO_INSTANCE_ID": str(manifest["instance_id"]),
        "CAO_TMUX_SOCKET": str(manifest["tmux_socket"]),
        "CAO_TMP_DIR": str(manifest["scratch_dir"]),
        "TMPDIR": str(manifest["scratch_dir"]),
        "CAO_GRAPH_EXPORT_ROOT": str(manifest["graph_exports_dir"]),
        "CAO_SANDBOX_MANIFEST": str(manifest_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for row in manifest["providers"].values():
        if row.get("classification") == "shared-auth-read-only":
            result[str(row["home_env"])] = str(row["home"])
    return result


def _validate_env(manifest: dict[str, Any], manifest_path: Path) -> None:
    expected = _manifest_env(manifest, manifest_path)
    for key, value in expected.items():
        if os.environ.get(key) != value:
            raise SandboxError(f"sandbox environment mismatch: {key}")


def validate_active_sandbox() -> dict[str, Any] | None:
    """Reopen and validate the active manifest; production is a no-op."""
    instance_id = os.environ.get("CAO_INSTANCE_ID", "").strip()
    if not instance_id:
        return None
    raw_path = os.environ.get("CAO_SANDBOX_MANIFEST", "")
    if not raw_path:
        raise SandboxError("CAO_SANDBOX_MANIFEST is required")
    manifest_path = Path(raw_path)
    manifest = validate_manifest(read_manifest(manifest_path), manifest_path)
    _validate_env(manifest, manifest_path)
    if manifest["instance_id"] != instance_id:
        raise SandboxError("active instance does not match manifest")
    return manifest


def _tmux_lifecycle(
    socket_name: str, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """The only import-clean lifecycle wrapper allowed to execute tmux directly."""
    return subprocess.run(
        ["tmux", "-L", socket_name, *args],
        check=check,
        text=True,
        capture_output=True,
    )


def _seed_sandbox_db(manifest: dict[str, Any]) -> None:
    db_path = Path(manifest["db_path"])
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE sandbox_metadata (instance_id TEXT PRIMARY KEY, owner_nonce TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sandbox_metadata (instance_id, owner_nonce) VALUES (?, ?)",
            (manifest["instance_id"], manifest["owner_nonce"]),
        )
    db_path.chmod(0o600)


def assert_sandbox_db_fence(manifest: dict[str, Any]) -> None:
    try:
        with sqlite3.connect(f"file:{manifest['db_path']}?mode=rw", uri=True) as connection:
            row = connection.execute(
                "SELECT instance_id, owner_nonce FROM sandbox_metadata"
            ).fetchone()
    except sqlite3.Error as exc:
        raise SandboxError("sandbox DB has no valid ownership stamp") from exc
    if row != (manifest["instance_id"], manifest["owner_nonce"]):
        raise SandboxError("sandbox DB ownership stamp mismatch")


def _write_once(path: Path, content: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _health(manifest: dict[str, Any], timeout: float = 1.0) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{manifest['endpoint']}/health",
        headers={"X-CAO-Instance": str(manifest["instance_id"])},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise SandboxError("sandbox health response is malformed")
    return payload


def _wait_ready(manifest: dict[str, Any], child: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise SandboxError(f"sandbox server exited before readiness ({child.returncode})")
        try:
            payload = _health(manifest)
        except (OSError, urllib.error.URLError, ValueError, SandboxError):
            time.sleep(0.1)
            continue
        source = manifest["source"]
        if (
            payload.get("instance_id") == manifest["instance_id"]
            and payload.get("source", {}).get("source_merkle") == source["source_merkle"]
            and payload.get("source", {}).get("module_contained") is True
            and payload.get("source", {}).get("interpreter_match") is True
        ):
            return
        raise SandboxError("sandbox readiness identity mismatch")
    raise SandboxError("sandbox server readiness timed out")


def _build_manifest(root: Path, port: int) -> dict[str, Any]:
    root = _canonical(root)
    if root.exists():
        raise SandboxError("sandbox root already exists")
    if any(_related(root, item) for item in (PRODUCTION_ROOT, *PROVIDER_NATIVE_HOMES)):
        raise SandboxError("sandbox root overlaps production")
    _assert_clean_components(root.parent)
    if port == PRODUCTION_PORT or not 1 <= port <= 65535:
        raise SandboxError("sandbox port is invalid or production-owned")
    fork_root = _canonical(Path(__file__).parents[2])
    expected_interpreter = fork_root / ".venv" / "bin" / "python"
    current_interpreter = Path(os.path.abspath(sys.executable))
    if _canonical(Path(sys.prefix)) != fork_root / ".venv" or _canonical(
        current_interpreter
    ) != _canonical(expected_interpreter):
        raise SandboxError("bootstrap must run from the fork's absolute venv interpreter")
    root.mkdir(mode=0o700)
    try:
        root_stat = root.stat()
        instance_id = uuid.uuid4().hex[:8]
        manifest: dict[str, Any] = {
            "instance_id": instance_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "root": str(root),
            "endpoint": f"http://127.0.0.1:{port}",
            "tmux_socket": f"cao-sbx-{instance_id}",
            "owner_nonce": secrets.token_hex(32),
            "root_device": root_stat.st_dev,
            "root_inode": root_stat.st_ino,
            "source": source_identity(fork_root),
            "providers": {},
        }
        for provider in PROVIDERS:
            pin = SHARED_AUTH_PROVIDERS.get(provider)
            if pin is None:
                manifest["providers"][provider] = {"classification": "unsafe"}
                continue
            home = root / str(pin["home_relative"])
            manifest["providers"][provider] = {
                "classification": "shared-auth-read-only",
                "home": str(home),
                "home_env": str(pin["home_env"]),
                "credential_source": str(pin["credential_source"]),
                "credential_path": str(home / str(pin["credential_name"])),
            }
        for field, relative in MUTABLE_PATHS.items():
            manifest[field] = str(root / relative)
        return manifest
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def command_up(args: argparse.Namespace) -> int:
    root = Path(args.root)
    manifest: dict[str, Any] | None = None
    sentinel_created = False
    child: subprocess.Popen[bytes] | None = None
    try:
        manifest = _build_manifest(root, args.port)
        manifest_path = Path(manifest["root"]) / MANIFEST_NAME
        validate_manifest(manifest, manifest_path)
        _write_once(manifest_path, render_manifest(manifest), 0o400)
        _write_once(
            Path(manifest["root"]) / OWNER_LOCK_NAME,
            json.dumps(
                {
                    "owner_nonce": manifest["owner_nonce"],
                    "root_device": manifest["root_device"],
                    "root_inode": manifest["root_inode"],
                },
                sort_keys=True,
            ),
            0o400,
        )
        for field in (
            "logs_dir",
            "snapshots_dir",
            "fifos_dir",
            "memory_dir",
            "workflows_dir",
            "scratch_dir",
            "graph_exports_dir",
        ):
            Path(manifest[field]).mkdir(mode=0o700, parents=True, exist_ok=True)
        _seed_sandbox_db(manifest)
        sentinel = f"cao-sbx-{manifest['instance_id']}-owner"
        existing = _tmux_lifecycle(
            str(manifest["tmux_socket"]),
            "list-sessions",
            "-F",
            "#{session_name}",
            check=False,
        )
        if existing.returncode == 0:
            raise SandboxError("sandbox tmux socket is already live")
        _tmux_lifecycle(
            str(manifest["tmux_socket"]),
            "new-session",
            "-d",
            "-s",
            sentinel,
            "-n",
            "owner",
        )
        claimed = _tmux_lifecycle(
            str(manifest["tmux_socket"]),
            "list-sessions",
            "-F",
            "#{session_name}",
        )
        if claimed.stdout.splitlines() != [sentinel]:
            _tmux_lifecycle(
                str(manifest["tmux_socket"]),
                "kill-session",
                "-t",
                sentinel,
                check=False,
            )
            raise SandboxError("sandbox tmux socket ownership collision")
        sentinel_created = True
        env = {**os.environ, **_manifest_env(manifest, manifest_path)}
        command = [
            str(manifest["source"]["interpreter_identity"]["interpreter_path"]),
            "-B",
            "-m",
            "cli_agent_orchestrator.sandbox_bootstrap",
            "serve",
            "--manifest",
            str(manifest_path),
        ]
        log_fd = os.open(
            Path(manifest["logs_dir"]) / "server.log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(log_fd, "wb", buffering=0) as log_stream:
            child = subprocess.Popen(
                command,
                env=env,
                start_new_session=True,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
            )
        pid_record = {
            "pid": child.pid,
            "start_time": _process_start_time(child.pid),
            "owner_nonce": manifest["owner_nonce"],
        }
        _write_once(Path(manifest["pidfile"]), json.dumps(pid_record, sort_keys=True), 0o600)
        _wait_ready(manifest, child)
        print(json.dumps({"status": "healthy", "manifest": str(manifest_path), **pid_record}))
        return 0
    except Exception:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if manifest is not None and sentinel_created:
            _tmux_lifecycle(str(manifest["tmux_socket"]), "kill-server", check=False)
        if manifest is not None:
            shutil.rmtree(manifest["root"], ignore_errors=True)
        raise


def _load_owned(root: Path) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    _assert_clean_components(root)
    root = _canonical(root)
    manifest_path = root / MANIFEST_NAME
    manifest = validate_manifest(read_manifest(manifest_path), manifest_path)
    try:
        pid_record = json.loads(Path(manifest["pidfile"]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SandboxError("invalid sandbox pidfile") from exc
    if pid_record.get("owner_nonce") != manifest["owner_nonce"]:
        raise SandboxError("sandbox pidfile owner mismatch")
    pid = int(pid_record.get("pid", 0))
    if _process_start_time(pid) != int(pid_record.get("start_time", -1)):
        raise SandboxError("sandbox pid identity mismatch")
    return manifest, manifest_path, pid_record


def command_status(args: argparse.Namespace) -> int:
    manifest, manifest_path, pid_record = _load_owned(Path(args.root))
    payload = _health(manifest)
    if (
        payload.get("instance_id") != manifest["instance_id"]
        or payload.get("source", {}).get("source_merkle") != manifest["source"]["source_merkle"]
        or payload.get("source", {}).get("module_contained") is not True
        or payload.get("source", {}).get("interpreter_match") is not True
    ):
        raise SandboxError("sandbox health instance mismatch")
    print(
        json.dumps(
            {
                "status": "healthy",
                "manifest": str(manifest_path),
                "pid": pid_record["pid"],
                "health": payload,
            },
            sort_keys=True,
        )
    )
    return 0


def _sentinel_owned(manifest: dict[str, Any]) -> bool:
    sentinel = f"cao-sbx-{manifest['instance_id']}-owner"
    result = _tmux_lifecycle(
        str(manifest["tmux_socket"]),
        "has-session",
        "-t",
        sentinel,
        check=False,
    )
    return result.returncode == 0


def command_down(args: argparse.Namespace) -> int:
    manifest, _, pid_record = _load_owned(Path(args.root))
    if not _sentinel_owned(manifest):
        raise SandboxError("tmux ownership sentinel missing")
    pid = int(pid_record["pid"])
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.killpg(pid, signal.SIGKILL)
    _tmux_lifecycle(str(manifest["tmux_socket"]), "kill-server")
    Path(manifest["pidfile"]).unlink(missing_ok=True)
    if args.purge:
        current = validate_manifest(
            read_manifest(Path(manifest["root"]) / MANIFEST_NAME),
            Path(manifest["root"]) / MANIFEST_NAME,
        )
        owner = json.loads((Path(current["root"]) / OWNER_LOCK_NAME).read_text(encoding="utf-8"))
        _assert_clean_components(Path(current["root"]) / OWNER_LOCK_NAME)
        root_stat = Path(current["root"]).stat()
        if (
            owner.get("owner_nonce") != current["owner_nonce"]
            or owner.get("root_device") != root_stat.st_dev
            or owner.get("root_inode") != root_stat.st_ino
        ):
            raise SandboxError("purge ownership fence failed")
        shutil.rmtree(current["root"])
    print(json.dumps({"status": "down", "purged": bool(args.purge)}))
    return 0


def command_serve(args: argparse.Namespace) -> NoReturn:
    sys.dont_write_bytecode = True
    manifest_path = Path(args.manifest)
    manifest = validate_manifest(read_manifest(manifest_path), manifest_path)
    _validate_env(manifest, manifest_path)
    assert_sandbox_db_fence(manifest)
    current_source = source_identity(Path(manifest["source"]["fork_root"]))
    if current_source["source_merkle"] != manifest["source"]["source_merkle"]:
        raise SandboxError("working-tree source changed after sandbox reservation")
    import uvicorn

    from cli_agent_orchestrator.api.main import app

    _, port = _validate_endpoint(manifest["endpoint"])
    uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False)
    raise SystemExit(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cao sandbox")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up")
    up.add_argument("--root", required=True)
    up.add_argument("--port", required=True, type=int)
    up.set_defaults(handler=command_up)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--root", required=True)
    status_parser.set_defaults(handler=command_status)
    down = commands.add_parser("down")
    down.add_argument("--root", required=True)
    down.add_argument("--purge", action="store_true")
    down.set_defaults(handler=command_down)
    serve = commands.add_parser("serve")
    serve.add_argument("--manifest", required=True)
    serve.set_defaults(handler=command_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        args = build_parser().parse_args(argv)
        return int(args.handler(args))
    except SandboxError as exc:
        print(f"sandbox error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
