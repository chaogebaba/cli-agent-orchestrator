"""What ``cao diag`` prints (WP-ARCH phase 1, AC7).

The output is designed backwards from one question, because it is the question
that produced this whole work package: *this worker has stalled — what happened
to it?*  Everything on the page earns its place against that.

* **The header is the projection plus its liveness columns.**  A stall is almost
  always one of four things: the state is wrong, the state is right but old, the
  pane is gone, or the truth source stopped feeding.  The header answers all
  four in one line each, and prints the AGE of every timestamp rather than the
  timestamp alone, because "42s ago" is the number an operator reasons with and
  "17:42:03Z" is the one they have to subtract first.
* **The timeline carries a gap column.**  Reading a stall means finding where the
  log went quiet, and a column of deltas makes that a scan instead of an
  arithmetic exercise.
* **Decisions sit inline with worker truth, marked.**  They are in the same table
  with the same sequence for exactly this reason (audit §4.2); splitting them
  back apart in the renderer would undo it.
* **The footer names the last legacy disagreement.**  When the shadow and the
  legacy status differ, that difference is usually the bug — and it is the thing
  the agreement report (AC10) counts in bulk, so the single-worker view shows the
  most recent instance with the rows that produced it.

``--json`` emits the same content as data.  Both come from one pair of builder
functions so the text view can never drift from the machine-readable one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cli_agent_orchestrator.app.worker_truth.agreement import AgreementReport
from cli_agent_orchestrator.app.worker_truth.mapping import legacy_state
from cli_agent_orchestrator.core.events import AnyKind, EventKind, WorkerEvent
from cli_agent_orchestrator.core.findings import Finding, FindingCode
from cli_agent_orchestrator.core.ports import EventStore, FindingStore, QueueStore, StateStore
from cli_agent_orchestrator.core.states import WorkerState

__all__ = [
    "DiagSources",
    "INGEST_OFF_NOTE",
    "findings_payload",
    "message_payload",
    "render_agreement",
    "render_message",
    "render_findings",
    "render_timeline",
    "render_why",
    "timeline_payload",
    "why_payload",
]

#: Printed whenever ``CAO_WORKER_TRUTH_INGEST`` is off.  AC5 keeps every phase-1
#: producer behind that switch, so with it off an empty log means "nothing is
#: writing", not "the fleet is quiet" — a distinction worth a line of output.
#:
#: Worded carefully, though: the CLI is a separate process from the server and
#: reads its OWN environment, so a shell without the variable cannot prove the
#: server is not ingesting.  The note says what is actually known and points at
#: the one check that settles it, rather than asserting a state it cannot see.
INGEST_OFF_NOTE = (
    "ingest off in this shell (CAO_WORKER_TRUTH_INGEST is not 1) — if the rows below "
    "look stale, check the server process's own environment"
)

_SEPARATOR = "-" * 96


@dataclass(frozen=True)
class DiagSources:
    """The three read-only stores every diag view needs.

    Built by the composition root, never by the CLI: AC9's fifth contract keeps
    ``cli`` from importing ``adapters`` at all.
    """

    events: EventStore
    states: StateStore
    findings: FindingStore
    #: The delivery queue, for ``cao diag <msg_id>`` (I5).  Optional because the
    #: message view is the only reader and a caller that does not need it should
    #: not have to open the queue's tables; a ``None`` here renders the worker
    #: events for the id and says plainly that the queue was not consulted,
    #: rather than reporting an empty queue as a fact.
    queue: QueueStore | None = None


# ---------------------------------------------------------------------- helpers


def _age(now: datetime, then: datetime | None) -> str:
    """Render a timestamp as an age, which is what an operator actually reads."""
    if then is None:
        return "never"
    seconds = (now - then).total_seconds()
    if seconds < 0:
        # A source clock ahead of the server's.  Say so rather than printing a
        # negative age that looks like a rendering bug.
        return f"{-seconds:.0f}s in the future"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _parse(value: Any) -> datetime | None:
    """Read a timestamp back out of the payload dict.

    The payload is the single source both views render from, so the text view
    has to reverse the ISO strings it put there.  Anything unparseable becomes
    ``None`` and prints as "never", which is the honest rendering of a column
    the store could not give us.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _clock(stamp: datetime) -> str:
    return stamp.strftime("%H:%M:%S.%f")[:-3]


def _gap(previous: datetime | None, current: datetime) -> str:
    if previous is None:
        return "".rjust(8)
    seconds = (current - previous).total_seconds()
    if seconds >= 100:
        return f"+{seconds:>6.0f}s"
    return f"+{seconds:>6.2f}s"


def _detail(event: WorkerEvent) -> str:
    """One compact line of the payload, chosen by kind.

    Deliberately not a JSON dump.  A diag view that prints every payload field
    stops being scannable at about six rows, and the whole point of the header is
    that the operator can already see the state.
    """
    payload = event.payload
    parts: list[str] = []

    if event.decision is not None:
        from_state = payload.get("from")
        to_state = payload.get("to")
        if from_state and to_state:
            parts.append(f"{from_state} -> {to_state}")
        rule = payload.get("rule")
        if rule:
            parts.append(f"rule={rule}")
        for key in ("from_reason", "to_reason", "degraded_reason", "outcome", "code", "reason"):
            value = payload.get(key)
            if value:
                parts.append(f"{key}={value}")
    else:
        for key in ("latched_status", "origin", "reason", "condition", "outcome"):
            value = payload.get(key)
            if value:
                parts.append(f"{key}={value}")

    if event.msg_id:
        parts.append(f"msg={event.msg_id}")
    if event.source_ref:
        parts.append(event.source_ref)
    if event.evidence:
        parts.append(f"<- {event.evidence}")
    return "  ".join(parts)


def _filter(
    rows: list[WorkerEvent], since: datetime | None, kinds: frozenset[AnyKind] | None
) -> list[WorkerEvent]:
    selected = rows
    if since is not None:
        selected = [row for row in selected if row.ingested_at >= since]
    if kinds is not None:
        selected = [row for row in selected if row.kind in kinds]
    return selected


def _last_disagreement(
    rows: list[WorkerEvent], state: WorkerState | None
) -> tuple[WorkerEvent, WorkerState] | None:
    """The most recent legacy publish whose status differs from the shadow state.

    Compared against the CURRENT shadow state rather than replaying the
    projection, because the single-worker view is a "what is wrong now" tool.
    The historical, per-interval version of this comparison is the agreement
    report, and duplicating its algorithm here would give an operator two numbers
    that could disagree.
    """
    if state is None:
        return None
    for event in reversed(rows):
        if event.decision is not None or event.kind is not EventKind.STATUS_LEGACY_PUBLISHED:
            continue
        raw = event.payload.get("latched_status")
        if not isinstance(raw, str):
            continue
        mapped = legacy_state(raw)
        if mapped is not None and mapped is not state:
            return event, mapped
    return None


# --------------------------------------------------------------------- timeline


def timeline_payload(
    sources: DiagSources,
    terminal_id: str,
    *,
    now: datetime,
    since: datetime | None = None,
    kinds: frozenset[AnyKind] | None = None,
    ingest_on: bool = True,
) -> dict[str, Any]:
    """The ``--json`` form of :func:`render_timeline`, and its data source."""
    rows = sources.events.read(terminal_id)
    projection = sources.states.get(terminal_id)
    shown = _filter(rows, since, kinds)
    disagreement = _last_disagreement(rows, projection.state if projection else None)

    header: dict[str, Any] = {"terminal_id": terminal_id, "projected": False}
    if projection is not None:
        header = {
            "terminal_id": terminal_id,
            "projected": True,
            "state": projection.state.value,
            "degraded_reason": (
                projection.degraded_reason.value if projection.degraded_reason is not None else None
            ),
            "prior_state": (
                projection.prior_state.value if projection.prior_state is not None else None
            ),
            "since": projection.since.isoformat() if projection.since else None,
            "last_event_seq": projection.last_event_seq,
            "last_probe_at": (
                projection.last_probe_at.isoformat() if projection.last_probe_at else None
            ),
            "last_source_probe_at": (
                projection.last_source_probe_at.isoformat()
                if projection.last_source_probe_at
                else None
            ),
            "pane_present": projection.pane_present,
            "pane_pid": projection.pane_pid,
            "miss_count": projection.miss_count,
        }

    return {
        "ingest_on": ingest_on,
        "generated_at": now.isoformat(),
        "header": header,
        "events": [
            {
                "seq": row.seq,
                "event_id": row.event_id,
                "kind": row.kind.value,
                "producer": row.producer.value,
                "confidence": row.confidence.value,
                "observed_at": row.observed_at.isoformat(),
                "ingested_at": row.ingested_at.isoformat(),
                "decision": row.decision.value if row.decision is not None else None,
                "evidence": row.evidence,
                "run_id": row.run_id,
                "msg_id": row.msg_id,
                "source_ref": row.source_ref,
                "payload": row.payload,
            }
            for row in shown
        ],
        "shown": len(shown),
        "total": len(rows),
        "last_legacy_disagreement": (
            None
            if disagreement is None
            else {
                "event_id": disagreement[0].event_id,
                "seq": disagreement[0].seq,
                "at": disagreement[0].ingested_at.isoformat(),
                "legacy": disagreement[1].value,
                "legacy_raw": disagreement[0].payload.get("latched_status"),
                "shadow": header.get("state"),
            }
        ),
    }


def render_timeline(
    sources: DiagSources,
    terminal_id: str,
    *,
    now: datetime,
    since: datetime | None = None,
    kinds: frozenset[AnyKind] | None = None,
    ingest_on: bool = True,
) -> str:
    """The human view: a liveness header, a chronological table, a footer.

    Rendered ENTIRELY from :func:`timeline_payload`, never from a second read of
    the stores.  Two reads could return different rows — the CLI opens the live
    database while the server is writing to it — and a text view that disagreed
    with its own ``--json`` would be worse than either alone.
    """
    data = timeline_payload(
        sources, terminal_id, now=now, since=since, kinds=kinds, ingest_on=ingest_on
    )
    header = data["header"]

    lines: list[str] = []
    if not ingest_on:
        lines.append(INGEST_OFF_NOTE)
    lines.append(f"terminal {terminal_id}")

    if not header["projected"]:
        lines.append("  shadow projection: none — this terminal has never been projected")
    else:
        state = header["state"]
        if header["degraded_reason"]:
            state = f"{state}({header['degraded_reason']})"
        lines.append(
            f"  state        {state}  since {_age(now, _parse(header['since']))}"
            f"  (seq {header['last_event_seq']})"
        )
        lines.append(
            f"  liveness     pane_present={header['pane_present']}"
            f"  pane_pid={header['pane_pid'] if header['pane_pid'] is not None else '-'}"
            f"  miss_count={header['miss_count']}"
            f"  prior_state={header['prior_state'] or '-'}"
        )
        lines.append(
            f"  last probe   {_age(now, _parse(header['last_probe_at']))}"
            f"    last source signal  {_age(now, _parse(header['last_source_probe_at']))}"
        )

    lines.append("")
    lines.append(
        f"{'seq':>5}  {'time':<12} {'gap':>8}  {'kind':<24} " f"{'producer':<8} {'conf':<14} detail"
    )
    lines.append(_SEPARATOR)

    rows = _filter(sources.events.read(terminal_id), since, kinds)
    if not rows:
        lines.append("  (no events match)")
    else:
        previous: datetime | None = None
        for row in rows:
            marker = "*" if row.decision is not None else " "
            lines.append(
                f"{row.seq:>5}{marker} {_clock(row.ingested_at):<12} "
                f"{_gap(previous, row.ingested_at)}  {row.kind.value:<24} "
                f"{row.producer.value:<8} {row.confidence.value:<14} {_detail(row)}".rstrip()
            )
            previous = row.ingested_at
        lines.append(_SEPARATOR)
        lines.append("  * = server decision row;  <- names the evidence event_id")

    lines.append(f"  {data['shown']} of {data['total']} rows shown")

    disagreement = data["last_legacy_disagreement"]
    if disagreement is None:
        lines.append("  last legacy disagreement: none")
    else:
        lines.append(
            f"  last legacy disagreement: shadow {disagreement['shadow']} vs legacy "
            f"{disagreement['legacy_raw']} at seq {disagreement['seq']} "
            f"({disagreement['event_id']})"
        )
    return "\n".join(lines)


# -------------------------------------------------------------------- why chain


def why_payload(sources: DiagSources, event_id: str, *, depth: int = 12) -> dict[str, Any]:
    """Walk ``evidence`` pointers back from one event.

    ``depth`` is a guard, not a preference.  The chain is acyclic by
    construction — evidence always points at an earlier row — but a corrupted or
    hand-edited row could close a loop, and a debugging command that hangs is a
    debugging command nobody runs twice.
    """
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = event_id

    while current is not None and len(chain) < depth:
        if current in seen:
            chain.append({"event_id": current, "error": "evidence cycle"})
            break
        seen.add(current)
        event = sources.events.get(current)
        if event is None:
            chain.append({"event_id": current, "error": "not found"})
            break
        chain.append(
            {
                "event_id": event.event_id,
                "seq": event.seq,
                "terminal_id": event.terminal_id,
                "kind": event.kind.value,
                "producer": event.producer.value,
                "confidence": event.confidence.value,
                "ingested_at": event.ingested_at.isoformat(),
                "decision": event.decision.value if event.decision is not None else None,
                "evidence": event.evidence,
                "source_ref": event.source_ref,
                "payload": event.payload,
            }
        )
        current = event.evidence

    return {"event_id": event_id, "chain": chain, "truncated": len(chain) >= depth}


def render_why(sources: DiagSources, event_id: str, *, depth: int = 12) -> str:
    data = why_payload(sources, event_id, depth=depth)
    lines = [f"evidence chain for {event_id}"]
    for index, link in enumerate(data["chain"]):
        indent = "  " * (index + 1)
        if "error" in link:
            lines.append(f"{indent}{link['event_id']}: {link['error']}")
            continue
        label = link["decision"] or link["kind"]
        # The id is on the line, not just in the JSON.  It is what an operator
        # pastes into the next ``cao diag --why`` and into the issue they file,
        # and a chain that names only kinds and sequence numbers forces them back
        # to the timeline to look it up.
        lines.append(
            f"{indent}{'<- ' if index else ''}{label}  seq {link['seq']}  "
            f"{link['producer']}/{link['confidence']}  {link['ingested_at']}  "
            f"{link['event_id']}"
        )
        detail = link["payload"]
        if detail:
            rendered = "  ".join(f"{key}={value}" for key, value in sorted(detail.items()))
            lines.append(f"{indent}   {rendered}")
    if data["truncated"]:
        lines.append(f"  (stopped at depth {depth})")
    if len(data["chain"]) == 1 and not data["chain"][0].get("evidence"):
        lines.append("  (this row cites no evidence — it is where the chain begins)")
    return "\n".join(lines)


# --------------------------------------------------------------------- findings


def findings_payload(
    sources: DiagSources, *, state: str | None = "open", code: FindingCode | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "finding_id": finding.finding_id,
            "code": finding.code.value,
            "terminal_id": finding.terminal_id,
            "dedupe_key": finding.dedupe_key,
            "detail": finding.detail,
            "count": finding.count,
            "state": finding.state.value,
            "first_seen_at": finding.first_seen_at.isoformat(),
            "last_seen_at": finding.last_seen_at.isoformat(),
            "sample_event_id": finding.sample_event_id,
        }
        for finding in sources.findings.list_findings(state=state, code=code)
    ]


def render_findings(
    sources: DiagSources,
    *,
    now: datetime,
    state: str | None = "open",
    code: FindingCode | None = None,
) -> str:
    findings: list[Finding] = sources.findings.list_findings(state=state, code=code)
    if not findings:
        return "no findings"

    lines = [
        f"{'code':<24} {'count':>6}  {'terminal':<16} {'last seen':<12} detail",
        _SEPARATOR,
    ]
    # Loudest first: a code seen four hundred times is the one to look at, and a
    # list sorted by insertion order buries it under whatever fired once.
    for finding in sorted(findings, key=lambda f: (-f.count, f.code.value)):
        lines.append(
            f"{finding.code.value:<24} {finding.count:>6}  "
            f"{(finding.terminal_id or '-'):<16} {_age(now, finding.last_seen_at):<12} "
            f"{finding.detail}".rstrip()
        )
        if finding.sample_event_id:
            lines.append(f"{'':<24} {'':>6}  sample {finding.sample_event_id}")
    return "\n".join(lines)


# -------------------------------------------------------------------- agreement


def render_agreement(report: AgreementReport) -> str:
    """The AC10 report as text.

    Validity is the first line, before any number, so a report that did not meet
    the content floor cannot be quoted out of context as a result.
    """
    lines: list[str] = []
    if report.valid:
        lines.append("AGREEMENT REPORT — VALID (content floor met)")
    else:
        lines.append("AGREEMENT REPORT — INVALID, no conclusion may be drawn")
        for reason in report.invalid_reasons:
            lines.append(f"  ! {reason}")

    rate = report.fleet_agreement_rate
    lines.append(
        f"  terminals={len(report.terminals)} (codex {report.codex_terminals})  "
        f"events={report.total_events}  transitions={report.total_transitions}  "
        f"legacy_publishes={report.total_legacy_publishes}"
    )
    # The pointwise rate counts ordinary lag as disagreement, because the two
    # sides are written by different producers and can never move in the same
    # instant.  Labelled rather than adjusted: an unlabelled 55% would read as a
    # broken projection, and a silently lag-corrected number would hide the very
    # thing the classifier below exists to expose.
    lines.append(
        "  pointwise agreement (lag counts against it): "
        + ("no comparable points" if rate is None else f"{rate:.1%} of {report.total_comparisons}")
    )
    counts = report.classification_counts()
    lines.append(
        f"  disagreements: projection_early={counts['projection_early']}  "
        f"legacy_early={counts['legacy_early']}  genuine={counts['genuine']}"
    )
    lines.append(
        "  the number that matters is genuine — the other two are one side "
        "arriving first and the other catching up"
    )

    lines.append("")
    lines.append(
        f"{'terminal':<16} {'provider':<12} {'rate':>7} {'cmp':>6} {'trans':>6} "
        f"{'legacy':>7}  disagreements"
    )
    lines.append(_SEPARATOR)
    for terminal in report.terminals:
        terminal_rate = terminal.agreement_rate
        rate_text = "n/a" if terminal_rate is None else f"{terminal_rate:.1%}"
        lines.append(
            f"{terminal.terminal_id:<16} {(terminal.provider or '-'):<12} {rate_text:>7} "
            f"{terminal.comparisons:>6} {terminal.transitions:>6} "
            f"{terminal.legacy_publishes:>7}  {len(terminal.disagreements)}"
        )

    genuine = [d for d in report.disagreements if d.classification == "genuine"]
    if genuine:
        lines.append("")
        lines.append("genuine disagreements (neither side was merely early):")
        for disagreement in genuine:
            duration = disagreement.duration_s
            span = "unresolved" if duration is None else f"{duration:.1f}s"
            lines.append(
                f"  {disagreement.terminal_id:<16} shadow {disagreement.projected.value} vs "
                f"legacy {disagreement.legacy.value}  {span}  "
                f"opened_by={disagreement.opened_by}  {disagreement.sample_event_id}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------- the message view


def message_payload(
    sources: DiagSources, msg_id: str, *, now: datetime, ingest_on: bool = True
) -> dict[str, Any]:
    """Everything stored about one ``msg_id``, as data (WP-ARCH phase 3a, I5).

    I5 is "one query returns a msg_id's full history", and the history has two
    halves that a reader needs together.  The queue row and its
    ``delivery_attempt`` rows say what the delivery machinery did.  The
    ``worker_event`` rows carrying the same ``msg_id`` say what the SERVER
    decided around it — phase 1 put the column on the log and the timeline
    renderer already shows it, so this view is the filter §7a asks for rather
    than a second store.

    Both halves, because either alone is the pane archaeology this replaces: the
    attempts without the decisions do not say why an injection was tried, and the
    decisions without the attempts do not say whether it landed.
    """
    message = None if sources.queue is None else sources.queue.get(msg_id)
    attempts = [] if sources.queue is None else sources.queue.attempts_for(msg_id)
    dead = None
    if sources.queue is not None:
        reader = getattr(sources.queue, "dead_letter", None)
        if reader is not None:
            dead = reader(msg_id)

    header: dict[str, Any] = {
        "found": message is not None,
        "queue_consulted": sources.queue is not None,
    }
    if message is not None:
        header.update(
            {
                "msg_id": message.msg_id,
                "state": message.state.value,
                "mode": message.mode.value,
                "receiver_id": message.receiver_id,
                "sender_id": message.sender_id,
                "kind": message.kind.value,
                "attempts": message.attempts,
                "max_attempts": message.max_attempts,
                "claim_id": message.claim_id,
                "lease_owner": message.lease_owner,
                "lease_expires_at": _iso(message.lease_expires_at),
                "available_at": _iso(message.available_at),
                # The deadline is stamped once at enqueue and never recomputed,
                # so printing it beside created_at is also the cheapest possible
                # audit of that rule: the gap is DELIVERY_MAX_LIFETIME_S, or the
                # caller's shorter expiry, and nothing else.
                "dead_by": _iso(message.dead_by),
                "held_since": _iso(message.held_since),
                "created_at": _iso(message.created_at),
                "terminated_at": _iso(message.terminated_at),
                "expire_after_s": message.expire_after_s,
                "supersede_key": message.supersede_key,
                "legacy_message_id": message.legacy_message_id,
                "is_notice": message.is_notice,
                "cancel_on_complete": message.cancel_on_complete,
            }
        )
    if dead is not None:
        header["dead_reason"] = dead.reason.value
        header["died_at"] = _iso(dead.died_at)

    events = sources.events.read()
    related = [row for row in events if row.msg_id == msg_id]

    return {
        "ingest_on": ingest_on,
        "generated_at": now.isoformat(),
        "header": header,
        "attempts": [
            {
                "claim_id": attempt.claim_id,
                "carrier": attempt.carrier,
                "started_at": attempt.started_at.isoformat(),
                "outcome": attempt.outcome.value,
                "detail": attempt.detail,
            }
            for attempt in attempts
        ],
        "events": [
            {
                "seq": row.seq,
                "event_id": row.event_id,
                "terminal_id": row.terminal_id,
                "kind": row.kind.value,
                "producer": row.producer.value,
                "confidence": row.confidence.value,
                "ingested_at": row.ingested_at.isoformat(),
                "decision": row.decision.value if row.decision is not None else None,
                "evidence": row.evidence,
                "payload": row.payload,
            }
            for row in related
        ],
    }


def render_message(
    sources: DiagSources, msg_id: str, *, now: datetime, ingest_on: bool = True
) -> str:
    """The human view of one message.  Rendered entirely from the payload.

    Two reads of the stores could return different rows — the CLI opens the live
    database while the server is writing to it — and a text view that disagreed
    with its own ``--json`` would be worse than either alone.
    """
    data = message_payload(sources, msg_id, now=now, ingest_on=ingest_on)
    header = data["header"]
    lines: list[str] = []
    if not ingest_on:
        lines.append(INGEST_OFF_NOTE)
    lines.append(f"message {msg_id}")

    if not header["queue_consulted"]:
        lines.append("  delivery queue: not consulted (no queue store was opened)")
    elif not header["found"]:
        lines.append("  delivery queue: no row with this id")
    else:
        state = header["state"]
        if header.get("dead_reason"):
            state = f"{state}({header['dead_reason']})"
        lines.append(
            f"  state        {state}  mode={header['mode']}  kind={header['kind']}"
            f"  attempts={header['attempts']}/{header['max_attempts']}"
            f"  claim_id={header['claim_id']}"
        )
        lines.append(f"  receiver     {header['receiver_id']}  from {header['sender_id'] or '-'}")
        lines.append(
            f"  created      {_age(now, _parse(header['created_at']))}"
            f"   deadline {_age(now, _parse(header['dead_by']))}"
            f"   available {_age(now, _parse(header['available_at']))}"
        )
        if header.get("held_since"):
            lines.append(f"  dialog hold  since {_age(now, _parse(header['held_since']))}")
        if header.get("terminated_at"):
            lines.append(f"  ended        {_age(now, _parse(header['terminated_at']))}")
        if header.get("legacy_message_id") is not None:
            lines.append(f"  mirrors      legacy inbox row {header['legacy_message_id']}")

    lines.append(_SEPARATOR)
    if not data["attempts"]:
        lines.append("no delivery attempts recorded")
    else:
        lines.append(f"{'claim':>6}  {'carrier':<16}  {'started':<14}  {'outcome':<16}  detail")
        for attempt in data["attempts"]:
            started = _parse(attempt["started_at"])
            lines.append(
                f"{attempt['claim_id']:>6}  {attempt['carrier'][:16]:<16}  "
                f"{(_clock(started) if started else '-'):<14}  "
                f"{attempt['outcome']:<16}  {attempt['detail']}"
            )

    lines.append(_SEPARATOR)
    if not data["events"]:
        lines.append("no worker events carry this msg_id")
    else:
        lines.append(f"{'seq':>6}  {'terminal':<20}  {'kind':<22}  {'producer':<10}  decision")
        for row in data["events"]:
            lines.append(
                f"{row['seq']:>6}  {row['terminal_id'][:20]:<20}  {row['kind'][:22]:<22}  "
                f"{row['producer']:<10}  {row['decision'] or '-'}"
            )
    return "\n".join(lines)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
