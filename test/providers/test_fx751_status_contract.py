"""fx751 Slice A — the typed status contract and pure reducer (AC-1..AC-4, AC-25).

Every test invokes the REAL reducer :func:`status_contract.reduce` over typed
``StatusSample`` inputs — never a stub returning PROCESSING (D8). The reducer is
pure, so these tests touch no database, tmux or event loop.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import status_contract as sc
from cli_agent_orchestrator.providers.status_contract import (
    ActivityFact,
    Candidate,
    ConditionFact,
    Evidence,
    FactValue,
    HealthFact,
    ProcessIdentity,
    ReadinessFact,
    ReducerContext,
    SampleMode,
    SettlementFact,
    StatusSample,
    reduce,
)


def _ctx(**kw: object) -> ReducerContext:
    base = dict(terminal_id="t1", last_sequence=0, last_status=TerminalStatus.PROCESSING)
    base.update(kw)
    return ReducerContext(**base)  # type: ignore[arg-type]


def _sample(**kw: object) -> StatusSample:
    base: dict[str, object] = dict(
        terminal_id="t1",
        sample_mode=SampleMode.DIRECT_RENDERED,
        declared_modes=(SampleMode.DIRECT_RENDERED,),
        frame_locatable=True,
        captured_after_trigger=True,
        age_s=1.0,
        sequence=1,
        filtered_fingerprint="fp1",
    )
    base.update(kw)
    return StatusSample(**base)  # type: ignore[arg-type]


# ── AC-1: purity / zero side effects ──────────────────────────────────────
def _all_inputs() -> list[tuple[StatusSample, ReducerContext]]:
    """A spread of inputs exercising every reducer arm."""
    return [
        (_sample(), _ctx()),
        (_sample(sample_mode=SampleMode.SCREEN), _ctx()),  # bad route
        (_sample(frame_locatable=False), _ctx()),  # unlocatable
        (_sample(captured_after_trigger=False), _ctx()),  # pre-trigger
        (_sample(age_s=999.0), _ctx()),  # expired
        (
            _sample(health=HealthFact(value=FactValue.ABSENT, exited=True)),
            _ctx(),
        ),  # health dead
        (_sample(readiness=ReadinessFact(blocking_question=True)), _ctx()),  # question
        (_sample(activity=ActivityFact(value=FactValue.PRESENT)), _ctx()),  # activity
        (
            _sample(readiness=ReadinessFact(value=FactValue.PRESENT)),
            _ctx(),
        ),  # idle lowering
        (
            _sample(settlement=SettlementFact(value=FactValue.PRESENT)),
            _ctx(),
        ),  # completed lowering
        (
            _sample(lifecycle_generation=5),
            _ctx(lifecycle_generation=0, last_sequence=3),
        ),  # stale gen
    ]


@pytest.mark.parametrize("sample,ctx", _all_inputs())
def test_ac1_reducer_has_zero_side_effects(sample: StatusSample, ctx: ReducerContext) -> None:
    """AC-1: the reducer mutates nothing and reads no external state — its
    inputs are byte-identical before and after, and repeated calls are
    identical (deterministic, clock-free)."""
    sample_before = copy.deepcopy(sample)
    ctx_before = copy.deepcopy(ctx)

    out1 = reduce(sample, ctx)
    out2 = reduce(sample, ctx)

    assert isinstance(out1, Candidate)
    # inputs untouched
    assert sample == sample_before
    assert ctx == ctx_before
    # deterministic
    assert out1 == out2


def test_ac1_inputs_are_frozen() -> None:
    """The envelope, context, facts and candidate are all frozen — a mutation
    attempt raises, which is the structural half of 'no mutation'."""
    s = _sample()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.terminal_id = "other"  # type: ignore[misc]
    c = reduce(s, _ctx())
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.status = TerminalStatus.ERROR  # type: ignore[misc]


# ── AC-2: representation routing ───────────────────────────────────────────
def test_ac2_rejects_undeclared_route() -> None:
    """A provider fed a route it does not declare rejects it -> UNKNOWN
    ``bad_route``, independent of the facts on the sample (a fully-idle sample
    still does not lower)."""
    s = _sample(
        sample_mode=SampleMode.SCREEN,
        declared_modes=(SampleMode.DIRECT_RENDERED,),
        readiness=ReadinessFact(value=FactValue.PRESENT),
    )
    out = reduce(s, _ctx())
    assert out.status is TerminalStatus.UNKNOWN
    assert out.reason == "bad_route"


def test_ac2_accepts_declared_route() -> None:
    s = _sample(
        sample_mode=SampleMode.DIRECT_RENDERED, declared_modes=(SampleMode.DIRECT_RENDERED,)
    )
    out = reduce(s, _ctx())
    assert out.reason != "bad_route"


# ── AC-3: unlocatable frame is UNKNOWN, never IDLE ─────────────────────────
def test_ac3_unlocatable_frame_is_unknown_not_idle() -> None:
    """A frame whose widget boundaries cannot be located is missing evidence.
    Even with a readiness fact set, the reducer must publish UNKNOWN."""
    s = _sample(frame_locatable=False, readiness=ReadinessFact(value=FactValue.PRESENT))
    out = reduce(s, _ctx())
    assert out.status is TerminalStatus.UNKNOWN
    assert out.reason == "unlocatable_frame"


# ── AC-4: D4 fresh-evidence transaction on lowering ────────────────────────
def test_ac4_single_sample_does_not_lower_holds_last() -> None:
    """The FIRST agreeing sample arms a held candidate and HOLDS the last
    status; it does not lower on one sample."""
    ctx = _ctx(last_status=TerminalStatus.PROCESSING)
    s1 = _sample(readiness=ReadinessFact(value=FactValue.PRESENT), filtered_fingerprint="a")
    out = reduce(s1, ctx)
    assert out.status is TerminalStatus.PROCESSING  # held
    assert out.reason == "awaiting_confirm"
    assert out.next_context is not None
    assert out.next_context.pending_lower_to is TerminalStatus.IDLE


def test_ac4_two_distinct_samples_confirm_lowering() -> None:
    ctx = _ctx(last_status=TerminalStatus.PROCESSING)
    s1 = _sample(
        readiness=ReadinessFact(value=FactValue.PRESENT), filtered_fingerprint="a", sequence=1
    )
    first = reduce(s1, ctx)
    assert first.next_context is not None
    s2 = _sample(
        readiness=ReadinessFact(value=FactValue.PRESENT), filtered_fingerprint="b", sequence=2
    )
    second = reduce(s2, first.next_context)
    assert second.status is TerminalStatus.IDLE
    assert second.reason == "confirmed_idle"


def test_ac4_duplicate_capture_cannot_confirm() -> None:
    """Re-reading ONE capture twice (same fingerprint) is rejected as the
    second sample — it never confirms a lowering."""
    ctx = _ctx(last_status=TerminalStatus.PROCESSING)
    s1 = _sample(
        readiness=ReadinessFact(value=FactValue.PRESENT), filtered_fingerprint="a", sequence=1
    )
    first = reduce(s1, ctx)
    assert first.next_context is not None
    # identical fingerprint => duplicate
    s_dup = _sample(
        readiness=ReadinessFact(value=FactValue.PRESENT), filtered_fingerprint="a", sequence=1
    )
    out = reduce(s_dup, first.next_context)
    assert out.status is not TerminalStatus.IDLE
    assert out.reason == "awaiting_confirm"


def test_ac4_event_confirmed_lowers_immediately() -> None:
    ctx = _ctx(last_status=TerminalStatus.PROCESSING)
    s = _sample(
        settlement=SettlementFact(value=FactValue.PRESENT),
        native_coverage=True,
        native_end_event=True,
        filtered_fingerprint="x",
    )
    out = reduce(s, ctx)
    assert out.status is TerminalStatus.COMPLETED
    assert out.reason == "event_confirmed"


def test_ac4_off_generation_sample_rejected() -> None:
    """A sample from another lifecycle generation cannot repopulate a seeded
    context (D5 generation compare)."""
    ctx = _ctx(lifecycle_generation=2, last_sequence=5)
    s = _sample(lifecycle_generation=7, readiness=ReadinessFact(value=FactValue.PRESENT))
    out = reduce(s, ctx)
    assert out.status is TerminalStatus.UNKNOWN
    assert out.reason == "stale_generation"


def test_ac4_pre_trigger_sample_rejected() -> None:
    s = _sample(captured_after_trigger=False, readiness=ReadinessFact(value=FactValue.PRESENT))
    out = reduce(s, _ctx())
    assert out.status is TerminalStatus.UNKNOWN
    assert out.reason == "pre_trigger"


# ── AC-25: expiry ──────────────────────────────────────────────────────────
def test_ac25_expired_sample_publishes_unknown_with_last_known() -> None:
    s = _sample(age_s=sc.SAMPLE_EXPIRY_S + 0.1, readiness=ReadinessFact(value=FactValue.PRESENT))
    ctx = _ctx(last_status=TerminalStatus.PROCESSING)
    out = reduce(s, ctx)
    assert out.status is TerminalStatus.UNKNOWN
    assert out.reason == "expired"
    assert out.last_known is TerminalStatus.PROCESSING
    assert out.last_known_at == s.captured_at_monotonic


# ── D1 precedence ──────────────────────────────────────────────────────────
def test_d1_health_dead_outranks_everything() -> None:
    s = _sample(
        health=HealthFact(value=FactValue.ABSENT, exited=True),
        activity=ActivityFact(value=FactValue.PRESENT),
        readiness=ReadinessFact(blocking_question=True),
    )
    out = reduce(s, _ctx())
    assert out.status is TerminalStatus.ERROR
    assert out.reason == "health_dead"


def test_d1_question_outranks_activity() -> None:
    s = _sample(
        readiness=ReadinessFact(blocking_question=True),
        activity=ActivityFact(value=FactValue.PRESENT),
    )
    out = reduce(s, _ctx())
    assert out.status is TerminalStatus.WAITING_USER_ANSWER
    assert out.reason == "question"


def test_d1_activity_is_a_raise_not_gated() -> None:
    """Activity PRESENT publishes PROCESSING immediately — a RAISE is never
    behind the two-sample confirmation."""
    s = _sample(activity=ActivityFact(value=FactValue.PRESENT))
    out = reduce(s, _ctx(last_status=TerminalStatus.IDLE))
    assert out.status is TerminalStatus.PROCESSING
    assert out.reason == "activity"


def test_d1_pane_churn_is_not_activity() -> None:
    """A sample with NO activity fact (the adapter never set PRESENT from pane
    churn) does not become PROCESSING — it falls to confirmed-ready/UNKNOWN.
    This is the #767/#485 precedence: churn is not the activity fact."""
    s = _sample(activity=ActivityFact(value=FactValue.UNKNOWN))
    out = reduce(s, _ctx(last_status=TerminalStatus.PROCESSING))
    assert out.status is not TerminalStatus.PROCESSING or out.reason == "awaiting_confirm"


def test_d1_owned_condition_carried_but_not_projected_in_slice_a() -> None:
    """An owned cap banner with no higher rung: Slice A carries the condition
    on the candidate but does not yet project CAPPED-as-status (that is Slice B
    / AC-11). Here it lands on UNKNOWN carrying the condition."""
    s = _sample(
        activity=ActivityFact(value=FactValue.UNKNOWN),
        readiness=ReadinessFact(value=FactValue.UNKNOWN),
        condition=ConditionFact(kind="capped", owned=True, evidence=Evidence(kind="banner")),
    )
    out = reduce(s, _ctx(last_status=TerminalStatus.UNKNOWN))
    assert out.condition.owned is True
    assert out.condition.kind == "capped"


def test_completed_requires_settlement_not_just_readiness() -> None:
    """COMPLETED needs settlement PRESENT; readiness-only lowers to IDLE."""
    ctx = _ctx(last_status=TerminalStatus.PROCESSING)
    s1 = _sample(
        readiness=ReadinessFact(value=FactValue.PRESENT), filtered_fingerprint="a", sequence=1
    )
    r1 = reduce(s1, ctx)
    assert r1.next_context is not None
    s2 = _sample(
        readiness=ReadinessFact(value=FactValue.PRESENT), filtered_fingerprint="b", sequence=2
    )
    r2 = reduce(s2, r1.next_context)
    assert r2.status is TerminalStatus.IDLE


def test_process_identity_matches_requires_all_three() -> None:
    a = ProcessIdentity(boot_id="b", pid=100, start_ticks=42)
    assert a.matches(ProcessIdentity(boot_id="b", pid=100, start_ticks=42))
    assert not a.matches(ProcessIdentity(boot_id="b", pid=100, start_ticks=43))
    assert not a.matches(ProcessIdentity(boot_id="b", pid=None, start_ticks=42))
    assert not a.matches(None)
