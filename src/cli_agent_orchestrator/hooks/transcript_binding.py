"""Best-effort Claude SessionStart transcript binding transport."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.utils.http import CAOHttpClient, resolve_endpoint

cao_http = CAOHttpClient(lambda: requests)
_DEADLETTER_MAX_BYTES = 512 * 1024
_SENSITIVE = re.compile(r"authorization|bearer|token|secret|api[_-]?key", re.IGNORECASE)


def _bounded_utf8(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _redact_error(value: object) -> str:
    text = " ".join(str(value).splitlines())
    sensitive = _SENSITIVE.search(text)
    if sensitive is not None:
        text = text[: sensitive.start()] + sensitive.group(0) + " [REDACTED]"
    home = str(Path.home())
    text = re.sub(
        re.escape(home) + r"(?:/[^\s'\";,]+)+",
        lambda match: Path(match.group(0)).name,
        text,
    )
    return text[:200]


def _deadletter(terminal_id: str, event_source: str, error_class: str, error: object) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "terminal_id": _bounded_utf8(terminal_id, 128),
        "event_source": _bounded_utf8(event_source, 64),
        "error_class": _bounded_utf8(error_class, 64),
        "error": _redact_error(error),
    }
    encoded = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    while len(encoded) > 1024 and record["error"]:
        record["error"] = record["error"][:-1]
        encoded = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    root = Path(CAO_HOME_DIR)
    lock_fd = None
    data_fd = None
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        lock_path = root / "hook-deadletter.lock"
        data_path = root / "hook-deadletter.jsonl"
        rotated_path = root / "hook-deadletter.jsonl.1"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lock_fd, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if data_path.exists():
            data_path.chmod(0o600)
        if rotated_path.exists():
            rotated_path.chmod(0o600)
        current_size = data_path.stat().st_size if data_path.exists() else 0
        if current_size + len(encoded) > _DEADLETTER_MAX_BYTES:
            if data_path.exists():
                os.replace(data_path, rotated_path)
                rotated_path.chmod(0o600)
            current_size = 0
        data_fd = os.open(data_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(data_fd, 0o600)
        os.write(data_fd, encoded)
    except Exception as exc:
        print(
            f"WARNING: CAO transcript binding dead-letter failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    finally:
        if data_fd is not None:
            os.close(data_fd)
        if lock_fd is not None:
            os.close(lock_fd)


def main() -> int:
    terminal_id = "unknown"
    event_source = "unparsed"
    try:
        event = json.load(sys.stdin)
        terminal_id = os.environ["CAO_TERMINAL_ID"]
        event_source = str(event.get("source", ""))
        base_url = (
            os.environ.get("CAO_ENDPOINT")
            or os.environ.get("CAO_API_BASE_URL")
            or resolve_endpoint()
        ).rstrip("/")
        payload = {
            "terminal_id": terminal_id,
            "session_id": event["session_id"],
            "transcript_path": event["transcript_path"],
            "cwd": event.get("cwd", ""),
            "source": event.get("source", ""),
        }
        headers = {}
        token = get_local_bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = cao_http.post(
            f"/terminals/{terminal_id}/transcript-binding",
            base_url=base_url,
            json=payload,
            headers=headers,
            timeout=5,
        )
        response.raise_for_status()
    except Exception as exc:
        _deadletter(terminal_id, event_source, type(exc).__name__, exc)
        print(
            f"WARNING: CAO transcript binding failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
