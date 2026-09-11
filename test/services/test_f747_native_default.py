"""F747 (#747): native seat delivery is the default, not an opt-in.

WP-ARCH 3c K2 deleted ``teammate_push_service``; the NATIVE half of it (the
health probe and the socket-path derivation) moved to
``services/native_delivery_health`` and is what these arms now exercise. The
arms about the four deleted flags, and about the legacy pusher itself, went with
their subject -- see ``test_3c_slice3_surfaces_gone.py`` for the deletions.

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
from cli_agent_orchestrator.services import native_delivery_health as tps
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

#: Keys ruling 4 flipped ON and that still exist. ``supervisor.mailbox_pull`` and
#: ``supervisor.teammate_push`` were two more until WP-ARCH 3c K2 deleted the
#: surfaces they gated.
FLIPPED_ON = [
    "apps.enabled",
    "supervisor.watchdog.quiescence",
]


def test_memory_stays_off() -> None:
    assert ConfigService.get("memory.enabled") is False


@pytest.mark.parametrize(
    "env_name,expected",
    [
        ("CAO_DELIVERY_PHASE", "shadow"),
        ("CAO_WORKER_TRUTH_INGEST", None),
    ],
)
def test_untouched_keys_keep_their_table_value(env_name: str, expected: object) -> None:
    """F883 owns delivery.phase and this batch does not move it.

    Asserted against the table rather than ``get()``: these paths are not in
    ``_OWNED_DEFAULTS``, so their RESOLUTION is unchanged by this batch and
    reading them through ``get()`` would test the pre-existing fall-through,
    not the shipped default.

    ``CAO_SUPERVISOR_WAKE_WS_MONITOR`` was a third row here, pinning the WS
    doorbell's ship-dark default. WP-ARCH 3c K3b deleted that plane, and a flag
    that gates nothing is not a posture worth pinning, so the key is gone from
    the registry entirely -- see ``test_ws_monitor_is_not_a_setting_any_more``.
    """
    if env_name not in cs.ENV_REGISTRY:
        pytest.skip(f"{env_name} is not a registry path")
    assert cs.ENV_REGISTRY[env_name][2] == expected


def test_ws_monitor_is_not_a_setting_any_more() -> None:
    """WP-ARCH 3c K3b: the flag goes with the plane it gated.

    The registry row is what made ``CAO_SUPERVISOR_WAKE_WS_MONITOR`` settable at
    all, and the dotted path is what a settings.json would carry; a row left
    behind would advertise a switch that turns nothing on. Asserted on the table
    rather than through ``get()``, which answers ``None`` for any unknown path
    and so cannot tell a deleted key from a typo.
    """
    assert "CAO_SUPERVISOR_WAKE_WS_MONITOR" not in cs.ENV_REGISTRY
    assert not [k for k, v in cs.ENV_REGISTRY.items() if v[0] == "supervisor.wake.ws_monitor"]


@pytest.mark.parametrize(
    "env_name,path",
    [
        ("CAO_SUPERVISOR_WAKE_MAX_RECORD_AGE_S", "supervisor.wake.max_record_age_s"),
        ("CAO_SUPERVISOR_WAKE_DEDUPE_WINDOW", "supervisor.wake.dedupe_window"),
    ],
)
def test_the_wake_keys_that_must_survive_are_still_registered(env_name: str, path: str) -> None:
    """The negative control for the deletion above.

    Both are named in the 3c plan as keys the phase MUST keep resolving, and both
    sit in the same ``supervisor.wake.*`` block as the deleted one -- so a sweep
    by prefix rather than by key would take them too.
    """
    assert env_name in cs.ENV_REGISTRY
    assert cs.ENV_REGISTRY[env_name][0] == path


def test_shipped_default_beats_call_site_default() -> None:
    """The shipped default wins over whatever a call site happens to pass.

    MUTANT (ruling 4): drop the flipped keys from ``_OWNED_DEFAULTS`` and
    ``_get_value`` falls through to the caller's ``default=``, which is how
    ``supervisor.teammate_push`` used to read falsy no matter what the table
    declared. That key is deleted; ``supervisor.watchdog.quiescence`` is the
    surviving owned default with the same shape and carries the arm now.
    """
    assert ConfigService.get("supervisor.watchdog.quiescence", default=False) is True


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


def test_native_fallback_reason_none_when_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _healthy_terminal(monkeypatch, tmp_path)
    assert tps.native_fallback_reason("t1") is None


def test_reason_no_inbox_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tps, "get_terminal_metadata", lambda tid: {"provider": "claude_code", "metadata": {}}
    )
    monkeypatch.setattr(tps, "resolve_inbox_path", lambda tid, **kw: None)
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
# WP-ARCH 3c K2: the push-path blocks below this line are GONE with their subject
# ---------------------------------------------------------------------------
# Four sections lived here and every one of them tested the legacy FILE carrier:
# the inbox file's size cap and trim, the lockfile's contention-vs-permanent-error
# distinction, the TOCTOU verify, and the stale-holder reclaim races. That writer,
# its lockfile and its file are deleted with ``teammate_push_service``; there is
# no file to cap, no lock to contend for and no holder to reclaim from.
#
# What replaced the concern is not another file test. The queue's own store takes
# a lease per row and its contention is SQLite's, exercised in
# ``test/adapters/test_queue_store.py`` against a real database rather than
# against a JSON file and an advisory lock.
