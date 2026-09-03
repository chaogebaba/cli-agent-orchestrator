"""The producers against the REAL SQLite store and composition root (F725 #581).

Every other test in this directory runs the producers against an in-memory fake
implementing ``core.ports.EventStore``.  That is the right default — it keeps the
producer tests fast and independent of lane A's schema — but it can only prove
the producers honour the Protocol, never that the Protocol was the same shape on
both sides.  This file closes that gap: one file, the real
``SqliteEventStore``, the real migrator, and the real ``bootstrap`` switch.

What it is checking, in order of how badly each would hurt if wrong:

1. **The switch arms and disarms the producers.** ``start_worker_truth`` with the
   variable unset must leave every hook a no-op; with it set, the same hooks must
   land rows in the database on disk.
2. **Rows survive the round trip.** A draft built by a producer must come back out
   of SQLite with its payload, evidence and ``source_ref`` intact — a producer
   that wrote a payload the store could not serialise would pass every fake test.
3. **Sequences are contiguous per terminal.** B7 lets consumers rely on
   ``seq + 1``; the producers are the things that will break it first if the store
   and the fake disagree about who mints it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.truth import legacy_egress, server_decisions, wiring
from cli_agent_orchestrator.core.events import DecisionKind, EventKind


class _Monitor:
    """The shape of ``StatusMonitor`` the egress hook touches."""

    _status_fusion_reason: dict[str, str] = {}

    def get_condition(self, terminal_id: str) -> str | None:
        return None


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "worker-truth.db"


@pytest.fixture(autouse=True)
def _clean(db: Path) -> Iterator[None]:
    legacy_egress.reset_edges()
    wiring.reset_producers()
    yield
    legacy_egress.reset_edges()
    wiring.reset_producers()


async def _boot(db: Path, *, enabled: bool) -> bootstrap.WorkerTruthRuntime:
    env = {bootstrap.INGEST_ENV_VAR: "1"} if enabled else {}
    return await bootstrap.start_worker_truth(db_path=db, env=env)


@pytest.mark.asyncio
async def test_the_switch_off_leaves_every_hook_inert(db: Path) -> None:
    runtime = await _boot(db, enabled=False)
    try:
        assert runtime.migration.ok is True  # the migrator runs at EVERY boot
        assert runtime.ingest_enabled is False
        assert wiring.producers_installed() is False

        legacy_egress.record_legacy_publish(
            _Monitor(), "t-off", "idle", "incremental", "incremental", "ok", None
        )
        server_decisions.record_delivery_attempt("t-off", carrier="send_input")
        server_decisions.record_teardown_intended(
            "t-off", scope_kind="terminal", scope_key="t-off", ttl_s=300.0
        )
    finally:
        await bootstrap.shutdown_worker_truth()

    # The tables exist and are empty: inert, not absent.
    runtime = await _boot(db, enabled=True)
    try:
        assert runtime.event_store is not None
        assert runtime.event_store.read() == []
    finally:
        await bootstrap.shutdown_worker_truth()


@pytest.mark.asyncio
async def test_the_switch_on_lands_producer_rows_in_sqlite(db: Path) -> None:
    runtime = await _boot(db, enabled=True)
    try:
        assert runtime.ingest_enabled is True
        assert wiring.producers_installed() is True
        store = runtime.event_store
        assert store is not None

        legacy_egress.record_legacy_publish(
            _Monitor(), "t-1", "idle", "incremental", "incremental", "ok", "classified"
        )
        for _ in range(20):  # B9: the edge, not the volume, decides
            legacy_egress.record_legacy_publish(
                _Monitor(), "t-1", "idle", "incremental", "incremental", "ok", "classified"
            )
        legacy_egress.record_legacy_publish(
            _Monitor(), "t-1", "processing", "incremental", "incremental", "ok", None
        )
        server_decisions.record_delivery_attempt("t-1", carrier="send_input")

        rows = store.read("t-1")
        kinds = [row.kind for row in rows]
        assert kinds == [
            EventKind.STATUS_LEGACY_PUBLISHED,
            EventKind.STATUS_LEGACY_PUBLISHED,
            DecisionKind.DELIVERY_ATTEMPT,
        ]
        assert [row.seq for row in rows] == [1, 2, 3]

        first, second, attempt = rows
        assert first.payload["latched_status"] == "idle"
        assert first.payload["raw_classification"] == "classified"
        assert second.payload["latched_status"] == "processing"
        assert attempt.evidence == second.event_id
        assert attempt.decision is DecisionKind.DELIVERY_ATTEMPT
        assert attempt.payload["outcome"] == "confirmed"
        assert attempt.msg_id
    finally:
        await bootstrap.shutdown_worker_truth()


@pytest.mark.asyncio
async def test_rows_survive_a_process_restart(db: Path) -> None:
    """The store is on disk; a producer's row outlives the runtime that wrote it."""
    runtime = await _boot(db, enabled=True)
    try:
        server_decisions.record_teardown_intended(
            "t-2", scope_kind="terminal", scope_key="t-2", ttl_s=300.0, requested_by="chao"
        )
    finally:
        await bootstrap.shutdown_worker_truth()

    runtime = await _boot(db, enabled=True)
    try:
        store = runtime.event_store
        assert store is not None
        rows = store.read("t-2")
        assert len(rows) == 1
        assert rows[0].kind is DecisionKind.TEARDOWN_INTENDED
        assert rows[0].payload["ttl_s"] == 300.0
        assert rows[0].payload["requested_by"] == "chao"
        assert isinstance(rows[0].observed_at, datetime)
        assert rows[0].observed_at.tzinfo is not None
    finally:
        await bootstrap.shutdown_worker_truth()


@pytest.mark.asyncio
async def test_shutdown_disarms_the_hooks(db: Path) -> None:
    """A hook firing during shutdown must not log a failure for a healthy stop."""
    await _boot(db, enabled=True)
    await bootstrap.shutdown_worker_truth()
    assert wiring.producers_installed() is False
    legacy_egress.record_legacy_publish(
        _Monitor(), "t-3", "idle", "incremental", "incremental", "ok", None
    )

    runtime = await _boot(db, enabled=True)
    try:
        store = runtime.event_store
        assert store is not None
        assert store.read("t-3") == []
    finally:
        await bootstrap.shutdown_worker_truth()


@pytest.mark.asyncio
async def test_a_payload_the_store_must_serialise(db: Path) -> None:
    """Producer payloads have to be JSON-safe, which a fake store never checks."""
    runtime = await _boot(db, enabled=True)
    try:
        store = runtime.event_store
        assert store is not None
        server_decisions.record_delivery_attempt(
            "t-4",
            carrier="send_prepared_input",
            exc=RuntimeError('a detail with "quotes", a newline\n and unicode ✓'),
        )
        row = store.read("t-4")[0]
        assert row.payload["outcome"] == "error"
        assert "unicode ✓" in row.payload["detail"]
        json.dumps(row.payload)  # the round trip really is JSON
    finally:
        await bootstrap.shutdown_worker_truth()
