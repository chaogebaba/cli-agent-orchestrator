"""F747 (#747): native seat delivery is the default, not an opt-in.

Covers the four rulings:
  1. ``supervisor.teammate_push`` ships True.
  2. ``cc_team_inbox_path`` is derived for EVERY claude_code terminal regardless
     of any flag, with a cwd fallback, and re-derived on read when missing.
  3. The legacy fallback surface engages only with a typed reason, never while
     native delivery is healthy.
  4. The opt-in defaults batch is flipped in the config table.

Each ruling carries a named mutant test that fails when the pre-F747 behaviour
is restored.
"""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import config_service as cs
from cli_agent_orchestrator.services import teammate_push_service as tps
from cli_agent_orchestrator.services.config_service import ConfigService
from cli_agent_orchestrator.services.terminal_service import _maybe_derive_cc_team_inbox_path


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Same isolation as test_config_service: no real settings.json, no env."""
    fake_settings = tmp_path / "settings.json"
    fake_legacy = tmp_path / "config.json"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake_settings
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(cs, "LEGACY_CONFIG_FILE", fake_legacy)
    for env_name in cs.ENV_REGISTRY:
        monkeypatch.delenv(env_name, raising=False)
    tps._native_write_failures.clear()
    tps._native_fallback_last_warn.clear()
    yield {"settings": fake_settings, "legacy": fake_legacy}
    tps._native_write_failures.clear()
    tps._native_fallback_last_warn.clear()


# ---------------------------------------------------------------------------
# Ruling 4 — shipped defaults are the values the operator actually runs.
# ---------------------------------------------------------------------------

FLIPPED_ON = [
    "apps.enabled",
    "supervisor.mailbox_pull",
    "supervisor.teammate_push",
    "supervisor.watchdog.quiescence",
]


@pytest.mark.parametrize("path", FLIPPED_ON)
def test_flipped_keys_default_true(path):
    assert ConfigService.get(path) is True, f"{path} must ship enabled"


def test_doorbell_defaults_false():
    """The seat prompt is never a message tunnel."""
    assert ConfigService.get("supervisor.doorbell") is False


def test_memory_stays_off():
    assert ConfigService.get("memory.enabled") is False


@pytest.mark.parametrize(
    "env_name,expected",
    [
        ("CAO_SUPERVISOR_WAKE_WS_MONITOR", False),
        ("CAO_DELIVERY_PHASE", "shadow"),
        ("CAO_WORKER_TRUTH_INGEST", None),
    ],
)
def test_untouched_keys_keep_their_table_value(env_name, expected):
    """ws_monitor stays ship-dark and F883 owns delivery.phase.

    Asserted against the table rather than ``get()``: these paths are not in
    ``_OWNED_DEFAULTS``, so their RESOLUTION is unchanged by this batch and
    reading them through ``get()`` would test the pre-existing fall-through,
    not the shipped default.
    """
    if env_name not in cs.ENV_REGISTRY:
        pytest.skip(f"{env_name} is not a registry path")
    assert cs.ENV_REGISTRY[env_name][2] == expected


def test_shipped_default_beats_call_site_default():
    """The shipped default wins over whatever a call site happens to pass.

    MUTANT (ruling 4): drop the flipped keys from ``_OWNED_DEFAULTS`` and
    ``_get_value`` falls through to the caller's ``default=``, which is how
    ``supervisor.teammate_push`` read falsy no matter what the table declared.
    """
    assert ConfigService.get("supervisor.teammate_push", default=False) is True
    assert ConfigService.get("supervisor.doorbell", default=True) is False


@pytest.mark.parametrize("path", FLIPPED_ON + ["supervisor.doorbell"])
def test_owned_default_agrees_with_the_env_registry_tuple(path):
    """The two tables must not drift: get() reads one, `cao config list` the other."""
    env_name = cs._PATH_TO_ENV[path]
    assert cs._OWNED_DEFAULTS[path] is cs.ENV_REGISTRY[env_name][2]


def test_file_and_env_still_beat_the_registry_default(monkeypatch, _isolated_settings):
    _isolated_settings["settings"].write_text(json.dumps({"supervisor": {"teammate_push": False}}))
    assert ConfigService.get("supervisor.teammate_push") is False
    monkeypatch.setenv("CAO_W2M_TEAMMATE_PUSH", "true")
    assert ConfigService.get("supervisor.teammate_push") is True


# ---------------------------------------------------------------------------
# Ruling 2 — create-time derivation is ungated and cwd-tolerant.
# ---------------------------------------------------------------------------


def _flags_off(monkeypatch):
    monkeypatch.setattr(
        ConfigService,
        "get",
        staticmethod(lambda path, default=None, override=None: False),
    )


def test_inbox_path_derived_with_every_flag_off(monkeypatch):
    """MUTANT (ruling 2): restore the flag-gated derivation and this fails."""
    _flags_off(monkeypatch)
    md = _maybe_derive_cc_team_inbox_path("claude_code", None, "/home/x/repo")
    assert md is not None
    assert md["cc_team_inbox_path"].endswith("/team-lead.json")
    assert "-home-x-repo" in md["cc_team_inbox_path"]


def test_inbox_path_derived_into_existing_metadata(monkeypatch):
    _flags_off(monkeypatch)
    md = _maybe_derive_cc_team_inbox_path("claude_code", {"group": "a"}, "/home/x/repo")
    assert md is not None and "cc_team_inbox_path" in md and md["group"] == "a"


def test_inbox_path_derived_when_working_directory_is_none(monkeypatch):
    """A seat created with working_directory=None still gets a usable path."""
    _flags_off(monkeypatch)
    monkeypatch.setattr("os.getcwd", lambda: "/srv/fallback")
    md = _maybe_derive_cc_team_inbox_path("claude_code", None, None)
    assert md is not None and "-srv-fallback" in md["cc_team_inbox_path"]


def test_existing_inbox_path_never_overwritten(monkeypatch):
    _flags_off(monkeypatch)
    md = _maybe_derive_cc_team_inbox_path("claude_code", {"cc_team_inbox_path": "/keep.json"}, "/x")
    assert md == {"cc_team_inbox_path": "/keep.json"}


def test_non_claude_code_provider_gets_no_path(monkeypatch):
    _flags_off(monkeypatch)
    assert _maybe_derive_cc_team_inbox_path("codex", None, "/home/x/repo") is None


def test_resolve_inbox_path_self_heals_from_the_recorded_cwd(monkeypatch):
    """Read-path re-derivation for a row that has a cwd but no persisted path."""
    monkeypatch.setattr(
        tps,
        "get_terminal_metadata",
        lambda tid: {
            "provider": "claude_code",
            "working_directory": "/home/x/repo",
            "metadata": {},
        },
    )
    persisted: dict = {}
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.update_terminal_metadata",
        lambda tid, md: persisted.update(md),
    )
    path = tps._resolve_inbox_path("t1")
    assert path is not None and "-home-x-repo" in str(path)
    assert "cc_team_inbox_path" in persisted


def test_resolve_inbox_path_never_invents_a_shared_path(monkeypatch):
    """MUTANT: restore the os.getcwd() fallback and every pathless terminal
    derives the SAME inbox file, so unrelated seats serialise on one lockfile."""
    monkeypatch.setattr(
        tps,
        "get_terminal_metadata",
        lambda tid: {"provider": "claude_code", "working_directory": None, "metadata": {}},
    )
    assert tps._resolve_inbox_path("t1") is None
    assert tps._resolve_inbox_path("t2") is None


# ---------------------------------------------------------------------------
# Rulings 1 + 3 — push by default; fallback only with a typed reason.
# ---------------------------------------------------------------------------


def _healthy_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tps,
        "get_terminal_metadata",
        lambda tid: {
            "provider": "claude_code",
            "working_directory": str(tmp_path),
            "metadata": {"cc_team_inbox_path": str(tmp_path / "team-lead.json")},
        },
    )


def test_should_teammate_push_is_true_by_default(monkeypatch, tmp_path):
    """Ruling 1: no settings.json, no env, and the seat still pushes natively."""
    _healthy_terminal(monkeypatch, tmp_path)
    assert tps._should_teammate_push("t1") is True


def test_native_fallback_reason_none_when_healthy(monkeypatch, tmp_path):
    _healthy_terminal(monkeypatch, tmp_path)
    assert tps.native_fallback_reason("t1") is None


def test_reason_push_disabled_by_operator(monkeypatch, tmp_path, _isolated_settings):
    _healthy_terminal(monkeypatch, tmp_path)
    monkeypatch.setenv("CAO_W2M_TEAMMATE_PUSH", "false")
    assert tps.native_fallback_reason("t1") == "push_disabled_by_operator"


def test_reason_no_inbox_path(monkeypatch):
    monkeypatch.setattr(
        tps, "get_terminal_metadata", lambda tid: {"provider": "claude_code", "metadata": {}}
    )
    monkeypatch.setattr(tps, "_resolve_inbox_path", lambda tid, **kw: None)
    assert tps.native_fallback_reason("t1") == "no_inbox_path"


def test_health_probe_never_writes_metadata(monkeypatch, tmp_path):
    """MUTANT (perf): drop ``persist=False`` and the probe writes the DB on every
    seat tool call, which serialized the whole suite behind one SQLite lock."""
    monkeypatch.setattr(
        tps,
        "get_terminal_metadata",
        lambda tid: {"provider": "claude_code", "working_directory": str(tmp_path), "metadata": {}},
    )
    writes: list = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.update_terminal_metadata",
        lambda tid, md: writes.append(tid),
    )
    tps.native_fallback_reason("t1")
    assert writes == []


def test_reason_native_write_failed(monkeypatch, tmp_path):
    _healthy_terminal(monkeypatch, tmp_path)
    tps.record_native_write_failure("t1")
    assert tps.native_fallback_reason("t1") == "native_write_failed"
    tps.clear_native_write_failure("t1")
    assert tps.native_fallback_reason("t1") is None


def test_reason_no_native_driver(monkeypatch, tmp_path):
    _healthy_terminal(monkeypatch, tmp_path)
    monkeypatch.setenv("CAO_SUPERVISOR_MAILBOX_PULL", "false")
    monkeypatch.setenv("CAO_DELIVERY_SEAT_WAKE_RECONCILE", "false")
    assert tps.native_fallback_reason("t1") == "no_native_driver"


def test_reason_provider_not_native(monkeypatch):
    monkeypatch.setattr(tps, "get_terminal_metadata", lambda tid: {"provider": "codex"})
    assert tps.native_fallback_reason("t1") == "provider_not_native"


@pytest.mark.parametrize("reason", list(tps.NATIVE_FALLBACK_REASONS))
def test_every_reason_is_in_the_closed_set(reason):
    assert isinstance(reason, str) and reason


def test_write_failure_ttl_expires(monkeypatch, tmp_path):
    _healthy_terminal(monkeypatch, tmp_path)
    tps.record_native_write_failure("t1")
    base = tps.time.monotonic()
    monkeypatch.setattr(tps.time, "monotonic", lambda: base + tps.NATIVE_WRITE_FAILURE_TTL_S + 1)
    assert tps.native_fallback_reason("t1") is None


def test_push_outcome_arms_and_disarms_the_fallback(monkeypatch, tmp_path):
    """MUTANT (ruling 3): drop the record/clear calls and the fallback either
    never arms on a broken write or never disarms after a good one."""
    _healthy_terminal(monkeypatch, tmp_path)
    monkeypatch.setattr(tps, "get_mailbox_consumption_cursor", lambda tid: None)

    class _Msg:
        id = 7
        sender_id = "w1"
        message = "hello"
        logical_receiver_id = "mb1"

    monkeypatch.setattr(tps, "_write_inbox_entry", lambda p, e: False)
    out = tps.attempt_teammate_push_reported("t1", [_Msg()])
    assert out.reason == "write_failed"
    assert tps.native_fallback_reason("t1") == "native_write_failed"

    monkeypatch.setattr(tps, "_write_inbox_entry", lambda p, e: True)
    out = tps.attempt_teammate_push_reported("t1", [_Msg()])
    assert out.pushed is True
    assert tps.native_fallback_reason("t1") is None


def test_engagement_warn_is_rate_limited(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger=tps.logger.name)
    assert tps.log_native_fallback_engaged("t1", "no_inbox_path") is True
    assert tps.log_native_fallback_engaged("t1", "no_inbox_path") is False
    lines = [r.getMessage() for r in caplog.records if "native_fallback_engaged" in r.getMessage()]
    assert len(lines) == 1
    assert "terminal=t1" in lines[0] and "reason=no_inbox_path" in lines[0]


# ---------------------------------------------------------------------------
# Ruling 3 — the hook surfaces stay silent while native delivery is healthy.
# ---------------------------------------------------------------------------


def _fake_response(payload, ok=True):
    class _R:
        def raise_for_status(self):
            if not ok:
                raise RuntimeError("boom")

        def json(self):
            return payload

    return _R()


def test_rewake_hook_does_not_wake_when_native_is_healthy(monkeypatch):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setattr(rewake.cao_http, "get", lambda *a, **k: _fake_response({"healthy": True}))
    assert rewake._native_delivery_healthy("t1", "http://x", {}) is True


def test_rewake_hook_fails_open_to_the_fallback(monkeypatch):
    """A probe error must arm the net, never silence it."""
    from cli_agent_orchestrator.hooks import rewake

    def _boom(*a, **k):
        raise RuntimeError("server down")

    monkeypatch.setattr(rewake.cao_http, "get", _boom)
    assert rewake._native_delivery_healthy("t1", "http://x", {}) is False


def test_drain_hook_skips_the_digest_when_native_is_healthy(monkeypatch):
    from cli_agent_orchestrator.hooks import supervisor_drain

    monkeypatch.setattr(
        supervisor_drain.cao_http, "get", lambda *a, **k: _fake_response({"healthy": True})
    )
    assert supervisor_drain._native_delivery_healthy("t1", "http://x", {}) is True

    monkeypatch.setattr(
        supervisor_drain.cao_http,
        "get",
        lambda *a, **k: _fake_response({"healthy": False, "reason": "no_inbox_path"}),
    )
    assert supervisor_drain._native_delivery_healthy("t1", "http://x", {}) is False


def test_session_start_always_sends_a_cwd():
    """MUTANT: restore the ``if working_directory:`` guard and the server
    persists its own cwd for a seat launched without --cwd."""
    src = Path("src/cli_agent_orchestrator/cli/commands/session.py").read_text(encoding="utf-8")
    assert 'params["working_directory"] = working_directory or os.getcwd()' in src


# ---------------------------------------------------------------------------
# The starvation the flag and the path never explained (#747 follow-up).
#
# Observed live: teammate_push=true, cc_team_inbox_path present on the seat, and
# cao-server STILL wrote nothing to team-lead.json for messages 5644-5647; the
# legacy "CAO callback waiting" wake fired instead. The mailbox indirection is
# not the culprit -- a row addressed to a mailbox id resolves to the seat and
# keeps the mailbox as logical_receiver_id, which is exactly what the pull-mode
# reconciler selects on. The culprit is the FALLBACK SURFACE ITSELF: the drain
# hook fires on every turn edge, claims and acks the rows inside the
# reconciler's grace window, and the native push's send-time recount against
# consumed_through_id then finds every message already consumed.
# ---------------------------------------------------------------------------


def test_mailbox_addressed_row_keeps_the_mailbox_as_logical_receiver():
    """The mb_ indirection does NOT bypass the reconciler's selection axis."""
    import inspect

    from cli_agent_orchestrator.clients import database as db_mod

    src = inspect.getsource(db_mod.resolve_inbox_receiver)
    # receiver cache becomes the terminal; the mailbox id becomes logical.
    assert "mailbox.current_terminal_id" in src
    assert "cast(str, mailbox.id)" in src


def test_push_reports_consumed_when_the_hook_already_acked(monkeypatch, tmp_path):
    """The exact live failure: nothing is written and the reason is `consumed`."""
    _healthy_terminal(monkeypatch, tmp_path)
    monkeypatch.setattr(tps, "get_mailbox_consumption_cursor", lambda tid: 5647)
    wrote: list = []
    monkeypatch.setattr(tps, "_write_inbox_entry", lambda p, e: wrote.append(p) or True)

    class _Msg:
        id = 5644
        sender_id = "w1"
        message = "callback"
        logical_receiver_id = "mb_d176ebe0"

    out = tps.attempt_teammate_push_reported("t1", [_Msg()])
    assert out.pushed is False and out.reason == "consumed"
    assert wrote == []


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_hook_claim_is_suppressed_while_native_is_healthy(monkeypatch):
    """MUTANT (#747 follow-up): drop the server-side gate and the drain hook
    keeps claiming + acking inside the grace window, so the native push always
    recounts to `consumed` and the seat never gets an agent message."""
    from cli_agent_orchestrator.api import main as api_main
    from cli_agent_orchestrator.security.auth import SCOPE_WRITE

    called: list = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        lambda *a, **k: called.append(a) or {"items": [{"id": 1}]},
    )
    monkeypatch.setattr(tps, "native_fallback_reason", lambda tid: None)

    out = _run(api_main.list_messages_endpoint(to="c244d80b", claim="hook", _scopes=[SCOPE_WRITE]))
    assert out == {"items": [], "next_after_id": None, "has_more": False}
    assert called == [], "the hook must not claim while native owns the seat"


def test_hook_claim_passes_through_when_native_is_broken(monkeypatch):
    from cli_agent_orchestrator.api import main as api_main
    from cli_agent_orchestrator.security.auth import SCOPE_WRITE

    called: list = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        lambda *a, **k: called.append(k.get("claim")) or {"items": [{"id": 1}]},
    )
    monkeypatch.setattr(tps, "native_fallback_reason", lambda tid: "no_inbox_path")

    out = _run(api_main.list_messages_endpoint(to="c244d80b", claim="hook", _scopes=[SCOPE_WRITE]))
    assert out["items"] == [{"id": 1}]
    assert called == ["hook"]


def test_hook_claim_gate_resolves_a_mailbox_address(monkeypatch):
    """The drain may address the mailbox; the gate must probe the SEAT."""
    from cli_agent_orchestrator.api import main as api_main
    from cli_agent_orchestrator.security.auth import SCOPE_WRITE

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_current_mailbox_terminal",
        lambda mbid: "c244d80b",
    )
    probed: list = []
    monkeypatch.setattr(tps, "native_fallback_reason", lambda tid: probed.append(tid) or None)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        lambda *a, **k: {"items": [{"id": 1}]},
    )

    out = _run(
        api_main.list_messages_endpoint(to="mb_d176ebe0", claim="hook", _scopes=[SCOPE_WRITE])
    )
    assert probed == ["c244d80b"]
    assert out["items"] == []


def test_non_hook_claims_are_never_gated(monkeypatch):
    from cli_agent_orchestrator.api import main as api_main
    from cli_agent_orchestrator.security.auth import SCOPE_WRITE

    seen: list = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        lambda *a, **k: seen.append(k.get("claim")) or {"items": []},
    )
    monkeypatch.setattr(tps, "native_fallback_reason", lambda tid: None)
    _run(api_main.list_messages_endpoint(to="c244d80b", claim="mcp", _scopes=[SCOPE_WRITE]))
    assert seen == ["mcp"]


# ---------------------------------------------------------------------------
# The push path must not get more expensive the more it is used (#747 r3).
# ---------------------------------------------------------------------------


def test_inbox_file_is_capped(tmp_path):
    """MUTANT: drop the trim and the file grows without bound, so every push
    re-reads and re-serialises a longer array -- O(M**2) over M pushes."""
    inbox = tmp_path / "team-lead.json"
    for i in range(tps.INBOX_ENTRIES_CAP + 25):
        assert tps._write_inbox_entry(inbox, {"msg_id": f"m{i}", "n": i}) is True
    entries = json.loads(inbox.read_text(encoding="utf-8"))
    assert len(entries) == tps.INBOX_ENTRIES_CAP


def test_trim_drops_oldest_and_keeps_newest(tmp_path):
    """Trimming is oldest-first: the newest entry is always still there."""
    inbox = tmp_path / "team-lead.json"
    for i in range(tps.INBOX_ENTRIES_CAP + 10):
        tps._write_inbox_entry(inbox, {"msg_id": f"m{i}", "n": i})
    entries = json.loads(inbox.read_text(encoding="utf-8"))
    ids = [e["msg_id"] for e in entries]
    assert ids[-1] == f"m{tps.INBOX_ENTRIES_CAP + 9}"
    assert "m0" not in ids


def test_under_the_cap_nothing_is_dropped(tmp_path):
    inbox = tmp_path / "team-lead.json"
    for i in range(5):
        tps._write_inbox_entry(inbox, {"msg_id": f"m{i}", "n": i})
    entries = json.loads(inbox.read_text(encoding="utf-8"))
    assert [e["msg_id"] for e in entries] == ["m0", "m1", "m2", "m3", "m4"]


def test_contended_lock_is_transient_not_a_broken_channel(monkeypatch, tmp_path):
    """MUTANT: return False instead of None on a contended lock and a transient
    race arms the write-failure ledger, so the fallback surface engages on a
    healthy seat -- exactly what ruling 3 forbids."""
    _healthy_terminal(monkeypatch, tmp_path)
    monkeypatch.setattr(tps, "get_mailbox_consumption_cursor", lambda tid: None)
    monkeypatch.setattr(tps, "_acquire_lockfile_deadline", lambda p, d: None)

    class _Msg:
        id = 9
        sender_id = "w1"
        message = "hi"
        logical_receiver_id = "mb1"

    out = tps.attempt_teammate_push_reported("t1", [_Msg()])
    assert out.pushed is False and out.reason == "lock_contended"
    assert tps.native_fallback_reason("t1") is None


def test_lock_wait_is_short(tmp_path):
    """The wait is a bounded courtesy, not a second of blocking per push."""
    assert tps.INBOX_LOCK_WAIT_S <= 0.25
