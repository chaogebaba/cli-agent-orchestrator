"""F168 — what is left of the idle-supervisor doorbell suite (WP-ARCH 3c).

This file WAS the acceptance suite for ``services/doorbell_service`` — forty-odd
arms over ``ring_supervisor_doorbell``: one nudge per run, the cursor dedup, the
gate-refusal reasons, the ordering and exception isolation between call sites,
the fixed nudge text, the rebind concurrency, the rate-limited log, the
config-flag matrix, the call-site inventory, the real-sqlite reconciler
integration and the G4 tmux fallback.

K3 deletes both doorbell modules and K8 deletes the paste they fell back to, so
every one of those arms lost its subject at once. Rewriting them as absence
checks would replace a suite that proved a nudge CORRECT with one that proves a
module missing — and ``test_3c_slice3_surfaces_gone.py`` already does that
properly, against the whole source tree and paired with live controls. The
behavioural replacement is ``test/app/delivery/``: the seat's wake is the
delivery tick's single native carrier now, and what used to be "did the doorbell
ring, and did it refuse politely" is asserted there as an emission and a typed
refusal on a durable row.

ONE group survives, and it is why the file is not deleted outright. It has
nothing to do with the doorbell: ``CallbackRunOutcome`` is the F136 runner's
return type in ``services/inbox_service``, the runner is kept, and 3c CHANGED
the dataclass (it removes the two ``_f459_*`` payload fields). A shape test over
a type the slice just edited is worth more than it was before, not less.
"""

from __future__ import annotations

from cli_agent_orchestrator.services.inbox_service import CallbackRunOutcome

# ===========================================================================
# V1 — max_written_row_id field on CallbackRunOutcome
# ===========================================================================


class TestMaxWrittenRowIdField:
    """CallbackRunOutcome carries max_written_row_id (keyword-defaulted)."""

    def test_default_zero(self):
        o = CallbackRunOutcome()
        assert o.max_written_row_id == 0

    def test_keyword_construction(self):
        o = CallbackRunOutcome(written=3, max_written_row_id=42)
        assert o.max_written_row_id == 42

    def test_positional_backward_compat(self):
        """Existing code constructing CallbackRunOutcome positionally still works."""
        # The new field is keyword-only at the end, so positional construction
        # of the existing fields should not break.
        o = CallbackRunOutcome(
            selected=10,
            processed=8,
            cursor_before=0,
            cursor_after=5,
            replay_selected=2,
            replay_drained=1,
            written=6,
            already_present=2,
            retryable_failure_count=0,
            identity_conflict_count=0,
            bootstrap_mode=None,
            needs_immediate_wake=False,
            retry_delay_s=None,
            reason="ok",
        )
        assert o.max_written_row_id == 0  # default
