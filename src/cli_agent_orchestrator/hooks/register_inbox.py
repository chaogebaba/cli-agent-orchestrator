"""Seat inbox registration hook (F810 #667 — overlay-composed port of f162).

Publishes the native carrier's socket coordinate (``cc_team_inbox_path``) onto
the seat terminal's metadata so the F783 native ring can reach it. This is the
Python port of the old repo-local ``.claude/hooks/f162-register-inbox.sh``,
relocated into the per-seat settings overlay composed by
``providers/claude_code.py::_write_terminal_settings`` and invoked exactly like
the sibling hooks: ``env CAO_API_BASE_URL=… python -m
cli_agent_orchestrator.hooks.register_inbox``. No console_scripts entry point,
no absolute install path, no ``~/.claude`` / ``.claude/hooks`` reference
(Do-NOT 20 / F569 #426).

Root cause it fixes (F810 evidence pack): the native carrier's socket was
published ONLY by the repo-local hook, so a seat in another repo — or one that
never spawned an in-harness Agent — was unreachable natively and had no
fallback, leaving callback rows pending for many minutes.

Derivation law (f162 D9, preserved verbatim in behaviour): the teamName is
derived BY EXACT KEY from the seat's OWN session's subagent meta files
(``~/.claude/projects/*/<session_id>/subagents/*.meta.json``), never by
recency/mtime among team dirs. Exactly one unique teamName registers
``~/.claude/teams/<teamName>/inboxes/team-lead.json``; zero or ambiguous
matches register NOTHING.

Zero-team WARN (F810): when zero team dirs are derivable, the seat is natively
unpublishable right now. We register nothing AND emit one journal-visible WARN
(``f810.native_unpublished``) to stderr, rate-limited to at most once per 10
minutes per terminal via a sentinel file, so a busy seat that fires this hook
on every tool call does not spam the journal.

Containment + fail-open + idempotency: fires ONLY inside a CAO terminal
(``CAO_TERMINAL_ID`` set) with a real session_id on stdin; any transport error
is swallowed with ``return 0`` (a hook must never block the seat); the PATCH is
skipped when a GET shows the same path already registered (idempotent).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.utils.http import CAOHttpClient, resolve_endpoint

cao_http = CAOHttpClient(lambda: requests)

#: F810: rate-limit the zero-team WARN to at most once per 10 min per terminal.
_NATIVE_UNPUBLISHED_WARN_INTERVAL_S: float = 600.0
_NATIVE_UNPUBLISHED_WARN_KIND = "f810.native_unpublished"


def _derive_team_names(session_id: str, home: Path) -> list[str]:
    """Return the UNIQUE teamNames derivable from the seat's own session.

    f162 D9 law: exact-key read of ``teamName`` from every
    ``~/.claude/projects/*/<session_id>/subagents/*.meta.json``; de-duplicated,
    order-preserving. Never a recency/mtime selection among team dirs.
    """
    team_names: list[str] = []
    projects_root = home / ".claude" / "projects"
    # glob is */<session_id>/subagents/*.meta.json — one project dir level.
    try:
        candidates = sorted(projects_root.glob(f"*/{session_id}/subagents/*.meta.json"))
    except OSError:
        return team_names
    for meta in candidates:
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        tname = data.get("teamName") if isinstance(data, dict) else None
        if tname and str(tname) not in team_names:
            team_names.append(str(tname))
    return team_names


def _post_native_unpublished(terminal_id: str, detail: str) -> None:
    """Best-effort POST one ``f810.native_unpublished`` trace event (BLOCKER 6).

    D1 requires the throttled WARN to be journal-visible VIA THE SERVER, not only
    on hook stderr. This posts through the SAME authenticated client path the
    register PATCH uses (F707 terminal-token header), to the
    ``/terminals/<id>/native-unpublished`` edge which appends one
    ``inbox_message_trace_event`` row. Fail-open: any transport error is
    swallowed (a hook must never block the seat).
    """
    try:
        base_url = (
            os.environ.get("CAO_ENDPOINT")
            or os.environ.get("CAO_API_BASE_URL")
            or resolve_endpoint()
        ).rstrip("/")
        headers: dict[str, str] = {}
        token = get_local_bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        terminal_token = os.environ.get("CAO_TERMINAL_TOKEN", "")
        if terminal_token:
            headers["X-CAO-Terminal-Token"] = terminal_token
        cao_http.post(
            f"/terminals/{terminal_id}/native-unpublished",
            base_url=base_url,
            json={"terminal_id": terminal_id, "ts": detail},
            headers=headers,
            timeout=5,
        )
    except Exception:
        pass  # best-effort; stderr WARN below is the supplemental signal.


def _warn_native_unpublished(terminal_id: str, session_id: str, detail: str) -> None:
    """Emit ONE journal-visible ``f810.native_unpublished`` WARN, rate-limited.

    Rate-limit is a per-terminal sentinel file whose mtime is checked against
    :data:`_NATIVE_UNPUBLISHED_WARN_INTERVAL_S`. Inside the window we do nothing;
    on the first fire after the window we (1) best-effort POST the server-side
    journal-visible trace event (BLOCKER 6, the D1 requirement) and (2) print the
    supplemental stderr WARN. Fail-open: any filesystem error still falls through
    to emit (a duplicate is harmless; a swallowed one is the silence F810 ends).
    """
    sentinel = Path(CAO_HOME_DIR) / f"f810-native-unpublished.{terminal_id}"
    now = time.time()
    try:
        last = sentinel.stat().st_mtime
        if (now - last) < _NATIVE_UNPUBLISHED_WARN_INTERVAL_S:
            return
    except FileNotFoundError:
        pass
    except OSError:
        pass  # fall through: emit anyway
    # (1) Server-side journal-visible trace event — the D1 primary signal.
    _post_native_unpublished(terminal_id, detail)
    # (2) Supplemental stderr WARN (f162's journal convention).
    print(
        f"WARNING: {_NATIVE_UNPUBLISHED_WARN_KIND} terminal={terminal_id} "
        f"session={session_id} {detail} — seat is not natively reachable; "
        f"registering nothing (recoverable)",
        file=sys.stderr,
    )
    try:
        Path(CAO_HOME_DIR).mkdir(parents=True, exist_ok=True, mode=0o700)
        sentinel.touch()
    except OSError:
        pass  # best-effort; a missing sentinel only relaxes the rate-limit


def main() -> int:
    # Containment BEFORE anything else: zero side effects outside a CAO terminal.
    if not os.environ.get("CAO_TERMINAL_ID"):
        return 0
    terminal_id = os.environ["CAO_TERMINAL_ID"]
    try:
        try:
            event = json.load(sys.stdin)
        except Exception:
            return 0
        if not isinstance(event, dict):
            return 0
        session_id = str(event.get("session_id", "") or "")
        if not session_id:
            return 0

        home = Path(os.path.expanduser("~"))
        team_names = _derive_team_names(session_id, home)

        if len(team_names) == 0:
            _warn_native_unpublished(terminal_id, session_id, "0 teamName matches")
            return 0
        if len(team_names) > 1:
            # Ambiguous — f162 registers nothing. A distinct condition from
            # "unpublishable" (an ambiguous seat DID spawn agents), so it is not
            # routed through the throttled native_unpublished WARN.
            print(
                f"WARNING: register_inbox terminal={terminal_id} "
                f"session={session_id} {len(team_names)} distinct teamNames — "
                f"ambiguous, registering nothing",
                file=sys.stderr,
            )
            return 0

        team_name = team_names[0]
        team_dir = home / ".claude" / "teams" / team_name
        inbox_path = team_dir / "inboxes" / "team-lead.json"
        # f162 / F213 D15: register only when the team dir AND inbox file exist.
        if not team_dir.is_dir() or not inbox_path.is_file():
            _warn_native_unpublished(
                terminal_id, session_id, f"team dir/inbox for {team_name} absent"
            )
            return 0

        cc_team_inbox_path = str(inbox_path)
        base_url = (
            os.environ.get("CAO_ENDPOINT")
            or os.environ.get("CAO_API_BASE_URL")
            or resolve_endpoint()
        ).rstrip("/")
        headers: dict[str, str] = {}
        token = get_local_bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # F707 (#562): bind this mutation to the ROUTE terminal via its own token.
        terminal_token = os.environ.get("CAO_TERMINAL_TOKEN", "")
        if terminal_token:
            headers["X-CAO-Terminal-Token"] = terminal_token

        # Idempotency: GET current metadata; skip PATCH when already registered.
        existing_meta: dict[str, Any] = {}
        try:
            resp = cao_http.get(
                f"/terminals/{terminal_id}",
                base_url=base_url,
                headers=headers,
                timeout=5,
            )
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, dict):
                existing_meta = body.get("metadata") or {}
                if not isinstance(existing_meta, dict):
                    existing_meta = {}
                if existing_meta.get("cc_team_inbox_path") == cc_team_inbox_path:
                    return 0  # already registered with the correct path — no-op.
        except Exception:
            existing_meta = {}

        # Whole-dict replace (PATCH semantics): preserve existing keys, set ours.
        merged = dict(existing_meta)
        merged["cc_team_inbox_path"] = cc_team_inbox_path
        response = cao_http.patch(
            f"/terminals/{terminal_id}/metadata",
            base_url=base_url,
            json={"metadata": merged},
            headers=headers,
            timeout=5,
        )
        response.raise_for_status()
    except Exception as exc:
        print(
            f"WARNING: CAO register-inbox edge failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
