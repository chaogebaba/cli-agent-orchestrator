"""F810 (#667): the fork owns the seat delivery edges.

Covers the four named unit tests from the build brief:

* overlay content for a claude_code supervisor contains the four F810 hook
  registrations with the resolved binary path, dedupes against a pre-existing
  identical command, and (BLOCKER 4) is appended ONLY for a supervisor seat —
  ``test_worker_profile_gets_no_f810_edges`` /
  ``test_worker_overlay_is_byte_equivalent_to_no_role`` are the real negative
  tests, and ``test_effective_project_plus_settings_composition_no_double_run``
  is the effective project-plus-``--settings`` composition test;
* register_inbox / supervisor_drain / rewake exit 0 on: no CAO_TERMINAL_ID,
  empty stdin, server down (mock), malformed JSON;
* the drain envelope text equals the root hook's format for one pending row and
  one suppressed BUSY ping (golden strings);
* transport_ejection emits exactly one native_unreachable condition per episode.

Mutants (see f810-build-report.md): drop the dedupe → duplicate registration;
register PATCH without the idempotency GET; ejection emits every attempt.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.providers.claude_code import (
    ClaudeCodeProvider,
    _dedupe_overlay_hooks_by_command,
)

_REGISTER_MOD = "cli_agent_orchestrator.hooks.register_inbox"
_DRAIN_MOD = "cli_agent_orchestrator.hooks.supervisor_drain"
_REWAKE_MOD = "cli_agent_orchestrator.hooks.rewake"

#: The parent (root) repository that carries the legacy repo-local `.claude`
#: hooks the overlay is replacing. Read-only reference. Resolved from the env
#: (CLAUDE_PROJECT_DIR, as Claude Code sets it) or the known laptop checkout;
#: the composition test does not REQUIRE it to exist (it uses the captured
#: command shapes below), but asserts the `.sh` files when the repo is present.
_PARENT_REPO = Path(
    os.environ.get("CLAUDE_PROJECT_DIR", "/home/chao/VScode_projects/cli-subagents")
)

#: The ACTUAL parent-repo hook command strings, captured verbatim from
#: /home/chao/VScode_projects/cli-subagents/.claude/settings.json (PostToolUse
#: matcher ".*" drain leg; Stop rewake --arm leg). These are the shapes Claude
#: Code composes ALONGSIDE the overlay until deliverable 3 removes them. They are
#: DIFFERENT command strings from the overlay's `python -m …` shapes, so the D2
#: command-string dedupe cannot merge them — the two co-execute (the 0.7 ms
#: race). Embedded as literals so the concurrency test is box-independent.
_PARENT_DRAIN_CMD = "$CLAUDE_PROJECT_DIR/.claude/hooks/supervisor-inbox-drain.sh"
_PARENT_REWAKE_CMD = (
    '"$CLAUDE_PROJECT_DIR/.claude/hooks/supervisor-only.sh" '
    "$CLAUDE_PROJECT_DIR/.claude/hooks/f213-callback-rewake.sh --arm --source=stop"
)


@pytest.fixture
def mailbox_db(monkeypatch, tmp_path):
    """File-backed SQLite wired into the mailbox/database seam for the
    drain-claim concurrency test. A FILE db (not :memory:/StaticPool) so each
    thread's own ``SessionLocal()`` gets its OWN connection — the production
    shape, and the only one under which ``claim_emission``'s SAVEPOINT + the
    UNIQUE(message_id, carrier) constraint arbitrate two concurrent claims
    correctly. Two terminals (sup, wrk) seeded like
    test/services/test_f642_list_claim_flag.py."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.clients.database import Base, create_terminal
    from cli_agent_orchestrator.services import mailbox_service

    db_file = tmp_path / "f810-concurrency.sqlite"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions, raising=False)
    database.clear_terminal_metadata_cache()
    create_terminal("sup", "cao-t", "w-sup", "claude_code")
    create_terminal("wrk", "cao-t", "w-wrk", "claude_code")
    return sessions


def _settings(role: str | None = "supervisor") -> dict:
    """Render the terminal-settings overlay for a seat of the given profile role.

    F810 BLOCKER 4: the seat-delivery edges are supervisor-only, gated on the
    server-authoritative ``AgentProfile.role``. Tests patch ``_load_profile`` so
    the overlay is exercised for a real ``role`` rather than the ``None`` default
    (which is a worker/unknown seat and gets NO F810 edges).
    """
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    provider = ClaudeCodeProvider("hookterm", "session", "window", None)
    prof = AgentProfile(name="p", description="d", role=role) if role is not None else None
    with patch.object(ClaudeCodeProvider, "_load_profile", return_value=prof):
        path = provider._write_terminal_settings()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)


# ── overlay content ───────────────────────────────────────────────────────────


def test_overlay_registers_the_four_f810_edges():
    h = _settings()["hooks"]

    def cmds(event: str) -> list[str]:
        return [hk["command"] for b in h[event] for hk in b["hooks"]]

    # register on SessionStart + PostToolUse(Agent|Task)
    assert any(_REGISTER_MOD in c for c in cmds("SessionStart"))
    assert any(_REGISTER_MOD in c for c in cmds("PostToolUse"))
    # drain on SessionStart + PostToolUse(.*) + Stop
    assert any(_DRAIN_MOD in c for c in cmds("SessionStart"))
    assert any(_DRAIN_MOD in c for c in cmds("PostToolUse"))
    assert any(_DRAIN_MOD in c for c in cmds("Stop"))
    # rewake on PostToolUse + Stop
    assert any(_REWAKE_MOD in c for c in cmds("PostToolUse"))
    assert any(_REWAKE_MOD in c for c in cmds("Stop"))


def test_overlay_commands_use_python_m_not_absolute_path():
    """Do-NOT 20 / F569 #426: python -m <module>, never a .claude/.sh path."""
    h = _settings()["hooks"]
    for event in ("SessionStart", "PostToolUse", "Stop"):
        for b in h[event]:
            for hk in b["hooks"]:
                c = hk["command"]
                if any(m in c for m in (_REGISTER_MOD, _DRAIN_MOD, _REWAKE_MOD)):
                    assert "-m" in c
                    assert ".claude" not in c
                    assert ".sh" not in c


def test_overlay_stop_rewake_is_async_with_root_summary():
    h = _settings()["hooks"]
    stop_rewake = [
        hk
        for b in h["Stop"]
        for hk in b["hooks"]
        if _REWAKE_MOD in hk["command"] and "--source=stop" in hk["command"]
    ]
    assert len(stop_rewake) == 1
    hk = stop_rewake[0]
    assert hk["asyncRewake"] is True
    assert hk["timeout"] == 3600
    assert hk["rewakeSummary"] == "CAO callback waiting"


def test_overlay_no_intra_event_duplicate_commands():
    h = _settings()["hooks"]
    for event, blocks in h.items():
        cmds = [hk["command"] for b in blocks for hk in b["hooks"]]
        assert len(cmds) == len(set(cmds)), (event, cmds)


def test_no_auth_token_leaks_in_overlay():
    assert "CAO_AUTH_LOCAL_TOKEN" not in json.dumps(_settings())


# ── BLOCKER 4: supervisor-only overlay + worker byte-equivalence ────────────────


def _f810_commands(settings: dict) -> list[str]:
    hooks = settings["hooks"]
    return [
        hk["command"]
        for blocks in hooks.values()
        for b in blocks
        for hk in b["hooks"]
        if any(m in hk["command"] for m in (_REGISTER_MOD, _DRAIN_MOD, _REWAKE_MOD))
    ]


def test_worker_profile_gets_no_f810_edges():
    """A real (non-supervisor) worker profile: register/rewake absent entirely,
    and drain present ONLY on the base D22 SessionStart edge — never on the F810
    PostToolUse/Stop legs.

    F810 #667 r3 (B4): the drain command DOES appear on a worker's SessionStart
    (that edge is the unchanged base D22 drain), but on that edge the drain
    module is SERVER-TRIGGER-ONLY — no ``claim=hook`` read, no ack. The
    behavioural proof is ``test_worker_sessionstart_drain_is_server_trigger_only``
    below; this test proves only the STRUCTURE (which edges carry which command)."""
    h = _settings(role="developer")["hooks"]

    def cmds(event: str) -> list[str]:
        return [hk["command"] for b in h.get(event, []) for hk in b["hooks"]]

    # No register / rewake anywhere for a worker.
    assert not any(_REGISTER_MOD in c for e in h for c in cmds(e))
    assert not any(_REWAKE_MOD in c for e in h for c in cmds(e))
    # Drain only on SessionStart (base D22), NOT the F810 PostToolUse/Stop legs.
    assert any(_DRAIN_MOD in c for c in cmds("SessionStart"))
    assert not any(_DRAIN_MOD in c for c in cmds("PostToolUse"))
    assert not any(_DRAIN_MOD in c for c in cmds("Stop"))


def test_worker_overlay_is_byte_equivalent_to_no_role():
    """`role=None` (unknown seat) and an explicit worker role produce the same
    overlay — the F810 gate keys on supervisor-ness, nothing else."""
    assert json.dumps(_settings(role=None), sort_keys=True) == json.dumps(
        _settings(role="developer"), sort_keys=True
    )


def test_worker_stop_block_matches_base_shape():
    """A worker's Stop carries only marker+ack+turn (base shape) — no F810 drain
    or rewake leg."""
    h = _settings(role="reviewer")["hooks"]
    stop_cmds = [hk["command"] for b in h["Stop"] for hk in b["hooks"]]
    assert not any(_DRAIN_MOD in c or _REWAKE_MOD in c for c in stop_cmds)


def test_supervisor_only_edges_present_for_supervisor():
    """The positive control: a supervisor DOES get all four F810 edges."""
    cmds = _f810_commands(_settings(role="supervisor"))
    assert any(_REGISTER_MOD in c for c in cmds)
    assert any(_DRAIN_MOD in c for c in cmds)
    assert any(_REWAKE_MOD in c for c in cmds)


def test_parent_sh_and_overlay_have_distinct_command_strings():
    """Precondition for the concurrency tests below: the parent-repo `.sh` hook
    commands and the overlay `python -m` commands are DIFFERENT strings, so the
    D2 command-string dedupe CANNOT merge them. Until the parent repo drops its
    copies (deliverable 3), Claude Code composes BOTH and starts them
    concurrently — exactly the 0.7 ms race the verdict flagged. Uses the ACTUAL
    parent command shapes (captured verbatim from the parent settings.json) and
    the overlay shapes; when the live parent repo is present it ALSO cross-checks
    that the captured shapes still match the on-disk settings.json (read-only)."""
    overlay = _settings(role="supervisor")["hooks"]
    overlay_drain = next(c for c in _f810_commands({"hooks": overlay}) if _DRAIN_MOD in c)
    overlay_rewake = next(
        hk["command"]
        for b in overlay["Stop"]
        for hk in b["hooks"]
        if _REWAKE_MOD in hk["command"] and "--source=stop" in hk["command"]
    )

    # DIFFERENT command strings → the D2 dedupe leaves BOTH → they co-execute.
    assert _PARENT_DRAIN_CMD != overlay_drain
    assert _PARENT_REWAKE_CMD != overlay_rewake
    merged = {
        "PostToolUse": [{"hooks": [{"command": _PARENT_DRAIN_CMD}, {"command": overlay_drain}]}],
        "Stop": [{"hooks": [{"command": _PARENT_REWAKE_CMD}, {"command": overlay_rewake}]}],
    }
    _dedupe_overlay_hooks_by_command(merged)
    ptu = [hk["command"] for b in merged["PostToolUse"] for hk in b["hooks"]]
    stop = [hk["command"] for b in merged["Stop"] for hk in b["hooks"]]
    assert _PARENT_DRAIN_CMD in ptu and overlay_drain in ptu  # neither deduped
    assert _PARENT_REWAKE_CMD in stop and overlay_rewake in stop

    # Optional live cross-check: if the parent repo is present, the captured
    # shapes must still equal the on-disk settings.json commands (read-only).
    parent_settings = _PARENT_REPO / ".claude" / "settings.json"
    if parent_settings.is_file():
        parent = json.loads(parent_settings.read_text(encoding="utf-8"))["hooks"]

        def pcmds(event: str) -> list[str]:
            return [hk["command"] for b in parent.get(event, []) for hk in b.get("hooks", [])]

        assert _PARENT_DRAIN_CMD in pcmds("PostToolUse")
        assert _PARENT_REWAKE_CMD in pcmds("Stop")
        assert (_PARENT_REPO / ".claude" / "hooks" / "supervisor-inbox-drain.sh").is_file()
        assert (_PARENT_REPO / ".claude" / "hooks" / "f213-callback-rewake.sh").is_file()


def test_concurrent_parent_and_overlay_drain_single_claim(mailbox_db):
    """DRAIN single-claim under the 0.7 ms race. The parent `.sh` drain runs
    ``cao messages list --to me --status pending --claim hook`` and the overlay
    ``supervisor_drain`` runs ``GET /messages?…claim=hook`` — DIFFERENT commands,
    but BOTH resolve to the SAME server read-as-claim
    (``list_messages(receiver, claim='hook')`` → ``hook_claim_ids``). Fired
    concurrently against ONE pending id, the server's UNIQUE(message_id, carrier)
    claim lets exactly ONE win; the other returns the id NOT at all. This is the
    server-side dedupe that HOLDS for the claim (the alternative the verdict
    named)."""
    import threading

    from cli_agent_orchestrator.clients.database import create_inbox_message
    from cli_agent_orchestrator.services.mailbox_service import list_messages

    msg = create_inbox_message("sup", "wrk", "one real callback")

    results: list[list[int]] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def drain_once() -> None:
        barrier.wait()  # release both within microseconds of each other
        won = [int(i["id"]) for i in list_messages("wrk", claim="hook")["items"]]
        with lock:
            results.append(won)

    threads = [threading.Thread(target=drain_once) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Exactly one carrier won the id; the other won nothing. One drain claim.
    winners = [r for r in results if msg.id in r]
    assert len(results) == 2
    assert len(winners) == 1, results
    assert sum(r.count(msg.id) for r in results) == 1


def _find_parent_rewake_hook():
    """Locate the real parent-repo ``f213-callback-rewake.sh`` (read-only).

    Order: explicit ``CAO_F810_PARENT_HOOK`` env → ``CLAUDE_PROJECT_DIR`` →
    ``_PARENT_REPO``. Returns a Path or None (the caller skips when absent, e.g.
    on a box that has no parent checkout)."""
    candidates = []
    env_hook = os.environ.get("CAO_F810_PARENT_HOOK")
    if env_hook:
        candidates.append(Path(env_hook))
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        candidates.append(Path(proj) / ".claude" / "hooks" / "f213-callback-rewake.sh")
    candidates.append(_PARENT_REPO / ".claude" / "hooks" / "f213-callback-rewake.sh")
    for c in candidates:
        if c.is_file():
            return c
    return None


def test_actual_parent_sh_and_overlay_single_wake_persistent_row(tmp_path):
    """B4 (r4): the ACTUAL parent ``.sh`` + overlay ``python -m`` pair against ONE
    PERSISTENTLY pending row must produce EXACTLY ONE wake — the r3 EMPIRICAL gate
    reproduced a DOUBLE wake here (overlay wins the lock, wakes id 42, releases;
    the parent Stop watcher retries the freed lock for up to 15s, consults its OWN
    state.json, and wakes id 42 again). The r4 fix is a one-directional shared
    wake cursor: the parent reads/writes the overlay's
    ``$CAO_HOME_DIR/f810-rewake-state.<tid>.json`` so a post-release retry sees the
    id as already woken.

    This runs the REAL scripts as subprocesses (reviewer driver shape,
    /data/cao-scratch/30eb5f40/actual_pair_repro.py): a stub HTTP server serves id
    42 on EVERY poll (persistent), the overlay is started first and holds
    ``watcher.lock`` between two stability polls, and the parent is started inside
    that window so its production 15s lock-retry fires. Assert exactly one exit 2
    and one ``rewakeSummary`` across BOTH processes.

    Skips when the parent ``.sh`` is not present (e.g. a box without the parent
    checkout); the box A/B run in the report exercises the real pair."""
    import json as _json
    import subprocess
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    parent_hook = _find_parent_rewake_hook()
    if parent_hook is None:
        pytest.skip("parent f213-callback-rewake.sh not present in this checkout")

    # Fork ``src`` for the overlay subprocess' PYTHONPATH — resolve from this file
    # (test/providers/…  → repo root is parents[2]).
    fork_src = Path(__file__).resolve().parents[2] / "src"

    home = tmp_path / "home"
    lockdir = tmp_path / "lock"
    datadir = tmp_path / "data"
    for d in (home, lockdir, datadir):
        d.mkdir(parents=True)

    reqs: list[tuple[float, str]] = []
    reqs_lock = threading.Lock()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            with reqs_lock:
                reqs.append((time.monotonic(), self.path))
            if self.path.startswith("/messages"):
                body: dict = {
                    "items": [
                        {
                            "id": 42,
                            "sender_id": "wrk1",
                            "message": "one persistent callback",
                            "status": "pending",
                        }
                    ]
                }
            elif self.path.startswith("/terminals/"):
                body = {"status": "ready"}
            else:
                body = {}
            enc = _json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(enc)))
            self.end_headers()
            self.wfile.write(enc)

        def log_message(self, *a: object) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"

    common = os.environ.copy()
    common.update(
        {
            "CAO_TERMINAL_ID": "abcd1234",
            "CAO_ENDPOINT": endpoint,
            "CAO_API_BASE_URL": endpoint,
            "CAO_HOME_DIR": str(home),
            "CAO_DATA_DIR": str(datadir),
            "F213_STATE_DIR": str(lockdir),  # shared watcher.lock dir
            "F213_COOLDOWN_S": "300",
            "F213_MAX_STREAK": "3",
            "F213_OWNER_CHECK_CADENCE": "999",
            "CAO_PROCESS_INCARNATION": "inc1",
            "CAO_OVERLAY_HOOKS_ACTIVE": "1",
        }
    )
    common.pop("CLAUDE_AGENT_ID", None)

    overlay_env = common.copy()
    overlay_env.update(
        {
            "PYTHONPATH": str(fork_src),
            "F213_POLL_INTERVAL_S": "0.20",
            "F213_STABILITY_POLLS": "2",
            "F213_DEADLINE_S": "6",
        }
    )
    parent_env = common.copy()
    parent_env.update(
        {
            "F213_POLL_INTERVAL_S": "0.01",
            "F213_STABILITY_POLLS": "2",
            "F213_DEADLINE_S": "6",
        }
    )

    overlay_cmd = [
        sys.executable,
        "-m",
        "cli_agent_orchestrator.hooks.rewake",
        "--arm",
        "--source=stop",
    ]
    parent_cmd = [str(parent_hook), "--arm", "--source=stop"]

    try:
        overlay = subprocess.Popen(
            overlay_cmd,
            env=overlay_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Wait until the overlay has reached its first poll (it now holds the
        # lock, between stability polls) before starting the parent.
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with reqs_lock:
                seen = any(p.startswith("/messages") for _, p in reqs)
            if seen:
                break
            time.sleep(0.005)
        else:
            overlay.kill()
            pytest.fail("overlay did not reach its first poll")

        parent_started = time.monotonic()
        parent = subprocess.Popen(
            parent_cmd,
            cwd=str(parent_hook.parent.parent.parent),
            env=parent_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        overlay_out, _ = overlay.communicate(timeout=30)
        parent_out, _ = parent.communicate(timeout=30)
    finally:
        server.shutdown()
        th.join(timeout=2)

    rcs = sorted([overlay.returncode, parent.returncode])
    wake_summaries = [
        out for out in (overlay_out.strip(), parent_out.strip()) if "rewakeSummary" in out
    ]
    # EXACTLY ONE wake across the real pair: one exit 2, one exit 0, one summary.
    assert rcs == [0, 2], (rcs, overlay_out, parent_out)
    assert len(wake_summaries) == 1, (overlay_out, parent_out)
    # And the one wake names id 42.
    assert '"id 42"' in wake_summaries[0] or "id 42" in wake_summaries[0]


def test_dedupe_drops_second_identical_command_first_wins():
    hooks = {
        "PostToolUse": [
            {"matcher": "Agent|Task", "hooks": [{"command": "X", "timeout": 10}]},
            {"matcher": ".*", "hooks": [{"command": "X", "timeout": 99}]},
            {"matcher": ".*", "hooks": [{"command": "Y", "timeout": 10}]},
        ]
    }
    _dedupe_overlay_hooks_by_command(hooks)
    cmds = [hk["command"] for b in hooks["PostToolUse"] for hk in b["hooks"]]
    assert cmds == ["X", "Y"]  # second X dropped
    # first occurrence's fields preserved (timeout 10, not 99)
    x = [hk for b in hooks["PostToolUse"] for hk in b["hooks"] if hk["command"] == "X"][0]
    assert x["timeout"] == 10


def test_dedupe_drops_emptied_block():
    hooks = {
        "Stop": [
            {"hooks": [{"command": "A"}]},
            {"hooks": [{"command": "A"}]},  # fully duplicate -> emptied -> dropped
        ]
    }
    _dedupe_overlay_hooks_by_command(hooks)
    assert len(hooks["Stop"]) == 1


def test_dedupe_is_per_event_not_cross_event():
    hooks = {
        "SessionStart": [{"hooks": [{"command": "DRAIN"}]}],
        "Stop": [{"hooks": [{"command": "DRAIN"}]}],
    }
    _dedupe_overlay_hooks_by_command(hooks)
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == "DRAIN"
    assert hooks["Stop"][0]["hooks"][0]["command"] == "DRAIN"


# ── register_inbox hook: containment + fail-open ────────────────────────────────


def _run_register(event, monkeypatch, *, stdin: str | None = None):
    from cli_agent_orchestrator.hooks import register_inbox

    payload = json.dumps(event) if stdin is None else stdin
    with patch("sys.stdin", io.StringIO(payload)):
        return register_inbox.main()


def test_register_no_terminal_id_zero_side_effects(monkeypatch):
    from cli_agent_orchestrator.hooks import register_inbox

    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    with patch.object(register_inbox.cao_http, "patch") as p:
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
    p.assert_not_called()


def test_register_empty_stdin_returns_zero(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    assert _run_register(None, monkeypatch, stdin="") == 0


def test_register_malformed_json_returns_zero(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    assert _run_register(None, monkeypatch, stdin="{not json") == 0


def test_register_no_session_id_returns_zero(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    assert _run_register({"source": "startup"}, monkeypatch) == 0


def test_register_zero_teams_warns_once_per_window(monkeypatch, tmp_path, capsys):
    from cli_agent_orchestrator.hooks import register_inbox

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    # No team dirs derivable → WARN, throttled by the sentinel under CAO_HOME_DIR.
    monkeypatch.setattr(register_inbox, "CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(register_inbox, "_derive_team_names", lambda sid, home: [])
    with (
        patch.object(register_inbox.cao_http, "post") as _post,
        patch.object(register_inbox.cao_http, "patch") as p,
    ):
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
        first = capsys.readouterr().err
        # second immediate fire is throttled (no new WARN)
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
        second = capsys.readouterr().err
    p.assert_not_called()
    assert "f810.native_unpublished" in first
    assert second == ""


def test_register_zero_teams_posts_server_trace_once_then_retries_after_window(
    monkeypatch, tmp_path
):
    """BLOCKER 6: the zero-team WARN is journal-visible VIA THE SERVER — exactly
    one POST to /native-unpublished inside the 10-min window, and a second POST
    after the window expires (sentinel mtime pushed back)."""
    from cli_agent_orchestrator.hooks import register_inbox

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.delenv("CAO_TERMINAL_TOKEN", raising=False)
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(register_inbox, "CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(register_inbox, "_derive_team_names", lambda sid, home: [])
    post = MagicMock()
    with (
        patch.object(register_inbox, "get_local_bearer", return_value=None),
        patch.object(register_inbox.cao_http, "post", post),
        patch.object(register_inbox.cao_http, "patch") as patch_call,
    ):
        # First fire → one server POST.
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
        # Second immediate fire → throttled, NO additional POST.
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
        assert post.call_count == 1
        assert post.call_args[0][0] == "/terminals/abcd1234/native-unpublished"
        assert post.call_args.kwargs["json"]["terminal_id"] == "abcd1234"

        # Expire the window: push the sentinel mtime back beyond the interval.
        sentinel = tmp_path / "f810-native-unpublished.abcd1234"
        import os as _os

        old = register_inbox.time.time() - (register_inbox._NATIVE_UNPUBLISHED_WARN_INTERVAL_S + 10)
        _os.utime(sentinel, (old, old))
        # Third fire after expiry → a second server POST (retry).
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
        assert post.call_count == 2
    patch_call.assert_not_called()


def test_register_idempotent_skips_patch_when_already_registered(monkeypatch, tmp_path):
    from cli_agent_orchestrator.hooks import register_inbox

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.delenv("CAO_TERMINAL_TOKEN", raising=False)
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    team = tmp_path / ".claude" / "teams" / "myteam" / "inboxes"
    team.mkdir(parents=True)
    inbox = team / "team-lead.json"
    inbox.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(register_inbox, "_derive_team_names", lambda sid, home: ["myteam"])
    monkeypatch.setattr(
        register_inbox.os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p
    )
    get_resp = MagicMock()
    get_resp.json.return_value = {"metadata": {"cc_team_inbox_path": str(inbox)}}
    get_resp.raise_for_status = MagicMock()
    with (
        patch.object(register_inbox, "get_local_bearer", return_value=None),
        patch.object(register_inbox.cao_http, "get", return_value=get_resp),
        patch.object(register_inbox.cao_http, "patch") as p,
    ):
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
    p.assert_not_called()  # already registered → no PATCH (idempotency)


def test_register_patches_when_absent(monkeypatch, tmp_path):
    from cli_agent_orchestrator.hooks import register_inbox

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.delenv("CAO_TERMINAL_TOKEN", raising=False)
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    team = tmp_path / ".claude" / "teams" / "myteam" / "inboxes"
    team.mkdir(parents=True)
    inbox = team / "team-lead.json"
    inbox.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(register_inbox, "_derive_team_names", lambda sid, home: ["myteam"])
    monkeypatch.setattr(
        register_inbox.os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p
    )
    get_resp = MagicMock()
    get_resp.json.return_value = {"metadata": {"existing": "keep"}}
    get_resp.raise_for_status = MagicMock()
    patch_resp = MagicMock()
    with (
        patch.object(register_inbox, "get_local_bearer", return_value=None),
        patch.object(register_inbox.cao_http, "get", return_value=get_resp),
        patch.object(register_inbox.cao_http, "patch", return_value=patch_resp) as p,
    ):
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
    assert p.call_args[0][0] == "/terminals/abcd1234/metadata"
    sent = p.call_args.kwargs["json"]["metadata"]
    assert sent["cc_team_inbox_path"] == str(inbox)
    assert sent["existing"] == "keep"  # whole-dict merge preserved prior keys


# ── rewake hook: containment + fail-open ────────────────────────────────────────


def test_rewake_no_terminal_id_returns_zero(monkeypatch):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    with patch("sys.stdin", io.StringIO("{}")):
        assert rewake.main(["--arm", "--source=stop"]) == 0


def test_rewake_subagent_gate_returns_zero(monkeypatch):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch("sys.stdin", io.StringIO(json.dumps({"agent_id": "sub"}))):
        assert rewake.main(["--arm", "--source=stop"]) == 0


def test_rewake_empty_stdin_returns_zero(monkeypatch):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch("sys.stdin", io.StringIO("")):
        # empty stdin -> gate returns None -> arm runs; force an instant deadline
        monkeypatch.setenv("F213_DEADLINE_S", "0")
        with patch.object(rewake, "get_local_bearer", return_value=None):
            assert rewake.main(["--arm", "--source=stop"]) == 0


def test_rewake_prime_is_noop(monkeypatch):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch("sys.stdin", io.StringIO("{}")):
        assert rewake.main(["--prime", "--notify"]) == 0


def test_rewake_wakes_on_new_pending(monkeypatch, tmp_path, capsys):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(rewake, "CAO_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("F213_STABILITY_POLLS", "1")
    monkeypatch.setenv("F213_POLL_INTERVAL_S", "0")
    resp = MagicMock()
    resp.json.return_value = {
        "items": [{"id": 42, "sender_id": "wrk1", "message": "hi", "status": "pending"}]
    }
    resp.raise_for_status = MagicMock()
    with (
        patch.object(rewake, "get_local_bearer", return_value=None),
        patch.object(rewake.cao_http, "get", return_value=resp),
        patch("sys.stdin", io.StringIO("{}")),
    ):
        rc = rewake.main(["--arm", "--source=stop"])
    out = capsys.readouterr()
    assert rc == 2
    assert json.loads(out.out.strip()) == {"rewakeSummary": "CAO callback waiting (id 42)"}
    assert "[42] from=wrk1" in out.err


def test_rewake_busy_ping_does_not_wake(monkeypatch, tmp_path):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(rewake, "CAO_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("F213_STABILITY_POLLS", "1")
    monkeypatch.setenv("F213_DEADLINE_S", "0")
    resp = MagicMock()
    resp.json.return_value = {
        "items": [
            {"id": 7, "sender_id": "x", "message": "[CONDITION] kind=BUSY", "status": "pending"}
        ]
    }
    resp.raise_for_status = MagicMock()
    with (
        patch.object(rewake, "get_local_bearer", return_value=None),
        patch.object(rewake.cao_http, "get", return_value=resp),
        patch("sys.stdin", io.StringIO("{}")),
    ):
        # BUSY filtered → treated as empty → loop hits the 0s deadline → exit 0.
        assert rewake.main(["--arm", "--source=stop"]) == 0
