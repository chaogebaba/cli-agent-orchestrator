"""F611 (#467) — runtime WIRING integration arms.

The AC1-AC15 arms prove the classifier/delivery logic in isolation. These arms
prove the classifier is actually CONNECTED to production execution (gate r1 B1):

1. the status-monitor transition seam invokes `classify_condition` on the
   provider and drives the ONE delivery seam;
2. the fleet `condition` field is set (§3 surface 1) and read back by the same
   getter `terminal_service.get_terminal` consumes;
3. `ConditionDelivery` PERFORMS the fan-out — exactly one supervisor inbox push
   (§3 surface 2) and one CLI/bus projection (§3 surface 3) per transition,
   de-duped per epoch (D4).

Wiring mutant recipe (documented + exercised): dropping the poll-site call
(`_classify_and_deliver_condition` becomes a no-op) leaves the fleet field unset
and fires zero inbox pushes → `test_wiring_transition_sets_all_three_surfaces`
goes RED.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.condition import (
    Condition,
    ConditionDelivery,
    ConditionKind,
    Confidence,
    classify_condition,
)

_COND = Path(__file__).parent / "fixtures" / "conditions"


def _load(name: str) -> str:
    return (_COND / f"{name}.txt").read_text(encoding="utf-8")


class _FakeProvider:
    """A provider whose classify_condition delegates to the real engine for a
    given key — stands in for a live CodexProvider/etc. at the poll site."""

    def __init__(self, key: str) -> None:
        self._key = key

    def classify_condition(self, pane: str, **kw: object) -> object:
        return classify_condition(pane, self._key)


def _recording_delivery() -> tuple[ConditionDelivery, dict]:
    """Build a ConditionDelivery whose three sinks record their effects."""
    rec: dict = {"fleet": [], "inbox": [], "cli": []}
    delivery = ConditionDelivery(
        fleet_sink=lambda tid, label: rec["fleet"].append((tid, label)),
        inbox_sink=lambda tid, cond: rec["inbox"].append((tid, cond.kind.value)),
        cli_sink=lambda tid, cond, label: rec["cli"].append((tid, label)),
    )
    return delivery, rec


# ── The three surfaces are PERFORMED (not just modeled) from ONE event ─────────
def test_delivery_performs_all_three_surfaces() -> None:
    delivery, rec = _recording_delivery()
    cond = classify_condition(_load("codex-capped-1"), "codex")
    assert cond is not None
    r = delivery.deliver("aaaaaaaa", cond, epoch=1)
    assert r.delivered is True
    assert rec["fleet"] == [("aaaaaaaa", "CAPPED")]
    assert rec["inbox"] == [("aaaaaaaa", "CAPPED")]  # exactly ONE inbox push
    assert rec["cli"] == [("aaaaaaaa", "CAPPED")]  # exactly ONE CLI projection


def test_delivery_dedup_suppresses_extra_inbox_and_cli() -> None:
    delivery, rec = _recording_delivery()
    cond = classify_condition(_load("codex-capped-1"), "codex")
    assert cond is not None
    delivery.deliver("bbbbbbbb", cond, epoch=5)
    delivery.deliver("bbbbbbbb", cond, epoch=5)  # same tuple, same epoch
    assert len(rec["inbox"]) == 1  # D4: one push per transition, not per pass
    assert len(rec["cli"]) == 1
    # A new epoch re-arms all three.
    delivery.deliver("bbbbbbbb", cond, epoch=6)
    assert len(rec["inbox"]) == 2


def test_delivery_low_confidence_touches_no_surface() -> None:
    delivery, rec = _recording_delivery()
    low = Condition(
        ConditionKind.PROC_EXITED, "cline_cli", "shell_baseline_return", "x", Confidence.LOW
    )
    r = delivery.deliver("cccccccc", low, epoch=1)
    assert r.delivered is False
    assert rec["fleet"] == [] and rec["inbox"] == [] and rec["cli"] == []


def test_delivery_none_clears_fleet_only() -> None:
    delivery, rec = _recording_delivery()
    delivery.deliver("dddddddd", None, epoch=1)
    assert rec["fleet"] == [("dddddddd", None)]  # clear label
    assert rec["inbox"] == [] and rec["cli"] == []


# ── The status-monitor poll seam invokes detection + delivery ──────────────────
def test_wiring_transition_sets_all_three_surfaces() -> None:
    """The production seam `_classify_and_deliver_condition` (called from the
    published-transition block in `_apply_detection`) classifies the pane and
    drives the ONE delivery seam. Fleet field set + one inbox + one CLI. This is
    the arm the wiring mutant (poll-site call dropped) turns RED."""
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    sm = StatusMonitor()
    tid = "beadfeed"
    # Inject a recording delivery so we observe the fan-out without a live DB.
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery
    sm._buffer_epochs[tid] = 3

    provider = _FakeProvider("codex")
    sm._classify_and_deliver_condition(tid, provider, _load("codex-capped-1"))

    assert rec["fleet"] == [(tid, "CAPPED")], "fleet condition field must be set"
    assert rec["inbox"] == [(tid, "CAPPED")], "exactly one supervisor inbox push"
    assert rec["cli"] == [(tid, "CAPPED")], "exactly one CLI/bus projection"


def test_wiring_fleet_field_read_back_by_getter() -> None:
    """The live fleet field the seam sets is read back by `get_condition` — the
    exact getter `terminal_service.get_terminal` uses to populate
    `Terminal.condition` (§3 surface 1 / CLI projection)."""
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    sm = StatusMonitor()
    tid = "feedface"
    # Use the REAL delivery with the monitor's own production fleet sink.
    sm._buffer_epochs[tid] = 1
    # Provide a benign inbox sink (no caller) and real fleet/cli sinks by using
    # the lazily-built production delivery, but stub the inbox to avoid DB.
    delivery = ConditionDelivery(
        fleet_sink=sm._condition_fleet_sink,
        inbox_sink=lambda tid_, cond_: None,
        cli_sink=lambda tid_, cond_, label_: None,
    )
    sm._condition_delivery = delivery

    provider = _FakeProvider("codex")
    sm._classify_and_deliver_condition(tid, provider, _load("codex-capped-1"))
    assert sm.get_condition(tid) == "CAPPED"

    # A subsequent BUSY-only pane transition updates the live field (new epoch).
    sm._buffer_epochs[tid] = 2
    sm._classify_and_deliver_condition(tid, provider, _load("kiro-cli-busy-1"))
    # kiro-cli-busy is a kiro pane; codex provider yields None → field cleared.
    assert sm.get_condition(tid) is None


def test_wiring_no_provider_is_safe_noop() -> None:
    """The seam tolerates a missing provider / classify hook without raising."""
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    sm = StatusMonitor()
    sm._classify_and_deliver_condition("00000000", None, "whatever")  # no raise

    class _NoHook:
        pass

    sm._classify_and_deliver_condition("00000000", _NoHook(), "whatever")  # no raise


# ── The PRODUCTION poll site: a transition through _apply_detection fans out ────
def test_apply_detection_transition_drives_condition_fanout(monkeypatch) -> None:
    """Drive a real status transition through `_apply_detection` (the production
    poll site at status_monitor.py's publish_external block) and assert the
    condition fan-out fired. This is the arm that the LITERAL mutant — replacing
    the `_classify_and_deliver_condition(...)` call at the transition seam with
    `pass` — turns RED. Every dependency the seam reaches on the transition path
    (metadata read, provider lookup, children reconcile) is stubbed so the test
    needs no live tmux/DB; the condition delivery uses a recording fake."""
    import cli_agent_orchestrator.services.status_monitor as sm_mod
    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    sm = StatusMonitor()
    tid = "cafebabe"

    # Recording delivery injected as the ONE production delivery seam.
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery

    # The live pane buffer the seam classifies = a CAPPED codex pane.
    sm._buffers[tid] = _load("codex-capped-1")
    sm._buffer_epochs[tid] = 1
    # Seed a prior status so COMPLETED is a genuine transition (publish_external).
    sm._last_status[tid] = TerminalStatus.IDLE

    # Stub the transition-path collaborators (no live tmux/DB).
    monkeypatch.setattr(
        sm_mod.provider_manager, "get_provider", lambda _tid: _FakeProvider("codex")
    )
    import cli_agent_orchestrator.clients.database as db_mod

    monkeypatch.setattr(db_mod, "get_terminal_metadata", lambda _tid: {"id": _tid})
    # _publish_observation / children reconcile / auto_responder must not need a DB.
    monkeypatch.setattr(sm, "_publish_observation", lambda *a, **k: None)
    monkeypatch.setattr(db_mod, "reconcile_children_on_publish", lambda *a, **k: None)

    # Drive the real production path.
    sm._apply_detection(tid, TerminalStatus.COMPLETED)

    assert rec["fleet"] == [(tid, "CAPPED")], "transition must set the fleet field"
    assert rec["inbox"] == [(tid, "CAPPED")], "transition must fire exactly one inbox push"
    assert rec["cli"] == [(tid, "CAPPED")], "transition must fire one CLI projection"


def test_apply_detection_no_transition_does_not_fire(monkeypatch) -> None:
    """A no-change pass (detected == last) does not publish, so the condition
    seam is not invoked — the fan-out is per TRANSITION, not per poll."""
    import cli_agent_orchestrator.services.status_monitor as sm_mod
    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    sm = StatusMonitor()
    tid = "d00dfeed"
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery
    sm._buffers[tid] = _load("codex-capped-1")
    sm._last_status[tid] = TerminalStatus.COMPLETED  # already COMPLETED
    monkeypatch.setattr(
        sm_mod.provider_manager, "get_provider", lambda _tid: _FakeProvider("codex")
    )
    monkeypatch.setattr(sm, "_publish_observation", lambda *a, **k: None)

    sm._apply_detection(tid, TerminalStatus.COMPLETED)  # no transition
    assert rec["fleet"] == [] and rec["inbox"] == [] and rec["cli"] == []


# ── B2: the /sessions/{name}/fleet RESPONSE carries the condition (§3 surface 1) ─
def test_fleet_response_carries_condition_after_transition(monkeypatch) -> None:
    """`fleet_service.build_fleet` projects the live condition onto each terminal
    row, so `/sessions/{name}/fleet` carries it (blueprint §3 surface 1). The
    mutant that drops the `condition` projection turns this arm RED."""
    import cli_agent_orchestrator.services.fleet_service as fs
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    tid = "0ddba11c"
    session = "sess-f611"

    # One terminal row; no native window inventory (keeps the projection simple).
    monkeypatch.setattr(
        fs,
        "list_terminals_by_session",
        lambda _s: [
            {
                "id": tid,
                "agent_profile": "dev",
                "provider": "codex",
                "tmux_window": "w0",
                "caller_id": None,
                "last_active": None,
                "lifecycle": "ephemeral",
            }
        ],
    )

    class _Backend:
        def get_session_windows(self, _s):
            return []

    monkeypatch.setattr(fs, "get_backend", lambda: _Backend())
    # Seed the live condition on the production singleton the projection reads.
    status_monitor._condition_fleet_sink(tid, "CAPPED")
    try:
        fleet = fs.build_fleet(session)
        rows = {r["id"]: r for r in fleet["terminals"]}
        assert tid in rows
        assert "condition" in rows[tid], "fleet row must carry a condition key"
        assert rows[tid]["condition"] == "CAPPED"
    finally:
        status_monitor._condition_fleet_sink(tid, None)  # clear seeded state


def test_fleet_response_condition_none_when_absent(monkeypatch) -> None:
    """With no condition detected, the fleet row's condition key is present and
    None (a stable key the TUI/consumers can rely on)."""
    import cli_agent_orchestrator.services.fleet_service as fs

    tid = "beeff00d"
    monkeypatch.setattr(
        fs,
        "list_terminals_by_session",
        lambda _s: [
            {
                "id": tid,
                "agent_profile": "dev",
                "provider": "codex",
                "tmux_window": "w0",
                "caller_id": None,
                "last_active": None,
                "lifecycle": "ephemeral",
            }
        ],
    )

    class _Backend:
        def get_session_windows(self, _s):
            return []

    monkeypatch.setattr(fs, "get_backend", lambda: _Backend())
    fleet = fs.build_fleet("sess-f611b")
    rows = {r["id"]: r for r in fleet["terminals"]}
    assert rows[tid]["condition"] is None


# ── #700/#701 r2: the PI provider's condition OPT-IN is wired end-to-end ────────
#
# The codex EMPIRICAL-GATE-NO found the #700/#701 wiring gap: every existing arm
# above drives the seam with a `_FakeProvider("codex")` (a fake key-based stand-
# in) or calls the module `classify_condition(...)` directly, so NONE asserts the
# Pi provider's own `condition_provider_key = "pi_cli"` opt-in. The ledger's OWN
# mutant (M6: `condition_provider_key` → None) therefore SURVIVED. This arm
# instantiates a REAL `PiCliProvider` and drives its verbatim 429 pane through
# the production `_classify_and_deliver_condition` seam; it goes RED when the
# opt-in is None (BaseProvider.classify_condition returns None → zero surfaces).

# The verbatim ClinePass 429 banner from the live pi panes (issue #700/#701).
_PI_BANNER = (
    'Error: 429: {"code":"INFERENCE_CAP_ERROR","message":"Error 429: You have '
    "reached your 5-hour Clinepass limit. The limit resets in 1h 29m, please try "
    'again later."}'
)
_PI_RETRY_FAILED = (
    'Error: Retry failed after 3 attempts: 429: {"code":"INFERENCE_CAP_ERROR",'
    '"message":"Error 429: You have reached your 5-hour Clinepass limit. The '
    'limit resets in 1h 29m, please try again later."}'
)


def _pi_capped_pane() -> str:
    """A live pi capped pane: the 429 banner storm above the idle composer."""
    rule = "─" * 120
    footer = "↑26k ↓172 R3.9k CH97.6% $0.002 0.4%/1.0M (auto)   cline-pass/glm-5.3-flash • high"
    return "\n".join(
        [
            " Call the probe tool. Then say DONE.",
            "",
            _PI_BANNER,
            _PI_BANNER,
            _PI_RETRY_FAILED,
            "",
            rule,
            " ",
            rule,
            "/data/scratch/probe",
            footer,
        ]
    )


def test_pi_cli_condition_wiring_delivers_capped_once() -> None:
    """A REAL PiCliProvider driven through the status-monitor transition seam
    delivers exactly one CAPPED event to the fleet field, the supervisor inbox,
    and the CLI projection — because it opts in via
    ``condition_provider_key = "pi_cli"``. This arm turns RED when that opt-in is
    None (the ledger's SURVIVED M6 mutant): BaseProvider.classify_condition then
    returns None and no surface fires."""
    from cli_agent_orchestrator.providers.pi_cli import PiCliProvider
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    sm = StatusMonitor()
    tid = "9a9a9a9a"
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery
    sm._buffer_epochs[tid] = 1

    # A REAL provider instance (not a fake key stand-in) — its own class-level
    # condition_provider_key is what routes the pane to the CAPPED classifier.
    provider = PiCliProvider(tid, "sess", "win0")
    assert (
        type(provider).condition_provider_key == "pi_cli"
    ), "precondition: pi opts into the F611 seam; the M6 mutant sets this None"

    sm._classify_and_deliver_condition(tid, provider, _pi_capped_pane())

    assert rec["fleet"] == [(tid, "CAPPED")], "fleet condition field must be set for pi"
    assert rec["inbox"] == [(tid, "CAPPED")], "exactly one supervisor inbox push for pi"
    assert rec["cli"] == [(tid, "CAPPED")], "exactly one CLI/bus projection for pi"


# ── #700/#701 r3: the PRODUCTION inbox sink writes a real seat inbox ROW ───────
#
# The codex r2 EMPIRICAL-GATE-NO on #700/#701: the r2 arm above proves the
# fan-out with a RECORDING delivery (a tuple recorder) — it never drives the
# classified event through the PRODUCTION inbox sink
# (``StatusMonitor._condition_inbox_sink``) to a real supervisor SEAT inbox row.
# The adjudication brief explicitly requires an arm that reaches
# ``create_inbox_message`` with real caller metadata and reads back exactly one
# ``[CONDITION]`` row (sender = worker, receiver = seat) in an isolated DB. This
# fixture + arm are that.


@pytest.fixture
def isolated_inbox_db(tmp_path, monkeypatch):
    """Route the whole DB layer to a fresh, initialized per-test SQLite file.

    ``_condition_inbox_sink`` resolves the caller via ``get_terminal_metadata``
    and writes with ``create_inbox_message`` — both use the module-level
    ``database.SessionLocal``. Swapping it here (mirroring the top-level
    ``isolated_memory_db`` fixture) gives a real, isolated inbox table the arm
    can query, without touching the suite DB or any production database. The
    metadata TTL cache is cleared on both edges so a prior test's row cannot leak
    in and this test's rows cannot leak out.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients import database

    engine = create_engine(
        f"sqlite:///{tmp_path / 'cao-inbox.db'}",
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    database.clear_terminal_metadata_cache()
    try:
        yield engine
    finally:
        database.clear_terminal_metadata_cache()
        engine.dispose()


def test_pi_cli_condition_wiring_writes_one_seat_inbox_row(isolated_inbox_db) -> None:
    """#700/#701 r3 (codex r2 EMPIRICAL-GATE-NO): a REAL PiCliProvider 429 pane
    driven through classify → the PRODUCTION ``_condition_inbox_sink`` (with the
    worker's real recorded ``caller_id`` metadata) writes EXACTLY ONE real
    ``create_inbox_message`` ``[CONDITION]`` row — sender = worker, receiver =
    seat — in an isolated fixture DB. This is the committed seat-row wiring arm
    the r2 recording-delivery arm lacked.

    Kills the stage-A ledger's SURVIVED M6 mutant
    (``PiCliProvider.condition_provider_key`` → None): classify then returns None,
    the sink is handed None, ``create_inbox_message`` is never called, and the
    row-count assertion below goes RED (0 rows)."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.clients.database import InboxModel
    from cli_agent_orchestrator.providers.pi_cli import PiCliProvider
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    worker = "worker70"
    seat = "seat7000"
    # Real terminal rows: the supervisor seat and the worker whose caller_id is
    # that seat — this is the metadata the production sink reads to route.
    database.create_terminal(seat, "sess", "w-seat", "claude_code", agent_profile="supervisor")
    database.create_terminal(
        worker, "sess", "w-work", "pi_cli", agent_profile="dev", caller_id=seat
    )

    sm = StatusMonitor()
    sm._buffer_epochs[worker] = 1
    # The ONE delivery seam, wired to the PRODUCTION inbox sink (not a recorder).
    # Fleet/CLI sinks are benign no-ops: this arm is specifically the seat inbox
    # row, which only ``_condition_inbox_sink`` can produce.
    delivery = ConditionDelivery(
        fleet_sink=lambda tid_, label_: None,
        inbox_sink=sm._condition_inbox_sink,
        cli_sink=lambda tid_, cond_, label_: None,
    )
    sm._condition_delivery = delivery

    provider = PiCliProvider(worker, "sess", "w-work")
    assert (
        type(provider).condition_provider_key == "pi_cli"
    ), "precondition: pi opts into the F611 seam; the M6 mutant sets this None"

    sm._classify_and_deliver_condition(worker, provider, _pi_capped_pane())

    # Exactly ONE real inbox row, routed worker → seat, carrying the typed event.
    with database.SessionLocal() as db:
        rows = db.query(InboxModel).all()
        assert len(rows) == 1, f"expected exactly one [CONDITION] seat inbox row, got {len(rows)}"
        row = rows[0]
        assert row.sender_id == worker, "sender must be the worker terminal"
        assert row.receiver_id == seat, "receiver must be the supervisor seat"
        assert row.message.startswith(
            f"[CONDITION] terminal={worker} kind=CAPPED provider=pi_cli"
        ), f"row body must be the typed CAPPED condition event, got: {row.message[:80]!r}"


def test_pi_cli_condition_wiring_read_back_by_getter() -> None:
    """The live fleet field the seam sets for a real PiCliProvider is read back by
    ``get_condition`` (the getter terminal_service.get_terminal uses). A second,
    non-capped pane transition clears it — proving it is the live field, not a
    latched artifact."""
    from cli_agent_orchestrator.providers.condition import ConditionDelivery
    from cli_agent_orchestrator.providers.pi_cli import PiCliProvider
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    sm = StatusMonitor()
    tid = "9b9b9b9b"
    sm._buffer_epochs[tid] = 1
    delivery = ConditionDelivery(
        fleet_sink=sm._condition_fleet_sink,
        inbox_sink=lambda tid_, cond_: None,
        cli_sink=lambda tid_, cond_, label_: None,
    )
    sm._condition_delivery = delivery

    provider = PiCliProvider(tid, "sess", "win0")
    sm._classify_and_deliver_condition(tid, provider, _pi_capped_pane())
    assert sm.get_condition(tid) == "CAPPED"

    # A clean composer pane (no 429 banner) → no condition → field cleared.
    sm._buffer_epochs[tid] = 2
    rule = "─" * 120
    footer = "↑1k ↓1k R1k CH1% $0.001 0.1%/1.0M (auto)   cline-pass/glm-5.3-flash • high"
    clean_pane = "\n".join([" All done.", "", rule, " ", rule, footer])
    sm._classify_and_deliver_condition(tid, provider, clean_pane)
    assert sm.get_condition(tid) is None
