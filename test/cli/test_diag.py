"""``cao diag`` end to end (WP-ARCH phase 1, AC7 — AC11 hook point 4).

Driven through Click against a real migrated database, because the parts most
likely to break are the wiring ones: the fall-through that makes
``cao diag <terminal-id>`` work without a subcommand, the ``--since`` parser, and
the read-only path from the CLI to the stores it may not import directly.
"""

from __future__ import annotations

from pathlib import Path
from test.app.fakes import FakeClock

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.adapters.store.event_log import SqliteEventStore
from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.adapters.store.state import SqliteStateStore
from cli_agent_orchestrator.app.worker_truth.checks import (
    CheckRegistry,
    LegacyDisagreementCheck,
    register_phase1_checks,
)
from cli_agent_orchestrator.app.worker_truth.projector import Projector, StaticSourceRegistry
from cli_agent_orchestrator.cli.commands.diag import INGEST_ENV_VAR, _parse_since, diag
from cli_agent_orchestrator.core.events import (
    Confidence,
    DecisionKind,
    EventDraft,
    EventKind,
    Producer,
)
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S

TERMINAL = "term-cli"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A database holding one worker that went busy and then fell silent."""
    path = tmp_path / "cao.db"
    result, pool = migrate(path, busy_timeout_ms=5000)
    assert result.ok and pool is not None

    clock = FakeClock()
    findings = SqliteFindingStore(pool, clock=clock)
    registry = register_phase1_checks(CheckRegistry(findings))
    events = SqliteEventStore(pool, clock=clock, check_runner=registry)
    states = SqliteStateStore(pool)
    projector = Projector(
        events,
        states,
        clock,
        StaticSourceRegistry(),
        legacy_check=LegacyDisagreementCheck(findings, events, states, clock),
    )

    def emit(kind, **kw):
        stored = events.append(
            EventDraft(
                terminal_id=TERMINAL,
                kind=kind,
                producer=kw.pop("producer", Producer.JSONL),
                confidence=kw.pop("confidence", Confidence.AUTHORITATIVE),
                observed_at=clock.now(),
                **kw,
            )
        )
        projector.project(stored)
        return stored

    emit(EventKind.SESSION_STARTED, source_ref="rollout:/tmp/r.jsonl#0")
    clock.advance(2)
    emit(EventKind.SUBMISSION_CONFIRMED, msg_id="MSGCLI")
    clock.advance(1)
    emit(EventKind.TURN_STARTED)
    clock.advance(5)
    emit(EventKind.USAGE_CAPPED, producer=Producer.PANE, confidence=Confidence.DERIVED)
    clock.advance(1)
    emit(EventKind.TURN_STARTED)  # capped -> busy: anomalous, applied, flagged
    clock.advance(NO_SIGNAL_S + 1)
    projector.sweep()
    pool.close_all()
    return path


def _run(db: Path, *args: str):
    return CliRunner().invoke(diag, [*args, "--db", str(db)])


# ---------------------------------------------------------------------- shapes


def test_a_bare_terminal_id_needs_no_subcommand(db: Path) -> None:
    """The blueprint spells the command ``cao diag <terminal_id>``.

    That is the right shape: the timeline is what the command is FOR, and the
    other views are variations on it.
    """
    result = _run(db, TERMINAL)

    assert result.exit_code == 0
    assert f"terminal {TERMINAL}" in result.output
    assert "degraded(no_signal)" in result.output


def test_a_real_subcommand_still_wins(db: Path) -> None:
    """The fall-through must not shadow ``findings``, which is also a plausible
    terminal id."""
    result = _run(db, "findings")

    assert result.exit_code == 0
    assert "DIAG-BAD-TRANSITION" in result.output


def test_help_resolves_rather_than_being_read_as_a_terminal_id(db: Path) -> None:
    result = CliRunner().invoke(diag, ["--help"])

    assert result.exit_code == 0
    assert "evidence chain" in result.output


def test_no_arguments_prints_help_rather_than_a_traceback() -> None:
    result = CliRunner().invoke(diag, [])

    assert result.exit_code == 0
    assert "cao diag" in result.output


# -------------------------------------------------------------------- timeline


def test_the_timeline_shows_decisions_evidence_and_ids(db: Path) -> None:
    result = _run(db, TERMINAL)

    assert DecisionKind.STATUS_TRANSITION.value in result.output
    assert "msg=MSGCLI" in result.output
    assert "rollout:/tmp/r.jsonl#0" in result.output
    assert "<- " in result.output


def test_json_output_parses(db: Path) -> None:
    import json

    result = _run(db, TERMINAL, "--json")

    data = json.loads(result.output)
    assert data["header"]["state"] == "degraded"
    assert data["total"] > 0
    assert any(row["decision"] for row in data["events"])


def test_a_kind_filter_narrows_the_rows(db: Path) -> None:
    import json

    result = _run(db, TERMINAL, "--kind", EventKind.TURN_STARTED.value, "--json")

    data = json.loads(result.output)
    assert data["shown"] == 2
    assert data["shown"] < data["total"]


def test_an_unknown_kind_is_a_usage_error_not_a_crash(db: Path) -> None:
    result = _run(db, TERMINAL, "--kind", "turn.exploded")

    assert result.exit_code != 0
    assert "unknown event kind" in result.output


def test_an_unknown_terminal_reports_rather_than_failing(db: Path) -> None:
    result = _run(db, "no-such-terminal")

    assert result.exit_code == 0
    assert "never been projected" in result.output


def test_the_ingest_note_follows_the_environment(db: Path, monkeypatch) -> None:
    monkeypatch.delenv(INGEST_ENV_VAR, raising=False)
    off = _run(db, TERMINAL)

    monkeypatch.setenv(INGEST_ENV_VAR, "1")
    on = _run(db, TERMINAL)

    assert "ingest off in this shell" in off.output
    assert "ingest off in this shell" not in on.output


# ------------------------------------------------------------------ why chains


def test_why_walks_the_evidence_chain(db: Path) -> None:
    import json

    rows = json.loads(_run(db, TERMINAL, "--json").output)["events"]
    transition = next(row for row in rows if row["decision"])

    result = _run(db, "why", transition["event_id"])

    assert result.exit_code == 0
    assert transition["evidence"] in result.output


def test_why_also_works_as_a_group_option(db: Path) -> None:
    """``cao diag --why <event-id>`` is how the blueprint writes it."""
    import json

    rows = json.loads(_run(db, TERMINAL, "--json").output)["events"]
    transition = next(row for row in rows if row["decision"])

    result = CliRunner().invoke(diag, ["--why", transition["event_id"], "--db", str(db)])

    assert result.exit_code == 0
    assert "evidence chain" in result.output


def test_why_on_a_missing_event_says_not_found(db: Path) -> None:
    result = _run(db, "why", "NOSUCHEVENTIDATALL0000000")

    assert result.exit_code == 0
    assert "not found" in result.output


# -------------------------------------------------------------------- findings


def test_findings_can_be_filtered_by_code(db: Path) -> None:
    result = _run(db, "findings", "--code", "DIAG-BAD-TRANSITION")

    assert result.exit_code == 0
    assert "capped -> busy" in result.output


def test_an_unknown_finding_code_is_a_usage_error(db: Path) -> None:
    result = _run(db, "findings", "--code", "DIAG-INVENTED")

    assert result.exit_code != 0
    assert "unknown finding code" in result.output


# ------------------------------------------------------------------- agreement


def test_an_invalid_agreement_report_exits_non_zero(db: Path) -> None:
    """AC10 makes this report the phase gate.  A gate that exits 0 on "no
    evidence" is not a gate."""
    result = _run(db, "agreement")

    assert result.exit_code == 2
    assert "INVALID" in result.output


# ----------------------------------------------------------------- since parser


def test_since_accepts_a_duration(db: Path) -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    assert _parse_since("30m", now) == now - timedelta(minutes=30)
    assert _parse_since("2h", now) == now - timedelta(hours=2)
    assert _parse_since("45s", now) == now - timedelta(seconds=45)
    assert _parse_since(None, now) is None


def test_since_accepts_an_iso_timestamp_and_assumes_utc(db: Path) -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    parsed = _parse_since("2026-09-02T11:00:00", now)

    assert parsed == datetime(2026, 9, 2, 11, 0, tzinfo=UTC)


def test_since_rejects_nonsense(db: Path) -> None:
    from datetime import UTC, datetime

    import click

    with pytest.raises(click.BadParameter):
        _parse_since("last tuesday", datetime(2026, 9, 2, tzinfo=UTC))


def test_since_narrows_the_timeline(db: Path) -> None:
    import json

    result = _run(db, TERMINAL, "--since", "1000000h", "--json")
    everything = json.loads(result.output)

    narrow = json.loads(_run(db, TERMINAL, "--since", "0s", "--json").output)

    assert everything["shown"] == everything["total"]
    assert narrow["shown"] <= everything["shown"]


def test_a_valid_agreement_report_exits_zero_and_names_its_scope(tmp_path: Path) -> None:
    """The happy path end to end, including the legacy scope lookup.

    The invalid path is covered above; this asserts that a run meeting the AC10
    content floor actually reports VALID, and that ``--session`` reaches the
    legacy ``terminals`` table through the composition root rather than silently
    comparing the whole fleet.
    """
    import sqlite3

    path = tmp_path / "cao.db"
    result, pool = migrate(path, busy_timeout_ms=5000)
    assert result.ok and pool is not None

    clock = FakeClock()
    findings = SqliteFindingStore(pool, clock=clock)
    registry = register_phase1_checks(CheckRegistry(findings))
    events = SqliteEventStore(pool, clock=clock, check_runner=registry)
    states = SqliteStateStore(pool)
    projector = Projector(events, states, clock, StaticSourceRegistry())

    for index in range(3):
        terminal = f"t{index}"
        producer = Producer.JSONL if index == 0 else Producer.PANE
        for _ in range(30):
            for kind, status in (
                (EventKind.TURN_STARTED, "processing"),
                (EventKind.TURN_ENDED, "idle"),
            ):
                projector.project(
                    events.append(
                        EventDraft(
                            terminal_id=terminal,
                            kind=kind,
                            producer=producer,
                            confidence=Confidence.AUTHORITATIVE,
                            observed_at=clock.now(),
                        )
                    )
                )
                projector.project(
                    events.append(
                        EventDraft(
                            terminal_id=terminal,
                            kind=EventKind.STATUS_LEGACY_PUBLISHED,
                            producer=Producer.PANE,
                            confidence=Confidence.DERIVED,
                            observed_at=clock.now(),
                            payload={"latched_status": status, "origin": "incremental"},
                        )
                    )
                )
                clock.advance(1)
    pool.close_all()

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY, tmux_session TEXT, provider TEXT)")
    conn.executemany(
        "INSERT INTO terminals VALUES (?, ?, ?)",
        [("t0", "cao-alpha", "codex"), ("t1", "cao-alpha", "kiro"), ("t2", "cao-alpha", "cline")],
    )
    conn.commit()
    conn.close()

    result_all = _run(path, "agreement")
    assert result_all.exit_code == 0
    assert "VALID" in result_all.output

    scoped = _run(path, "agreement", "--session", "cao-alpha")
    assert scoped.exit_code == 0
    assert "codex" in scoped.output

    absent = _run(path, "agreement", "--session", "cao-nothing-here")
    assert absent.exit_code == 2
    assert "no evidence" in absent.output
