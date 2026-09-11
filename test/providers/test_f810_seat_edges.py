"""F810 (#667): the fork owns the seat delivery edges.

Covers the four named unit tests from the build brief:

* overlay content for a claude_code supervisor contains the F810 hook
  registrations with the resolved binary path, dedupes against a pre-existing
  identical command, and (BLOCKER 4) is appended ONLY for a supervisor seat —
  ``test_worker_profile_gets_no_f810_edges`` /
  ``test_worker_overlay_is_byte_equivalent_to_no_role`` are the real negative
  tests;
* register_inbox / rewake exit 0 on: no CAO_TERMINAL_ID, empty stdin, server
  down (mock), malformed JSON.

WP-ARCH 3c K1: the supervisor_drain / supervisor_ack hooks are DELETED — they
were a second seat carrier over the same message id, racing the server-side
delivery tick with an ack watermark the tick never consulted (#506/#499). What
survives here is exactly the pair the native carrier needs: ``register_inbox``
(the registration edge that publishes the seat's socket — without it the server
can reach no seat at all) and ``rewake`` (the residual idle-gap re-arm). The
arms below pin BOTH their presence and the drain's absence.

Mutants (see f810-build-report.md): drop the dedupe → duplicate registration;
register PATCH without the idempotency GET.
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
_REWAKE_MOD = "cli_agent_orchestrator.hooks.rewake"
#: WP-ARCH 3c K1: deleted. Kept as a literal so the arms below can assert the
#: overlay never composes it again.
_DRAIN_MOD = "cli_agent_orchestrator.hooks.supervisor_drain"
_ACK_MOD = "cli_agent_orchestrator.hooks.supervisor_ack"


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


def test_overlay_registers_the_surviving_f810_edges():
    h = _settings()["hooks"]

    def cmds(event: str) -> list[str]:
        return [hk["command"] for b in h[event] for hk in b["hooks"]]

    # register on SessionStart + PostToolUse(Agent|Task)
    assert any(_REGISTER_MOD in c for c in cmds("SessionStart"))
    assert any(_REGISTER_MOD in c for c in cmds("PostToolUse"))
    # rewake on PostToolUse + Stop
    assert any(_REWAKE_MOD in c for c in cmds("PostToolUse"))
    assert any(_REWAKE_MOD in c for c in cmds("Stop"))


def test_supervisor_overlay_composes_no_drain_or_ack_arm():
    """WP-ARCH 3c K1: the seat's carrier is the server-side delivery tick, so no
    hook may drain or ack the seat's inbox on ANY event — that was the second
    carrier over one id (#506/#499). The registration edge must survive alongside
    (deleting the whole is_supervisor block silences every seat)."""
    h = _settings(role="supervisor")["hooks"]
    every = [hk["command"] for blocks in h.values() for b in blocks for hk in b["hooks"]]
    assert not any(_DRAIN_MOD in c for c in every), every
    assert not any(_ACK_MOD in c for c in every), every
    assert any(_REGISTER_MOD in c for c in every), every


def test_overlay_commands_use_python_m_not_absolute_path():
    """Do-NOT 20 / F569 #426: python -m <module>, never a .claude/.sh path."""
    h = _settings()["hooks"]
    for event in ("SessionStart", "PostToolUse", "Stop"):
        for b in h[event]:
            for hk in b["hooks"]:
                c = hk["command"]
                if any(m in c for m in (_REGISTER_MOD, _REWAKE_MOD)):
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
        if any(m in hk["command"] for m in (_REGISTER_MOD, _REWAKE_MOD))
    ]


def test_worker_profile_gets_no_f810_edges():
    """A real (non-supervisor) worker profile: register/rewake absent entirely.

    WP-ARCH 3c K1 also removed the base D22 drain that used to appear on a
    worker's SessionStart, so a worker overlay now carries no seat-delivery
    command on ANY event."""
    h = _settings(role="developer")["hooks"]

    def cmds(event: str) -> list[str]:
        return [hk["command"] for b in h.get(event, []) for hk in b["hooks"]]

    assert not any(_REGISTER_MOD in c for e in h for c in cmds(e))
    assert not any(_REWAKE_MOD in c for e in h for c in cmds(e))
    assert not any(_DRAIN_MOD in c for e in h for c in cmds(e))
    assert not any(_ACK_MOD in c for e in h for c in cmds(e))


def test_worker_overlay_is_byte_equivalent_to_no_role():
    """`role=None` (unknown seat) and an explicit worker role produce the same
    overlay — the F810 gate keys on supervisor-ness, nothing else."""
    assert json.dumps(_settings(role=None), sort_keys=True) == json.dumps(
        _settings(role="developer"), sort_keys=True
    )


def test_worker_stop_block_matches_base_shape():
    """A worker's Stop carries only marker+turn (base shape) — no drain, ack or
    rewake leg. WP-ARCH 3c K1 removed the ack leg that used to sit in between."""
    h = _settings(role="reviewer")["hooks"]
    stop_cmds = [hk["command"] for b in h["Stop"] for hk in b["hooks"]]
    assert not any(_DRAIN_MOD in c or _ACK_MOD in c or _REWAKE_MOD in c for c in stop_cmds)


def test_supervisor_only_edges_present_for_supervisor():
    """The positive control: a supervisor DOES get the surviving F810 edges."""
    cmds = _f810_commands(_settings(role="supervisor"))
    assert any(_REGISTER_MOD in c for c in cmds)
    assert any(_REWAKE_MOD in c for c in cmds)


def test_concurrent_hook_claims_yield_a_single_winner(mailbox_db):
    """The server-side read-as-claim is the dedupe of last resort.

    Two concurrent ``list_messages(receiver, claim='hook')`` reads against ONE
    pending id: the UNIQUE(message_id, carrier) claim lets exactly one win and
    the other sees the id not at all. WP-ARCH 3c K1 removed the drain hook that
    used to be the second racer here, but the invariant is what makes any future
    hook-shaped reader safe, so it stays pinned."""
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


# WP-ARCH 3c K1/K5: the parent-repo ``supervisor-inbox-drain.sh`` /
# ``f213-callback-rewake.sh`` pair and the overlay drain they raced are both
# deleted, so the "two carriers, one id" co-execution arms that lived here
# (``test_parent_sh_and_overlay_have_distinct_command_strings``,
# ``test_actual_parent_sh_and_overlay_single_wake_persistent_row``) have no
# subject left. The server-side claim uniqueness they leaned on is still pinned
# by ``test_concurrent_hook_claims_yield_a_single_winner`` above.


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
