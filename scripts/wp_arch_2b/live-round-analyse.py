#!/usr/bin/env python3
"""Read two live-round arms and decide FLIP-READY (WP-ARCH 2b, plan §3).

Runs on the BOX, where the fork is installed, because the comparison has to map
the pane's legacy vocabulary through the projection's own ``legacy_state`` rather
than through a second copy of that table — the plan is explicit that comparing
raw strings is what produced the ``''`` artefact in the earlier round.

Input: two coordination databases and two fleet-snapshot files, one pair per arm.
Output: one ``PASS``/``FAIL``/``SKIP`` line per check on stdout, then a verdict
line.  Exit status is 0 when the verdict is ``FLIP-READY: YES``, 1 otherwise, so
a caller can gate on it without parsing.

A SKIP is never a pass.  A check whose workload did not happen (no cap banner was
driven, no dialog appeared) reports SKIP and the verdict is NO, because a round
that did not exercise a criterion has not met it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from cli_agent_orchestrator.app.worker_truth.mapping import legacy_state
from cli_agent_orchestrator.core.timing import PANE_HEARTBEAT_S

PANE_CLASSIFIED = "status.pane_classified"
STATUS_TRANSITION = "status.transition"
USAGE_CAPPED = "usage.capped"
PROMPT_AWAITING = "prompt.awaiting"
LEGACY_PUBLISHED = "status.legacy_published"


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    failed: int = 0
    skipped: int = 0

    def ok(self, name: str, detail: str) -> None:
        self.lines.append(f"PASS {name} — {detail}")

    def bad(self, name: str, detail: str) -> None:
        self.failed += 1
        self.lines.append(f"FAIL {name} — {detail}")

    def skip(self, name: str, detail: str) -> None:
        self.skipped += 1
        self.lines.append(f"SKIP {name} — {detail}")


def _rows(db: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute(sql, args))
    finally:
        connection.close()


def _when(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _sourced(db: Path) -> set[str]:
    """Terminals the projection actually PUBLISHED for, in this arm.

    Read from the log rather than from a flag, because the log is what the round
    produced and a flag would be this harness trusting the thing under test.

    The discriminator is ``fed_by``, not the presence of a transition row.  The
    projector folds for EVERY terminal whenever ingestion is on — that is phase
    1, and it is why the off arm has transitions too — so "has a
    ``status.transition``" would name the whole fleet and make every comparison
    below vacuous.  A publish stamped ``fed_by: worker_truth`` has exactly one
    producer, and D5 put that field there for this question.
    """
    return {
        row["terminal_id"]
        for row in _rows(
            db,
            "SELECT DISTINCT terminal_id FROM worker_event WHERE kind = ? AND payload LIKE ?",
            (LEGACY_PUBLISHED, '%"fed_by": "worker_truth"%'),
        )
    }


def _latest_classified(db: Path) -> dict[str, sqlite3.Row]:
    latest: dict[str, sqlite3.Row] = {}
    for row in _rows(
        db,
        "SELECT terminal_id, seq, payload, ingested_at FROM worker_event "
        "WHERE kind = ? ORDER BY terminal_id, seq",
        (PANE_CLASSIFIED,),
    ):
        latest[row["terminal_id"]] = row
    return latest


def _latched(row: sqlite3.Row) -> str:
    try:
        return str(json.loads(row["payload"]).get("latched_status") or "")
    except Exception:
        return ""


def check_unmapped(db: Path, report: Report) -> None:
    """Every non-empty pane reading must be a status the vocabulary knows.

    The plan's own correction: ``legacy_state('')`` is ``None`` and the check
    SKIPS such a row, so a round that counted raw strings read those rows as
    disagreements.  A non-empty value that still maps to ``None`` is a genuine
    defect — a status nobody can compare.
    """
    bad: list[str] = []
    for row in _rows(db, "SELECT terminal_id, payload FROM worker_event WHERE kind = ?", (PANE_CLASSIFIED,)):
        raw = _latched(row)
        if raw and legacy_state(raw) is None:
            bad.append(f"{row['terminal_id']}={raw!r}")
    if bad:
        report.bad("unmapped-pane-status", f"{len(bad)} row(s): {sorted(set(bad))[:5]}")
    else:
        report.ok("unmapped-pane-status", "no non-empty reading failed to map")


def check_disagreements(db: Path, report: Report) -> None:
    """I1: the projection and the pane agree, outside one heartbeat of lag."""
    sourced = _sourced(db)
    if not sourced:
        report.skip("pane-disagreement", "no terminal was projected in this arm")
        return
    states = {
        row["terminal_id"]: row
        for row in _rows(db, "SELECT terminal_id, state, since FROM worker_state_shadow")
    }
    horizon = timedelta(seconds=PANE_HEARTBEAT_S)
    disagreeing: list[str] = []
    for terminal_id, classified in _latest_classified(db).items():
        if terminal_id not in sourced or terminal_id not in states:
            continue
        raw = _latched(classified)
        mapped = legacy_state(raw) if raw else None
        if mapped is None:
            continue  # the '' artefact, excluded by the plan's own rule
        projected = states[terminal_id]["state"]
        if mapped.value == projected:
            continue
        seen = _when(classified["ingested_at"])
        since = _when(states[terminal_id]["since"])
        if seen is not None and since is not None and abs(seen - since) <= horizon:
            continue  # ordinary lag: the two sides are fed by different clocks
        disagreeing.append(f"{terminal_id}: projected {projected} vs pane {raw}")
    if disagreeing:
        report.bad("pane-disagreement", "; ".join(disagreeing))
    else:
        report.ok("pane-disagreement", f"{len(sourced)} sourced terminal(s) agree")


def check_unsourced_identical(on: Path, off: Path, report: Report) -> None:
    """I7 / AC-2b case 6: an unsourced lane is byte-identical across arms.

    The criterion that proves the fallback was DEMOTED rather than damaged.
    """
    on_sourced, off_sourced = _sourced(on), _sourced(off)
    unsourced = {
        terminal
        for terminal in set(_latest_classified(off)) | set(_latest_classified(on))
        if terminal not in on_sourced and terminal not in off_sourced
    }
    if not unsourced:
        report.skip("unsourced-identical", "the round had no unsourced lane to compare")
        return

    def sequence(db: Path) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for row in _rows(
            db,
            "SELECT terminal_id, payload FROM worker_event WHERE kind = ? ORDER BY terminal_id, seq",
            (PANE_CLASSIFIED,),
        ):
            out.setdefault(row["terminal_id"], []).append(_latched(row))
        return out

    on_seq, off_seq = sequence(on), sequence(off)
    differing = [t for t in unsourced if on_seq.get(t, []) != off_seq.get(t, [])]
    if differing:
        report.bad("unsourced-identical", f"classification differs across arms for {differing}")
    else:
        report.ok("unsourced-identical", f"{len(unsourced)} unsourced lane(s) identical")


def check_evidence_chain(db: Path, report: Report) -> None:
    """I2 / AC-2b case 8: every transition names an event that resolves."""
    transitions = _rows(
        db, "SELECT event_id, evidence, terminal_id FROM worker_event WHERE kind = ?", (STATUS_TRANSITION,)
    )
    if not transitions:
        report.skip("evidence-chain", "no transition rows in this arm")
        return
    known = {row["event_id"] for row in _rows(db, "SELECT event_id FROM worker_event")}
    broken = [
        row["terminal_id"]
        for row in transitions
        if not row["evidence"] or row["evidence"] not in known
    ]
    if broken:
        report.bad("evidence-chain", f"{len(broken)} transition(s) cite nothing resolvable")
    else:
        report.ok("evidence-chain", f"{len(transitions)} transition(s) resolve")


def check_capped_parity(on: Path, off: Path, report: Report) -> None:
    """AC-2b case 12: the one case whose arms are EXPECTED to agree."""

    def count(db: Path) -> int:
        return len(_rows(db, "SELECT event_id FROM worker_event WHERE kind = ?", (USAGE_CAPPED,)))

    on_count, off_count = count(on), count(off)
    if on_count == 0 and off_count == 0:
        report.skip("capped-parity", "no cap was driven in either arm")
        return
    if on_count == off_count:
        report.ok("capped-parity", f"{on_count} usage.capped in both arms")
    else:
        report.bad("capped-parity", f"on={on_count} off={off_count}")


def check_prompt_awaiting(db: Path, report: Report) -> None:
    """AC-2b case 13: a sourced codex lane still reaches ``awaiting_input``."""
    rows = _rows(db, "SELECT terminal_id FROM worker_event WHERE kind = ?", (PROMPT_AWAITING,))
    if not rows:
        report.skip("prompt-awaiting", "no dialog was driven in the on arm")
        return
    sourced = _sourced(db)
    on_sourced = [row["terminal_id"] for row in rows if row["terminal_id"] in sourced]
    if not on_sourced:
        report.bad("prompt-awaiting", "prompt.awaiting exists but on no projected terminal")
        return
    reached = _rows(
        db,
        "SELECT terminal_id FROM worker_event WHERE kind = ? AND payload LIKE ?",
        (STATUS_TRANSITION, '%"to": "awaiting_input"%'),
    )
    if reached:
        report.ok("prompt-awaiting", f"{len(set(on_sourced))} projected lane(s) reached awaiting_input")
    else:
        report.bad("prompt-awaiting", "prompt.awaiting folded to no awaiting_input transition")


def check_status_since(fleet: Path, db: Path, report: Report) -> None:
    """D11 / AC-2b case 5, from the fleet snapshot the on arm captured."""
    try:
        rows = json.loads(fleet.read_text(encoding="utf-8"))
    except Exception as exc:
        report.skip("status-since", f"no readable fleet snapshot ({exc})")
        return
    if not isinstance(rows, list) or not rows:
        report.skip("status-since", "the fleet snapshot is empty")
        return
    sourced = _sourced(db)
    wrong_null = [r["id"] for r in rows if r.get("id") in sourced and not r.get("status_since")]
    wrong_value = [r["id"] for r in rows if r.get("id") not in sourced and r.get("status_since")]
    if wrong_value:
        report.bad("status-since", f"non-null for unsourced terminal(s) {wrong_value}")
    elif wrong_null:
        report.bad("status-since", f"null for projected terminal(s) {wrong_null}")
    else:
        report.ok("status-since", f"{len(rows)} fleet row(s) carry the right presence")


def check_condition_lifetime(fleet_series: Path, report: Report) -> None:
    """AC-2b case 3: a non-BUSY label does not outlive a sweep.

    Reads the on arm's fleet snapshots taken over the round.  A label that is
    still present two heartbeats after its terminal last moved has outlived the
    sweep that should have cleared it.
    """
    try:
        samples = [json.loads(line) for line in fleet_series.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception as exc:
        report.skip("condition-lifetime", f"no readable fleet series ({exc})")
        return
    if len(samples) < 3:
        report.skip("condition-lifetime", f"only {len(samples)} snapshot(s); need at least 3")
        return
    runs: dict[str, int] = {}
    worst: tuple[str, int] | None = None
    for sample in samples:
        for row in sample.get("rows", []):
            terminal_id = row.get("id")
            label = row.get("condition")
            if label and label != "BUSY":
                runs[terminal_id] = runs.get(terminal_id, 0) + 1
                if worst is None or runs[terminal_id] > worst[1]:
                    worst = (f"{terminal_id}:{label}", runs[terminal_id])
            else:
                runs[terminal_id] = 0
    limit = 3  # snapshots are taken one heartbeat apart; three is two sweeps plus slack
    if worst is not None and worst[1] > limit:
        report.bad("condition-lifetime", f"{worst[0]} held for {worst[1]} consecutive snapshots")
    elif worst is None:
        report.skip("condition-lifetime", "no non-BUSY condition appeared in the on arm")
    else:
        report.ok("condition-lifetime", f"longest non-BUSY run {worst[1]} snapshot(s)")


def check_read_path(read_path: Path, db: Path, report: Report) -> None:
    """AC-2b case 10: the FUSED getters do not move for a projected terminal.

    D1d's bypass is the highest-risk surface in the cutover and no post-mortem of
    the database can see it — the getters are a property of the running server.
    What the round captures instead is the outside view: ``/terminals/<id>``
    reads through ``get_status`` and the fleet row through ``fuse_status``, so
    the two agreeing with each other, and with the projection's own state, is
    "the fused getter did not move" observed rather than argued.
    """
    try:
        samples = [
            json.loads(line)
            for line in read_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as exc:
        report.skip("read-path", f"no readable read-path capture ({exc})")
        return
    if not samples:
        report.skip("read-path", "the read-path capture is empty")
        return
    sourced = _sourced(db)
    states = {
        row["terminal_id"]: row["state"]
        for row in _rows(db, "SELECT terminal_id, state FROM worker_state_shadow")
    }
    disagreeing: list[str] = []
    checked = 0
    for sample in samples:
        terminal_id = sample.get("id")
        if terminal_id not in sourced:
            continue
        terminal = sample.get("terminal") or {}
        getter = terminal.get("status")
        fleet = sample.get("fleet_status")
        projected = states.get(terminal_id)
        if getter is None or fleet is None or projected is None:
            continue
        checked += 1
        expected = legacy_status_for(projected)
        if getter != fleet:
            disagreeing.append(f"{terminal_id}: get_status {getter} vs fleet {fleet}")
        elif expected is not None and getter != expected:
            disagreeing.append(f"{terminal_id}: getters {getter} vs projection {projected}")
    if not checked:
        report.skip("read-path", "no projected terminal appeared in the capture")
    elif disagreeing:
        report.bad("read-path", "; ".join(disagreeing))
    else:
        report.ok("read-path", f"{checked} projected read(s) agree with the projection")


def legacy_status_for(state: str) -> str | None:
    """The legacy status a projected state publishes, where it is unambiguous.

    ``idle`` is the one state with two possible publishes — ``idle`` or
    ``completed``, depending on whether a turn just ended — so it is excluded
    rather than guessed at.
    """
    from cli_agent_orchestrator.app.worker_truth.mapping import FORWARD_STATUS_MAP
    from cli_agent_orchestrator.core.states import WorkerState

    try:
        worker_state = WorkerState(state)
    except ValueError:
        return None
    if worker_state is WorkerState.IDLE:
        return None
    return FORWARD_STATUS_MAP.get(worker_state)


def check_announcement_count(server_log: Path, db: Path, report: Report) -> None:
    """B1 and B2's shape: one announcement per status CHANGE, never more.

    The announce path fires the event bus, the auto-responder record, the
    children-ledger reconcile and the condition classifier, and every one of them
    is edge-shaped.  The monitor logs a line per announcement, so counting those
    against the number of distinct consecutive published statuses is the check
    that would have caught B1 before a reviewer did.
    """
    try:
        lines = server_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        report.skip("announce-count", f"no readable server log ({exc})")
        return
    announcements: dict[str, int] = {}
    for line in lines:
        marker = " status changed: "
        if marker not in line or "Terminal " not in line:
            continue
        terminal_id = line.split("Terminal ", 1)[1].split(" ", 1)[0]
        announcements[terminal_id] = announcements.get(terminal_id, 0) + 1
    if not announcements:
        report.skip("announce-count", "no announcements in the log")
        return
    # The published-status CHANGES, counted from the projection's own transitions
    # mapped forward: two transitions onto one legacy status are one change.
    changes: dict[str, int] = {}
    for row in _rows(
        db,
        "SELECT terminal_id, payload FROM worker_event WHERE kind = ? ORDER BY terminal_id, seq",
        (STATUS_TRANSITION,),
    ):
        try:
            to_state = json.loads(row["payload"]).get("to")
        except Exception:
            continue
        published = legacy_status_for(str(to_state)) or str(to_state)
        key = row["terminal_id"]
        last = changes.get(f"{key}:last")
        if last != published:
            changes[key] = changes.get(key, 0) + 1
            changes[f"{key}:last"] = published  # type: ignore[assignment]
    noisy = [
        f"{terminal_id}: {count} announcements for {changes.get(terminal_id, 0)} changes"
        for terminal_id, count in announcements.items()
        if count > max(1, changes.get(terminal_id, 0)) + 2
    ]
    if noisy:
        report.bad("announce-count", "; ".join(noisy))
    else:
        report.ok("announce-count", f"{sum(announcements.values())} announcement(s), none in excess")


def check_condition_ledger(on: Path, off: Path, report: Report) -> None:
    """B2: the clear branch must not write a row per terminal per sweep.

    A periodic driver on a seam built for an edge is this phase's recurring
    defect, and the ledger is append-only with no prune — so the number that
    matters is not "is it large" but "does it grow with TICKS rather than with
    events".  The off arm has no sweep-driven re-drive at all, so it is the
    control.
    """

    # The column is ``decision`` (clients/database.py:2755), not ``outcome``.
    # It was ``outcome`` here, and a blanket ``except Exception`` turned the
    # resulting "no such column" into "no condition_ledger table" — so this
    # check, the only live detector for B2, reported SKIP on every real
    # database and could never have failed.  Distinguish the two now: a missing
    # table is a skip, a broken query is a FAIL, because a check that cannot
    # run has not passed.
    def cleared(db: Path) -> tuple[int, str]:
        present = _rows(
            db, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("condition_ledger",),
        )
        if not present:
            return -1, "no condition_ledger table"
        try:
            rows = _rows(
                db, "SELECT id FROM condition_ledger WHERE decision = ?", ("cleared",)
            )
        except Exception as exc:  # pragma: no cover - a schema drift, reported not hidden
            return -2, f"condition_ledger query failed: {exc}"
        return len(rows), ""

    on_rows, on_why = cleared(on)
    off_rows, off_why = cleared(off)
    if on_rows == -2 or off_rows == -2:
        report.bad("condition-ledger", on_why or off_why)
        return
    if on_rows < 0 or off_rows < 0:
        report.skip("condition-ledger", on_why or off_why)
        return
    transitions = len(_rows(on, "SELECT event_id FROM worker_event WHERE kind = ?", (STATUS_TRANSITION,)))
    budget = off_rows + transitions + 10
    if on_rows > budget:
        report.bad(
            "condition-ledger",
            f"{on_rows} cleared rows on the on arm against {off_rows} off and "
            f"{transitions} transitions — the clear is firing per tick",
        )
    else:
        report.ok("condition-ledger", f"{on_rows} cleared rows (off arm {off_rows})")


def check_certified_pane_silence(db: Path, report: Report) -> None:
    """WP-HERDR §6(ii): no pane publish for a certified terminal after it degraded.

    §6(ii) keeps a certified terminal PROJECTED through ``degraded(no_signal)``
    instead of handing the lifecycle back to the pane (AC-2b case 7's rule for
    everyone else).  The whole value of that is the pane staying quiet — if the
    scraper resumes publishing anyway, the terminal has a second writer at
    exactly the moment nobody knows what it is doing, which is the failure the
    override exists to prevent.

    The certified-and-stale terminals are read from the finding the sweep writes
    for precisely that condition, rather than re-derived here: a second
    implementation of "certified and proven and stale" in the harness would
    eventually disagree with the projector's, and then this check would be
    testing the harness.
    """
    stale = _rows(
        db,
        "SELECT terminal_id FROM finding WHERE code = ?",
        ("DIAG-CERTIFIED-SOURCE-STALE",),
    )
    if not stale:
        report.skip("certified-pane-silence", "no certified source went stale in this arm")
        return
    offending: list[str] = []
    for row in stale:
        terminal_id = row["terminal_id"]
        degraded_at = [
            event["seq"]
            for event in _rows(
                db,
                "SELECT seq, payload FROM worker_event WHERE terminal_id = ? AND kind = ? "
                "ORDER BY seq",
                (terminal_id, STATUS_TRANSITION),
            )
            if '"rule": "no_signal_sweep"' in (event["payload"] or "")
        ]
        if not degraded_at:
            continue
        first_degrade = degraded_at[0]
        after = _rows(
            db,
            "SELECT seq, payload FROM worker_event WHERE terminal_id = ? AND kind = ? "
            "AND seq > ? ORDER BY seq",
            (terminal_id, LEGACY_PUBLISHED, first_degrade),
        )
        pane_publishes = [
            event for event in after if '"fed_by": "worker_truth"' not in (event["payload"] or "")
        ]
        if pane_publishes:
            offending.append(f"{terminal_id}: {len(pane_publishes)} pane publish(es) after degrade")
    if offending:
        report.bad("certified-pane-silence", "; ".join(offending))
    else:
        report.ok(
            "certified-pane-silence",
            f"{len(stale)} certified terminal(s) degraded, no pane publish after",
        )


def check_off_arm_inert(off: Path, on: Path, report: Report) -> None:
    """The off arm publishes nothing through the projection, fleet-wide.

    ``unsourced-identical`` compares the lanes that were never eligible; this is
    the claim about EVERYTHING.  ``fed_by`` is stamped on every publish the
    legacy egress records, so a single ``worker_truth`` row in the off arm means
    the switch did not hold — and none in the on arm means the round never
    exercised the cutover it was run to test.
    """

    def fed_by_projection(db: Path) -> int:
        return len(
            _rows(
                db,
                "SELECT event_id FROM worker_event WHERE kind = ? AND payload LIKE ?",
                (LEGACY_PUBLISHED, '%"fed_by": "worker_truth"%'),
            )
        )

    off_count, on_count = fed_by_projection(off), fed_by_projection(on)
    if off_count:
        report.bad("off-arm-inert", f"{off_count} projection publish(es) with the switch off")
    elif not on_count:
        report.bad("off-arm-inert", "the on arm published nothing through the projection either")
    else:
        report.ok("off-arm-inert", f"off arm silent, on arm published {on_count}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--on-db", type=Path, required=True)
    parser.add_argument("--off-db", type=Path, required=True)
    parser.add_argument("--on-fleet", type=Path, required=True)
    parser.add_argument("--on-fleet-series", type=Path, required=True)
    parser.add_argument("--on-read-path", type=Path, required=True)
    parser.add_argument("--on-server-log", type=Path, required=True)
    args = parser.parse_args()

    report = Report()
    for db in (args.on_db, args.off_db):
        if not db.exists():
            print(f"FAIL arm-database — {db} does not exist")
            print("FLIP-READY: NO")
            return 1

    check_unmapped(args.on_db, report)
    check_disagreements(args.on_db, report)
    check_unsourced_identical(args.on_db, args.off_db, report)
    check_evidence_chain(args.on_db, report)
    check_capped_parity(args.on_db, args.off_db, report)
    check_prompt_awaiting(args.on_db, report)
    check_status_since(args.on_fleet, args.on_db, report)
    check_condition_lifetime(args.on_fleet_series, report)
    check_read_path(args.on_read_path, args.on_db, report)
    check_announcement_count(args.on_server_log, args.on_db, report)
    check_condition_ledger(args.on_db, args.off_db, report)
    check_certified_pane_silence(args.on_db, report)
    check_off_arm_inert(args.off_db, args.on_db, report)

    for line in report.lines:
        print(line)
    ready = report.failed == 0 and report.skipped == 0
    print(f"checks: {len(report.lines)}  failed: {report.failed}  skipped: {report.skipped}")
    print(f"FLIP-READY: {'YES' if ready else 'NO'}")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
