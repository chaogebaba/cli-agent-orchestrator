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
import os
import time
from pathlib import Path
from typing import Any, Iterator, cast

import pytest

from cli_agent_orchestrator.services import config_service as cs
from cli_agent_orchestrator.services import teammate_push_service as tps
from cli_agent_orchestrator.services.config_service import ConfigService
from cli_agent_orchestrator.services.terminal_service import _maybe_derive_cc_team_inbox_path


@pytest.fixture(autouse=True)
def _isolated_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
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
def test_flipped_keys_default_true(path: str) -> None:
    assert ConfigService.get(path) is True, f"{path} must ship enabled"


def test_doorbell_defaults_false() -> None:
    """The seat prompt is never a message tunnel."""
    assert ConfigService.get("supervisor.doorbell") is False


def test_memory_stays_off() -> None:
    assert ConfigService.get("memory.enabled") is False


@pytest.mark.parametrize(
    "env_name,expected",
    [
        ("CAO_SUPERVISOR_WAKE_WS_MONITOR", False),
        ("CAO_DELIVERY_PHASE", "shadow"),
        ("CAO_WORKER_TRUTH_INGEST", None),
    ],
)
def test_untouched_keys_keep_their_table_value(env_name: str, expected: object) -> None:
    """ws_monitor stays ship-dark and F883 owns delivery.phase.

    Asserted against the table rather than ``get()``: these paths are not in
    ``_OWNED_DEFAULTS``, so their RESOLUTION is unchanged by this batch and
    reading them through ``get()`` would test the pre-existing fall-through,
    not the shipped default.
    """
    if env_name not in cs.ENV_REGISTRY:
        pytest.skip(f"{env_name} is not a registry path")
    assert cs.ENV_REGISTRY[env_name][2] == expected


def test_shipped_default_beats_call_site_default() -> None:
    """The shipped default wins over whatever a call site happens to pass.

    MUTANT (ruling 4): drop the flipped keys from ``_OWNED_DEFAULTS`` and
    ``_get_value`` falls through to the caller's ``default=``, which is how
    ``supervisor.teammate_push`` read falsy no matter what the table declared.
    """
    assert ConfigService.get("supervisor.teammate_push", default=False) is True
    assert ConfigService.get("supervisor.doorbell", default=True) is False


@pytest.mark.parametrize("path", FLIPPED_ON + ["supervisor.doorbell"])
def test_owned_default_agrees_with_the_env_registry_tuple(path: str) -> None:
    """The two tables must not drift: get() reads one, `cao config list` the other."""
    env_name = cs._PATH_TO_ENV[path]
    assert cs._OWNED_DEFAULTS[path] is cs.ENV_REGISTRY[env_name][2]


def test_file_and_env_still_beat_the_registry_default(
    monkeypatch: pytest.MonkeyPatch, _isolated_settings: dict[str, Path]
) -> None:
    _isolated_settings["settings"].write_text(json.dumps({"supervisor": {"teammate_push": False}}))
    assert ConfigService.get("supervisor.teammate_push") is False
    monkeypatch.setenv("CAO_W2M_TEAMMATE_PUSH", "true")
    assert ConfigService.get("supervisor.teammate_push") is True


# ---------------------------------------------------------------------------
# Ruling 2 — create-time derivation is ungated and cwd-tolerant.
# ---------------------------------------------------------------------------


def _flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ConfigService,
        "get",
        staticmethod(lambda path, default=None, override=None: False),
    )


def test_inbox_path_derived_with_every_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """MUTANT (ruling 2): restore the flag-gated derivation and this fails."""
    _flags_off(monkeypatch)
    md = _maybe_derive_cc_team_inbox_path("claude_code", None, "/home/x/repo")
    assert md is not None
    assert md["cc_team_inbox_path"].endswith("/team-lead.json")
    assert "-home-x-repo" in md["cc_team_inbox_path"]


def test_inbox_path_derived_into_existing_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags_off(monkeypatch)
    md = _maybe_derive_cc_team_inbox_path("claude_code", {"group": "a"}, "/home/x/repo")
    assert md is not None and "cc_team_inbox_path" in md and md["group"] == "a"


def test_inbox_path_derived_when_working_directory_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A seat created with working_directory=None still gets a usable path."""
    _flags_off(monkeypatch)
    monkeypatch.setattr("os.getcwd", lambda: "/srv/fallback")
    md = _maybe_derive_cc_team_inbox_path("claude_code", None, None)
    assert md is not None and "-srv-fallback" in md["cc_team_inbox_path"]


def test_existing_inbox_path_never_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags_off(monkeypatch)
    md = _maybe_derive_cc_team_inbox_path("claude_code", {"cc_team_inbox_path": "/keep.json"}, "/x")
    assert md == {"cc_team_inbox_path": "/keep.json"}


def test_non_claude_code_provider_gets_no_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags_off(monkeypatch)
    assert _maybe_derive_cc_team_inbox_path("codex", None, "/home/x/repo") is None


def test_resolve_inbox_path_self_heals_from_the_recorded_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    persisted: dict[str, object] = {}
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.update_terminal_metadata",
        lambda tid, md: persisted.update(md),
    )
    path = tps._resolve_inbox_path("t1")
    assert path is not None and "-home-x-repo" in str(path)
    assert "cc_team_inbox_path" in persisted


def test_resolve_inbox_path_never_invents_a_shared_path(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _healthy_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        tps,
        "get_terminal_metadata",
        lambda tid: {
            "provider": "claude_code",
            "working_directory": str(tmp_path),
            "metadata": {"cc_team_inbox_path": str(tmp_path / "team-lead.json")},
        },
    )


def test_should_teammate_push_is_true_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ruling 1: no settings.json, no env, and the seat still pushes natively."""
    _healthy_terminal(monkeypatch, tmp_path)
    assert tps._should_teammate_push("t1") is True


def test_native_fallback_reason_none_when_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _healthy_terminal(monkeypatch, tmp_path)
    assert tps.native_fallback_reason("t1") is None


def test_reason_push_disabled_by_operator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_settings: dict[str, Path]
) -> None:
    _healthy_terminal(monkeypatch, tmp_path)
    monkeypatch.setenv("CAO_W2M_TEAMMATE_PUSH", "false")
    assert tps.native_fallback_reason("t1") == "push_disabled_by_operator"


def test_reason_no_inbox_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tps, "get_terminal_metadata", lambda tid: {"provider": "claude_code", "metadata": {}}
    )
    monkeypatch.setattr(tps, "_resolve_inbox_path", lambda tid, **kw: None)
    assert tps.native_fallback_reason("t1") == "no_inbox_path"


def test_health_probe_never_writes_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MUTANT (perf): drop ``persist=False`` and the probe writes the DB on every
    seat tool call, which serialized the whole suite behind one SQLite lock."""
    monkeypatch.setattr(
        tps,
        "get_terminal_metadata",
        lambda tid: {"provider": "claude_code", "working_directory": str(tmp_path), "metadata": {}},
    )
    writes: list[str] = []

    def _record_persist(tid: str, md: object) -> None:
        writes.append(tid)

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.update_terminal_metadata",
        _record_persist,
    )
    tps.native_fallback_reason("t1")
    assert writes == []


def test_reason_native_write_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _healthy_terminal(monkeypatch, tmp_path)
    tps.record_native_write_failure("t1")
    assert tps.native_fallback_reason("t1") == "native_write_failed"
    tps.clear_native_write_failure("t1")
    assert tps.native_fallback_reason("t1") is None


def test_reason_no_native_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # WP-ARCH 3c K6 deleted the idle-seat wake reconcile, so the pull-mode
    # reconciler is the only remaining driver of a legacy native push.
    _healthy_terminal(monkeypatch, tmp_path)
    monkeypatch.setenv("CAO_SUPERVISOR_MAILBOX_PULL", "false")
    assert tps.native_fallback_reason("t1") == "no_native_driver"


def test_reason_provider_not_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tps, "get_terminal_metadata", lambda tid: {"provider": "codex"})
    assert tps.native_fallback_reason("t1") == "provider_not_native"


@pytest.mark.parametrize("reason", list(tps.NATIVE_FALLBACK_REASONS))
def test_every_reason_is_in_the_closed_set(reason: str) -> None:
    assert isinstance(reason, str) and reason


def test_write_failure_ttl_expires(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _healthy_terminal(monkeypatch, tmp_path)
    tps.record_native_write_failure("t1")
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + tps.NATIVE_WRITE_FAILURE_TTL_S + 1)
    assert tps.native_fallback_reason("t1") is None


def test_push_outcome_arms_and_disarms_the_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    out = tps.attempt_teammate_push_reported("t1", [cast(Any, _Msg())])
    assert out.reason == "write_failed"
    assert tps.native_fallback_reason("t1") == "native_write_failed"

    monkeypatch.setattr(tps, "_write_inbox_entry", lambda p, e: True)
    out = tps.attempt_teammate_push_reported("t1", [cast(Any, _Msg())])
    assert out.pushed is True
    assert tps.native_fallback_reason("t1") is None


def test_engagement_warn_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
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


def _fake_response(payload: object, ok: bool = True) -> Any:
    class _R:
        def raise_for_status(self) -> None:
            if not ok:
                raise RuntimeError("boom")

        def json(self) -> object:
            return payload

    return _R()


def test_rewake_hook_does_not_wake_when_native_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setattr(rewake.cao_http, "get", lambda *a, **k: _fake_response({"healthy": True}))
    assert rewake._native_delivery_healthy("t1", "http://x", {}) is True


def test_rewake_hook_fails_open_to_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe error must arm the net, never silence it."""
    from cli_agent_orchestrator.hooks import rewake

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("server down")

    monkeypatch.setattr(rewake.cao_http, "get", _boom)
    assert rewake._native_delivery_healthy("t1", "http://x", {}) is False


def test_session_start_always_sends_a_cwd() -> None:
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


def test_mailbox_addressed_row_keeps_the_mailbox_as_logical_receiver() -> None:
    """The mb_ indirection does NOT bypass the reconciler's selection axis."""
    import inspect

    from cli_agent_orchestrator.clients import database as db_mod

    src = inspect.getsource(db_mod.resolve_inbox_receiver)
    # receiver cache becomes the terminal; the mailbox id becomes logical.
    assert "mailbox.current_terminal_id" in src
    assert "cast(str, mailbox.id)" in src


def test_push_reports_consumed_when_the_hook_already_acked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact live failure: nothing is written and the reason is `consumed`."""
    _healthy_terminal(monkeypatch, tmp_path)
    monkeypatch.setattr(tps, "get_mailbox_consumption_cursor", lambda tid: 5647)
    wrote: list[Path] = []

    def _record_write(path: Path, entry: object) -> bool:
        wrote.append(path)
        return True

    monkeypatch.setattr(tps, "_write_inbox_entry", _record_write)

    class _Msg:
        id = 5644
        sender_id = "w1"
        message = "callback"
        logical_receiver_id = "mb_d176ebe0"

    out = tps.attempt_teammate_push_reported("t1", [cast(Any, _Msg())])
    assert out.pushed is False and out.reason == "consumed"
    assert wrote == []


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def test_hook_claim_is_suppressed_while_native_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """MUTANT (#747 follow-up): drop the server-side gate and the drain hook
    keeps claiming + acking inside the grace window, so the native push always
    recounts to `consumed` and the seat never gets an agent message."""
    from cli_agent_orchestrator.api import main as api_main
    from cli_agent_orchestrator.security.auth import SCOPE_WRITE

    called: list[object] = []

    def _record_args(*a: Any, **k: Any) -> dict[str, Any]:
        called.append(a)
        return {"items": [{"id": 1}]}

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        _record_args,
    )
    monkeypatch.setattr(tps, "native_fallback_reason", lambda tid: None)

    out = _run(api_main.list_messages_endpoint(to="c244d80b", claim="hook", _scopes=[SCOPE_WRITE]))
    assert out == {"items": [], "next_after_id": None, "has_more": False}
    assert called == [], "the hook must not claim while native owns the seat"


def test_hook_claim_passes_through_when_native_is_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli_agent_orchestrator.api import main as api_main
    from cli_agent_orchestrator.security.auth import SCOPE_WRITE

    called: list[object] = []

    def _record_claim(*a: Any, **k: Any) -> dict[str, Any]:
        called.append(k.get("claim"))
        return {"items": [{"id": 1}]}

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        _record_claim,
    )
    monkeypatch.setattr(tps, "native_fallback_reason", lambda tid: "no_inbox_path")

    out = _run(api_main.list_messages_endpoint(to="c244d80b", claim="hook", _scopes=[SCOPE_WRITE]))
    assert out["items"] == [{"id": 1}]
    assert called == ["hook"]


def test_hook_claim_gate_resolves_a_mailbox_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drain may address the mailbox; the gate must probe the SEAT."""
    from cli_agent_orchestrator.api import main as api_main
    from cli_agent_orchestrator.security.auth import SCOPE_WRITE

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_current_mailbox_terminal",
        lambda mbid: "c244d80b",
    )
    probed: list[str] = []

    def _record_probe(tid: str) -> str | None:
        probed.append(tid)
        return None

    monkeypatch.setattr(tps, "native_fallback_reason", _record_probe)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        lambda *a, **k: {"items": [{"id": 1}]},
    )

    out = _run(
        api_main.list_messages_endpoint(to="mb_d176ebe0", claim="hook", _scopes=[SCOPE_WRITE])
    )
    assert probed == ["c244d80b"]
    assert out["items"] == []


def test_non_hook_claims_are_never_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli_agent_orchestrator.api import main as api_main
    from cli_agent_orchestrator.security.auth import SCOPE_WRITE

    seen: list[object] = []

    def _record_seen(*a: Any, **k: Any) -> dict[str, Any]:
        seen.append(k.get("claim"))
        return {"items": []}

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        _record_seen,
    )
    monkeypatch.setattr(tps, "native_fallback_reason", lambda tid: None)
    _run(api_main.list_messages_endpoint(to="c244d80b", claim="mcp", _scopes=[SCOPE_WRITE]))
    assert seen == ["mcp"]


# ---------------------------------------------------------------------------
# The push path must not get more expensive the more it is used (#747 r3).
# ---------------------------------------------------------------------------


def test_inbox_file_is_capped(tmp_path: Path) -> None:
    """MUTANT: drop the trim and the file grows without bound, so every push
    re-reads and re-serialises a longer array -- O(M**2) over M pushes."""
    inbox = tmp_path / "team-lead.json"
    for i in range(tps.INBOX_ENTRIES_CAP + 25):
        assert tps._write_inbox_entry(inbox, {"msg_id": f"m{i}", "n": i}) is True
    entries = json.loads(inbox.read_text(encoding="utf-8"))
    assert len(entries) == tps.INBOX_ENTRIES_CAP


def test_trim_drops_oldest_and_keeps_newest(tmp_path: Path) -> None:
    """Trimming is oldest-first: the newest entry is always still there."""
    inbox = tmp_path / "team-lead.json"
    for i in range(tps.INBOX_ENTRIES_CAP + 10):
        tps._write_inbox_entry(inbox, {"msg_id": f"m{i}", "n": i})
    entries = json.loads(inbox.read_text(encoding="utf-8"))
    ids = [e["msg_id"] for e in entries]
    assert ids[-1] == f"m{tps.INBOX_ENTRIES_CAP + 9}"
    assert "m0" not in ids


def test_under_the_cap_nothing_is_dropped(tmp_path: Path) -> None:
    inbox = tmp_path / "team-lead.json"
    for i in range(5):
        tps._write_inbox_entry(inbox, {"msg_id": f"m{i}", "n": i})
    entries = json.loads(inbox.read_text(encoding="utf-8"))
    assert [e["msg_id"] for e in entries] == ["m0", "m1", "m2", "m3", "m4"]


def test_contended_push_is_transient_not_a_broken_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A contended lock must not be conflated with a failed write."""
    _healthy_terminal(monkeypatch, tmp_path)
    monkeypatch.setattr(tps, "get_mailbox_consumption_cursor", lambda tid: None)
    monkeypatch.setattr(tps, "_try_acquire_lockfile", lambda p: None)
    tps._inbox_contended_logged.clear()

    class _Msg:
        id = 9
        sender_id = "w1"
        message = "hi"
        logical_receiver_id = "mb1"

    out = tps.attempt_teammate_push_reported("t1", [cast(Any, _Msg())])
    assert out.pushed is False and out.reason == "inbox_contended"
    assert tps.native_fallback_reason("t1") is None


def test_contended_row_is_logged_once_not_once_per_tick(caplog: pytest.LogCaptureFixture) -> None:
    """The reconciler retries a contended row every tick; the log must not."""
    import logging

    tps._inbox_contended_logged.clear()
    caplog.set_level(logging.INFO, logger=tps.logger.name)
    for _ in range(5):
        tps._log_inbox_contended_once("t1", (7,))
    lines = [r.getMessage() for r in caplog.records if "inbox_contended" in r.getMessage()]
    assert len(lines) == 1
    assert "rows=7" in lines[0]


# --- the three probes the r3 ruling names ------------------------------------


def test_a_one_push_lands_the_other_is_contended_and_left_for_the_reconciler(
    tmp_path: Path,
) -> None:
    """(a) Two concurrent pushes to one inbox: one lands, one is contended.

    The contended one writes NOTHING, which is what leaves its row PENDING for
    the reconciler's next tick to carry.
    """
    inbox = tmp_path / "team-lead.json"
    lock = Path(str(inbox.resolve()) + ".lock")
    inbox.parent.mkdir(parents=True, exist_ok=True)

    first = tps._write_inbox_entry(inbox, {"msg_id": "a", "n": 1})
    assert first is True

    # Hold the lock exactly as a concurrent writer would.
    import os as _os

    held = _os.open(str(lock), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY, 0o644)
    try:
        second = tps._write_inbox_entry(inbox, {"msg_id": "b", "n": 2})
    finally:
        _os.close(held)
        lock.unlink(missing_ok=True)

    assert second is None, "a contended push reports contention, not success or failure"
    assert [e["msg_id"] for e in json.loads(inbox.read_text())] == ["a"]

    # The reconciler's next tick carries it, and then it lands.
    assert tps._write_inbox_entry(inbox, {"msg_id": "b", "n": 2}) is True
    assert [e["msg_id"] for e in json.loads(inbox.read_text())] == ["a", "b"]


def test_b_a_contended_push_never_blocks(tmp_path: Path) -> None:
    """(b) MUTANT: restore the 1s blocking acquire and this wall-clock bound fails.

    Two attempts plus one short pause, so the whole contended path is bounded by
    a small multiple of the retry pause -- never the second it used to cost.
    """
    inbox = tmp_path / "team-lead.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(inbox.resolve()) + ".lock")
    import os as _os
    import time as _time

    held = _os.open(str(lock), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY, 0o644)
    try:
        started = _time.monotonic()
        assert tps._write_inbox_entry(inbox, {"msg_id": "x"}) is None
        elapsed = _time.monotonic() - started
    finally:
        _os.close(held)
        lock.unlink(missing_ok=True)

    budget = tps.INBOX_LOCK_RETRY_PAUSE_S * 4 + 0.2
    assert elapsed < budget, f"contended push took {elapsed:.3f}s, budget {budget:.3f}s"


def test_c_an_uncontended_push_is_still_synchronous(tmp_path: Path) -> None:
    """The ruling keeps the fast path fast: no pause when nothing contends."""
    import time as _time

    inbox = tmp_path / "team-lead.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    started = _time.monotonic()
    assert tps._write_inbox_entry(inbox, {"msg_id": "only"}) is True
    assert (_time.monotonic() - started) < tps.INBOX_LOCK_RETRY_PAUSE_S


# ---------------------------------------------------------------------------
# Cache eviction must not scale with the number of terminals ever seen (#747 r4).
# ---------------------------------------------------------------------------


def test_invalidate_does_not_scan_the_whole_terminal_cache() -> None:
    """MUTANT: put the __session__ keys back in the shared dict and restore the
    `for k in list(cache)` scan, and this cost curve bends with cache size.

    28 call sites invalidate on mutation, and the cache has no size eviction, so
    an O(cache) eviction is a session-length cost curve behind an O(1)-looking
    dict.
    """
    import time as _time

    from cli_agent_orchestrator.clients import database as db_mod

    db_mod.clear_terminal_metadata_cache()
    try:
        for i in range(200):
            db_mod._terminal_metadata_cache[f"t{i}"] = (_time.monotonic(), {"id": i})
        small = _time.monotonic()
        for i in range(200):
            db_mod.invalidate_terminal_metadata_cache(f"t{i}")
        small_elapsed = _time.monotonic() - small

        for i in range(20000):
            db_mod._terminal_metadata_cache[f"t{i}"] = (_time.monotonic(), {"id": i})
        big = _time.monotonic()
        for i in range(200):
            db_mod.invalidate_terminal_metadata_cache(f"t{i}")
        big_elapsed = _time.monotonic() - big
    finally:
        db_mod.clear_terminal_metadata_cache()

    # An ABSOLUTE bound, not a ratio. 200 evictions against a 20k-entry cache
    # are ~0.2 ms when eviction is O(1). Restoring the prefix scan makes each
    # one copy the whole dict with list(), which is ~100 ms for the same 200 --
    # a ratio bound wide enough to absorb timing noise would not catch that, so
    # the bound is stated in absolute terms.
    assert big_elapsed < 0.05, (
        f"eviction scaled with cache size: {small_elapsed:.4f}s at 200 entries, "
        f"{big_elapsed:.4f}s at 20000 (budget 0.05s)"
    )


def test_invalidate_still_drops_the_terminal_and_session_entries() -> None:
    """The O(1) split must not change what an invalidation actually evicts."""
    import time as _time

    from cli_agent_orchestrator.clients import database as db_mod

    db_mod.clear_terminal_metadata_cache()
    try:
        db_mod._terminal_metadata_cache["keep"] = (_time.monotonic(), {"id": "keep"})
        db_mod._terminal_metadata_cache["gone"] = (_time.monotonic(), {"id": "gone"})
        db_mod._session_metadata_cache["__session__s1"] = (_time.monotonic(), ["x"])
        db_mod.invalidate_terminal_metadata_cache("gone")
        assert "gone" not in db_mod._terminal_metadata_cache
        assert "keep" in db_mod._terminal_metadata_cache
        assert db_mod._session_metadata_cache == {}
    finally:
        db_mod.clear_terminal_metadata_cache()


# ---------------------------------------------------------------------------
# A permanent lock error is a BROKEN channel, not a busy one (r7 repair 1).
# ---------------------------------------------------------------------------


def test_permanent_lock_error_is_a_write_failure_not_contention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MUTANT: swallow the PermissionError back into `return None` and this fails.

    Verdict r1's blocker: every OSError from os.open became None, the caller read
    that as inbox_contended, the write-failure ledger was never armed, and
    native_fallback_reason kept answering healthy -- so the hook gate suppressed
    the fallback indefinitely for a seat whose inbox can never be written.
    """
    _healthy_terminal(monkeypatch, tmp_path)
    monkeypatch.setattr(tps, "get_mailbox_consumption_cursor", lambda tid: None)
    tps.clear_native_write_failure("t1")

    real_open = os.open

    def _deny(path: Any, *a: Any, **k: Any) -> Any:
        if str(path).endswith(".lock"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", _deny)

    class _Msg:
        id = 11
        sender_id = "w1"
        message = "hi"
        logical_receiver_id = "mb1"

    out = tps.attempt_teammate_push_reported("t1", [cast(Any, _Msg())])
    assert out.pushed is False
    assert out.reason == "write_failed", f"permanent error reported as {out.reason!r}"

    monkeypatch.setattr(os, "open", real_open)
    assert tps.native_fallback_reason("t1") == "native_write_failed"


def test_permanent_lock_error_returns_false_from_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer's own contract: False (failure), never None (contention)."""
    inbox = tmp_path / "team-lead.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    real_open = os.open

    def _deny(path: Any, *a: Any, **k: Any) -> Any:
        if str(path).endswith(".lock"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", _deny)
    assert tps._write_inbox_entry(inbox, {"msg_id": "x"}) is False


def test_a_held_lock_is_still_only_contention(tmp_path: Path) -> None:
    """The repair must not turn an ordinary race into a failure."""
    inbox = tmp_path / "team-lead.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(inbox.resolve()) + ".lock")
    import os as _os

    held = _os.open(str(lock), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY, 0o644)
    try:
        assert tps._write_inbox_entry(inbox, {"msg_id": "y"}) is None
    finally:
        _os.close(held)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# r9 repair 1: the TOCTOU verify must classify permanent errors as failures.
# r9 repair 2: the vanished-holder path is a race, and is now pinned.
# ---------------------------------------------------------------------------


def test_a_real_unwritable_directory_is_a_write_failure(tmp_path: Path) -> None:
    """A REAL filesystem permission denial -- chmod, no patching.

    The parent directory is made unwritable, so the very first O_CREAT raises
    EACCES from the kernel. That is permanent, so the writer must report False
    (write failure), never None (contention).
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permission bits")
    d = tmp_path / "locked"
    d.mkdir()
    inbox = d / "team-lead.json"
    inbox.write_text("[]", encoding="utf-8")
    os.chmod(d, 0o500)  # r-x: traversable and readable, NOT writable
    try:
        assert tps._write_inbox_entry(inbox, {"msg_id": "x"}) is False
    finally:
        os.chmod(d, 0o700)


def test_permanent_error_on_the_toctou_verify_is_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTANT target: restore `except OSError: return None` on the verify and
    this fails.

    This branch is only reachable after a STALE lock is reclaimed, so a real
    chmod cannot deterministically fail exactly here without also failing the
    unlink or the re-open before it. The permission error is therefore injected
    at the one call the branch guards, which is the narrowest way to pin the
    classification the verdict found wrong.
    """
    inbox = tmp_path / "team-lead.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(inbox.resolve()) + ".lock")
    lock.write_text("", encoding="utf-8")
    # Backdate it past the stale threshold so the reclaim path is taken.
    old = time.time() - (tps._LOCK_STALE_SECONDS + 60)
    os.utime(lock, (old, old))

    real_stat = os.stat

    def _deny_stat(path: Any, *a: Any, **k: Any) -> Any:
        if str(path).endswith(".lock"):
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *a, **k)

    seen: list[str] = []

    def _stat_after_reclaim(path: Any, *a: Any, **k: Any) -> Any:
        # Call 1 is the stale check; call 2 is the TOCTOU verify that follows
        # the reclaim. Fail ONLY call 2 and let every later call succeed.
        #
        # That exactness is the point. An earlier version failed call 2 AND
        # every call after it, so under the mutation the retry's stale-check
        # stat raised instead and the writer still returned False -- the test
        # passed on mutated code and the variant patch survived. Isolating the
        # single call is what makes this witness discriminating.
        if str(path).endswith(".lock"):
            seen.append("x")
            if len(seen) == 2:
                raise PermissionError(13, "Permission denied")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", _stat_after_reclaim)
    assert tps._write_inbox_entry(inbox, {"msg_id": "y"}) is False
    monkeypatch.setattr(os, "stat", real_stat)
    lock.unlink(missing_ok=True)


def test_a_vanished_holder_is_a_race_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r9 repair 2: the holder disappearing mid-reclaim is contention.

    The verdict noted this behaviour was right but untested. If the lock the
    stale check saw is already gone by the time we unlink it, another writer
    reclaimed it: a race. The writer must report None so the reconciler carries
    the row, NOT False, which would engage the fallback surface on a healthy
    seat.
    """
    inbox = tmp_path / "team-lead.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(inbox.resolve()) + ".lock")
    lock.write_text("", encoding="utf-8")
    old = time.time() - (tps._LOCK_STALE_SECONDS + 60)
    os.utime(lock, (old, old))

    def _vanished(path: Any, *a: Any, **k: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(os, "unlink", _vanished)
    assert tps._write_inbox_entry(inbox, {"msg_id": "z"}) is None


def test_a_holder_that_beats_us_to_the_reclaim_is_a_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the vanished-holder path: we unlink, someone else
    re-creates before our open. Also a race, also None."""
    inbox = tmp_path / "team-lead.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(inbox.resolve()) + ".lock")
    lock.write_text("", encoding="utf-8")
    old = time.time() - (tps._LOCK_STALE_SECONDS + 60)
    os.utime(lock, (old, old))

    real_open = os.open
    calls: list[str] = []

    def _taken(path: Any, *a: Any, **k: Any) -> Any:
        if str(path).endswith(".lock"):
            calls.append("x")
            raise FileExistsError(17, "File exists")
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", _taken)
    assert tps._write_inbox_entry(inbox, {"msg_id": "w"}) is None
    monkeypatch.setattr(os, "open", real_open)
    lock.unlink(missing_ok=True)
