"""F642 delivery-ledger spine — PURE decision-logic tests (no DB).

Covers the import-cheap logic in ``clients.delivery_ledger``:

* D7/AC21 — the durable de-dup rule (``should_suppress_condition``): the four
  sequences (a)/(b)/(c)/(d) AND both mutant arms, asserted directly against the
  rule so the mutant is EXECUTABLE, not narrated.
* D5/AC6 — the kind→surfaces map is data; an unmapped kind defaults to
  ``inbox=False`` and is reported as unmapped.
* D2/S2/AC19/AC23 — carrier exhaustion over the stored applicable set, including
  the ``carrier_unavailable`` (disarm) arm and its never-satisfiable mutant.
"""

from cli_agent_orchestrator.clients.delivery_ledger import (
    Carrier,
    ConditionDecision,
    ConditionLogRow,
    ConditionTuple,
    EmissionOutcome,
    EmissionView,
    busy_class_declines_inbox,
    carriers_exhausted,
    is_kind_mapped,
    latest_memory_row,
    should_suppress_condition,
    surfaces_for_kind,
)

# ── helpers ─────────────────────────────────────────────────────────────────
_next_id = [0]


def _row(decision: ConditionDecision, tup):
    _next_id[0] += 1
    return ConditionLogRow(_next_id[0], decision, tup)


def _capped(epoch=1):
    return ConditionTuple("CAPPED", "usage_cap", epoch)


def _dialog(epoch=1):
    return ConditionTuple("DIALOG_BLOCKED", "wait", epoch)


# ── D7 / AC21: the durable de-dup rule ───────────────────────────────────────
def test_ac21a_capped_then_capped_suppresses_second():
    """(a) CAPPED → CAPPED: latest memory row is the first delivered CAPPED →
    SUPPRESS the second (matches F611 prev == key)."""
    rows = [_row(ConditionDecision.DELIVERED, _capped())]
    assert should_suppress_condition(_capped(), rows) is True


def test_ac21b_capped_dialog_capped_delivers_all_three():
    """(b) CAPPED → DIALOG_BLOCKED → CAPPED: the latest memory row when the second
    CAPPED arrives is the DIALOG delivered row → tuple differs → DELIVER."""
    rows = [
        _row(ConditionDecision.DELIVERED, _capped()),
        _row(ConditionDecision.DELIVERED, _dialog()),
    ]
    assert should_suppress_condition(_capped(), rows) is False


def test_ac21c_capped_clear_capped_delivers_second():
    """(c) CAPPED → clear → CAPPED: the clear wrote a `cleared` row (NULL tuple);
    the latest memory row is that cleared row (not a delivered) → DELIVER. The
    NULL tuple is never compared."""
    rows = [
        _row(ConditionDecision.DELIVERED, _capped()),
        _row(ConditionDecision.CLEARED, None),
    ]
    assert should_suppress_condition(_capped(), rows) is False


def test_ac21d_capped_gated_capped_suppresses_second():
    """(d) CAPPED → gated(LOW) → CAPPED: the `gated` row is SKIPPED; the latest
    memory-updating row is still the first delivered CAPPED → SUPPRESS. This is
    the common shell-baseline interleave (PROC_EXITED/LOW wins selection)."""
    rows = [
        _row(ConditionDecision.DELIVERED, _capped()),
        _row(ConditionDecision.GATED, ConditionTuple("PROC_EXITED", "shell_baseline_return", 1)),
    ]
    assert should_suppress_condition(_capped(), rows) is True


def test_ac21d_deduped_row_also_skipped():
    """A `deduped` audit row is likewise skipped by the comparison."""
    rows = [
        _row(ConditionDecision.DELIVERED, _capped()),
        _row(ConditionDecision.DEDUPED, _capped()),
    ]
    assert should_suppress_condition(_capped(), rows) is True


def test_ac21_mutant_once_per_epoch_pk_would_drop_b_and_c():
    """MUTANT (r2's once-per-epoch PK): membership over ALL delivered tuples.
    Under that rule (b) and (c) would be SUPPRESSED — dropping decision-carrying
    originals. We assert the CORRECT rule does NOT suppress them, and that the
    mutant rule (simulated) WOULD, so the arms disagree."""
    # correct rule: (b) delivers
    rows_b = [
        _row(ConditionDecision.DELIVERED, _capped()),
        _row(ConditionDecision.DELIVERED, _dialog()),
    ]
    assert should_suppress_condition(_capped(), rows_b) is False

    def mutant_once_per_epoch(incoming, rows):
        # membership across every delivered tuple ever (r2's PK)
        seen = {r.tuple_ for r in rows if r.decision is ConditionDecision.DELIVERED}
        return incoming in seen

    assert mutant_once_per_epoch(_capped(), rows_b) is True  # mutant DROPS the original


def test_ac21_mutant_unqualified_latest_row_would_redeliver_d():
    """MUTANT (r3's unqualified rule): read the literal latest row (any decision).
    Under that rule (d) would RE-DELIVER because the latest row is the `gated`
    one, whose tuple differs. The correct rule suppresses; the mutant does not."""
    rows_d = [
        _row(ConditionDecision.DELIVERED, _capped()),
        _row(ConditionDecision.GATED, ConditionTuple("PROC_EXITED", "shell_baseline_return", 1)),
    ]
    assert should_suppress_condition(_capped(), rows_d) is True  # correct

    def mutant_unqualified(incoming, rows):
        latest = max(rows, key=lambda r: r.id)
        return latest.decision is ConditionDecision.DELIVERED and latest.tuple_ == incoming

    assert mutant_unqualified(_capped(), rows_d) is False  # mutant RE-DELIVERS


def test_latest_memory_row_ignores_audit_rows():
    rows = [
        _row(ConditionDecision.DELIVERED, _capped()),
        _row(ConditionDecision.GATED, _dialog()),
        _row(ConditionDecision.DEDUPED, _capped()),
    ]
    latest = latest_memory_row(rows)
    assert latest is not None and latest.decision is ConditionDecision.DELIVERED


def test_empty_log_never_suppresses():
    assert should_suppress_condition(_capped(), []) is False


def test_busy_class_stays_inside_comparison():
    """A busy_class delivery is a `delivered` row, so it participates in de-dup:
    BUSY → BUSY suppresses the second."""
    busy = ConditionTuple("BUSY", "working", 1)
    rows = [_row(ConditionDecision.DELIVERED, busy)]
    assert should_suppress_condition(busy, rows) is True


# ── D5 / AC6: the kind→surfaces map is data ──────────────────────────────────
def test_ac6_unmapped_kind_defaults_to_no_inbox_and_reports_unmapped():
    assert is_kind_mapped("SOME_NEW_KIND") is False
    surf = surfaces_for_kind("SOME_NEW_KIND")
    assert surf.inbox is False  # never a silent push
    assert surf.fleet is True and surf.bus is True


def test_ac5_busy_declines_inbox_capped_does_not():
    assert busy_class_declines_inbox("BUSY") is True
    assert busy_class_declines_inbox("CAPPED") is False
    assert is_kind_mapped("CAPPED") and surfaces_for_kind("CAPPED").inbox is True


def test_map_matches_blueprint_section_4():
    assert surfaces_for_kind("NET_INTERRUPTED").inbox is False
    assert surfaces_for_kind("TRANSIENT_OVERLOAD").inbox is False
    assert surfaces_for_kind("AUTH_EXPIRED").inbox is True
    assert surfaces_for_kind("DIALOG_BLOCKED").inbox is True
    assert surfaces_for_kind("PROC_EXITED").inbox is True
    assert surfaces_for_kind("CONTEXT_EXHAUSTED").inbox is True


# ── D2 / S2 / AC19 / AC23: carrier exhaustion ────────────────────────────────
def test_ac19_exhaustion_when_every_applicable_carrier_failed():
    applicable = [Carrier.NATIVE, Carrier.DOORBELL]
    emissions = [
        EmissionView(Carrier.NATIVE, EmissionOutcome.FAILED, retryable=False),
        EmissionView(Carrier.DOORBELL, EmissionOutcome.FAILED, retryable=False),
    ]
    assert carriers_exhausted(applicable, emissions, acked=False) is True


def test_ac19_not_exhausted_while_a_carrier_is_retryable():
    applicable = [Carrier.NATIVE, Carrier.DOORBELL]
    emissions = [
        EmissionView(Carrier.NATIVE, EmissionOutcome.FAILED, retryable=True),
        EmissionView(Carrier.DOORBELL, EmissionOutcome.FAILED, retryable=False),
    ]
    assert carriers_exhausted(applicable, emissions, acked=False) is False


def test_not_exhausted_while_a_carrier_is_still_owed_an_attempt():
    applicable = [Carrier.NATIVE, Carrier.DOORBELL]
    emissions = [EmissionView(Carrier.NATIVE, EmissionOutcome.FAILED, retryable=False)]
    # DOORBELL has no row yet — still outstanding.
    assert carriers_exhausted(applicable, emissions, acked=False) is False


def test_acked_id_is_never_undeliverable():
    applicable = [Carrier.NATIVE]
    emissions = [EmissionView(Carrier.NATIVE, EmissionOutcome.FAILED, retryable=False)]
    assert carriers_exhausted(applicable, emissions, acked=True) is False


def test_ac23_absent_doorbell_not_counted_but_native_failure_exhausts():
    """AC23: on a seat with no armed WS, applicable_carriers excludes doorbell;
    exhaustion is reached when the carriers actually in the set have failed."""
    applicable = [Carrier.NATIVE]  # doorbell absent from stored set
    emissions = [EmissionView(Carrier.NATIVE, EmissionOutcome.FAILED, retryable=False)]
    assert carriers_exhausted(applicable, emissions, acked=False) is True


def test_ac23_disarm_arm_carrier_unavailable_completes_exhaustion():
    """AC23 second arm: a carrier in the stored set that DISARMS before its turn
    gets outcome=carrier_unavailable, and exhaustion still completes — the id
    reaches undeliverable rather than waiting forever."""
    applicable = [Carrier.NATIVE, Carrier.DOORBELL]
    emissions = [
        EmissionView(Carrier.NATIVE, EmissionOutcome.FAILED, retryable=False),
        EmissionView(Carrier.DOORBELL, EmissionOutcome.CARRIER_UNAVAILABLE, retryable=False),
    ]
    assert carriers_exhausted(applicable, emissions, acked=False) is True


def test_ac23_mutant_never_records_outcome_is_never_satisfiable():
    """MUTANT (omit the emit-time re-check): the disarmed carrier never records an
    outcome, so it stays outstanding and the predicate is never satisfiable — the
    id would be silently never delivered (failure mode three)."""
    applicable = [Carrier.NATIVE, Carrier.DOORBELL]
    emissions_mutant = [
        EmissionView(Carrier.NATIVE, EmissionOutcome.FAILED, retryable=False),
        # DOORBELL disarmed but NO outcome recorded (mutant): still outstanding.
    ]
    assert carriers_exhausted(applicable, emissions_mutant, acked=False) is False
