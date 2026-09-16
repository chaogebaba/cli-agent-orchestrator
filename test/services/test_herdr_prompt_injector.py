"""``HerdrPromptInjector`` — WP-HERDR Seam B's carrier (H2-S3).

Everything here is driven through doubles for the herdr client and the two
legacy facts the injector resolves (the role probe and the pane map), because
what is under test is the injector's OWN decisions: the supervisor refusal, the
no-second-submission rule, and the projection of a transport result onto an
``InjectionResult``.  The transport mapping itself is tested against a real
socket in ``test/adapters/herdr/test_client.py`` and live on a box.
"""

from __future__ import annotations

from typing import Any

import pytest

from cli_agent_orchestrator.adapters.herdr.client import PromptSubmission
from cli_agent_orchestrator.core.delivery import AttemptOutcome
from cli_agent_orchestrator.services.queue_carrier import HerdrPromptInjector


class FakeClient:
    """Stands in for ``HerdrClient``: scripted submissions and state reads."""

    def __init__(
        self,
        submissions: list[PromptSubmission],
        states: list[PromptSubmission | None] | None = None,
    ) -> None:
        self._submissions = list(submissions)
        self._states = list(states or [])
        self.prompts: list[tuple[str, str]] = []
        self.state_reads: list[str] = []

    async def prompt_agent(self, *, target: str, text: str, wait_timeout_ms: int = 0) -> Any:
        self.prompts.append((target, text))
        return self._submissions.pop(0)

    async def agent_state(self, *, target: str) -> Any:
        self.state_reads.append(target)
        return self._states.pop(0) if self._states else None


def _submission(outcome: AttemptOutcome, detail: str, seq: int | None = None) -> PromptSubmission:
    return PromptSubmission(outcome=outcome, detail=detail, state_change_seq=seq)


def _state(seq: int | None, status: str = "idle") -> PromptSubmission:
    return PromptSubmission(
        outcome=AttemptOutcome.DELIVERED,
        detail="agent_state",
        agent_status=status,
        state_change_seq=seq,
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """An injector whose two legacy resolutions are answered, not performed."""
    import cli_agent_orchestrator.services.mailbox_service as mailbox
    import cli_agent_orchestrator.services.queue_carrier as qc

    monkeypatch.setattr(mailbox, "probe_supervisor_role", lambda _t: False)

    def make(client: FakeClient, *, is_supervisor: bool = False) -> HerdrPromptInjector:
        monkeypatch.setattr(mailbox, "probe_supervisor_role", lambda _t: is_supervisor)
        injector = HerdrPromptInjector()
        monkeypatch.setattr(
            HerdrPromptInjector, "_resolve", staticmethod(lambda _t: ("%3", "/tmp/sock"))
        )
        monkeypatch.setattr(qc, "_herdr_client", lambda _path: client)
        return injector

    return make


# ------------------------------------------------------------ the refusals


def test_a_supervisor_target_is_refused_and_never_submitted(wired) -> None:  # type: ignore[no-untyped-def]
    """K8's kill as a property of the call graph, re-asserted at entry.

    A herdr submission into a human's own pane would be WORSE than a paste: the
    runtime presses Enter.  A dispatch defect that routed a seat row here must
    therefore be as loud as it is at the paste seam, with the same outcome so the
    two carriers' journals agree.
    """
    client = FakeClient([_submission(AttemptOutcome.DELIVERED, "herdr:working", 2)])
    injector = wired(client, is_supervisor=True)
    result = injector.inject(terminal_id="t-seat", line="hello")
    assert result.outcome is AttemptOutcome.PASTE_ATTEMPTED
    assert result.detail == "paste_attempted"
    assert client.prompts == []


def test_an_unresolvable_pane_is_a_pane_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same word the paste seam uses for the same fact: nothing to write to."""
    import cli_agent_orchestrator.services.mailbox_service as mailbox

    monkeypatch.setattr(mailbox, "probe_supervisor_role", lambda _t: False)
    injector = HerdrPromptInjector()
    monkeypatch.setattr(
        HerdrPromptInjector,
        "_resolve",
        staticmethod(lambda _t: (_ for _ in ()).throw(ValueError("no pane"))),
    )
    result = injector.inject(terminal_id="t-1", line="hello")
    assert result.outcome is AttemptOutcome.PANE_ABSENT
    assert result.detail.startswith("herdr_no_target:")


# --------------------------------------------------- the happy projection


def test_a_delivered_submission_is_a_delivered_injection(wired) -> None:  # type: ignore[no-untyped-def]
    client = FakeClient([_submission(AttemptOutcome.DELIVERED, "herdr:working", 5)])
    injector = wired(client)
    result = injector.inject(terminal_id="t-1", line="digest")
    assert result.outcome is AttemptOutcome.DELIVERED
    assert result.detail == "herdr:herdr:working"
    assert client.prompts == [("%3", "digest")]


# -------------------------------------------- the no-second-submission rule


def test_an_uncertain_submission_blocks_the_next_one(wired) -> None:  # type: ignore[no-untyped-def]
    """Blueprint §6, and the half ``NON_DELIVERY_OUTCOMES`` does NOT carry.

    The queue retains the lease but nothing in the store stops a re-offer, so
    the rule lives where the evidence lives.  An unmoved ``state_change_seq``
    means the earlier text may still be sitting unconsumed in the composer, and
    a second submission would concatenate onto it.
    """
    client = FakeClient(
        submissions=[
            _submission(AttemptOutcome.SUBMISSION_UNCERTAIN, "submitted_while_working", 7)
        ],
        states=[_state(7)],
    )
    injector = wired(client)

    first = injector.inject(terminal_id="t-1", line="one")
    assert first.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN

    second = injector.inject(terminal_id="t-1", line="two")
    assert second.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
    assert second.detail == "herdr:unresolved_prior"
    # The point of the rule: the SECOND text never went to the runtime.
    assert client.prompts == [("%3", "one")]


def test_an_advanced_state_sequence_releases_the_block(wired) -> None:  # type: ignore[no-untyped-def]
    """The runtime observed the pane change, which resolves the question."""
    client = FakeClient(
        submissions=[
            _submission(AttemptOutcome.SUBMISSION_UNCERTAIN, "agent_prompt_stalled", 7),
            _submission(AttemptOutcome.DELIVERED, "herdr:working", 12),
        ],
        states=[_state(11)],
    )
    injector = wired(client)
    injector.inject(terminal_id="t-1", line="one")
    second = injector.inject(terminal_id="t-1", line="two")
    assert second.outcome is AttemptOutcome.DELIVERED
    assert client.prompts == [("%3", "one"), ("%3", "two")]


def test_an_unreadable_state_keeps_the_block(wired) -> None:  # type: ignore[no-untyped-def]
    """No evidence is not evidence of resolution, so it is not treated as one."""
    client = FakeClient(
        submissions=[_submission(AttemptOutcome.SUBMISSION_UNCERTAIN, "no_pre_state", 3)],
        states=[None],
    )
    injector = wired(client)
    injector.inject(terminal_id="t-1", line="one")
    second = injector.inject(terminal_id="t-1", line="two")
    assert second.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
    assert second.detail == "herdr:unresolved_unreadable"
    assert client.prompts == [("%3", "one")]


def test_a_resolved_submission_leaves_no_marker(wired) -> None:  # type: ignore[no-untyped-def]
    """A delivery, a veto and a pane-absent all CLEAR the block.

    Only an uncertain submission leaves text whose fate is unknown; every other
    outcome is a settled fact, and a marker that outlived one would wedge the
    terminal's deliveries behind a question nobody was asking.
    """
    client = FakeClient(
        submissions=[
            _submission(AttemptOutcome.SUBMISSION_UNCERTAIN, "no_state_advance", 2),
            _submission(AttemptOutcome.DELIVERED, "herdr:working", 9),
            _submission(AttemptOutcome.VETO_DIALOG, "agent_blocked"),
            _submission(AttemptOutcome.DELIVERED, "herdr:working", 11),
        ],
        states=[_state(8)],
    )
    injector = wired(client)
    injector.inject(terminal_id="t-1", line="one")
    injector.inject(terminal_id="t-1", line="two")
    injector.inject(terminal_id="t-1", line="three")
    fourth = injector.inject(terminal_id="t-1", line="four")
    assert fourth.outcome is AttemptOutcome.DELIVERED
    # One state read only — the block was never re-armed after "two" delivered.
    assert client.state_reads == ["%3"]


def test_the_block_is_per_terminal(wired) -> None:  # type: ignore[no-untyped-def]
    """One wedged worker must not stop the fleet's other deliveries."""
    client = FakeClient(
        submissions=[
            _submission(AttemptOutcome.SUBMISSION_UNCERTAIN, "submitted_while_working", 4),
            _submission(AttemptOutcome.DELIVERED, "herdr:working", 1),
        ]
    )
    injector = wired(client)
    injector.inject(terminal_id="t-wedged", line="one")
    other = injector.inject(terminal_id="t-other", line="two")
    assert other.outcome is AttemptOutcome.DELIVERED
    assert client.prompts == [("%3", "one"), ("%3", "two")]


def test_a_submission_whose_sequence_was_never_learned_is_not_a_permanent_wedge(
    wired,  # type: ignore[no-untyped-def]
) -> None:
    """An injection that TIMED OUT records no sequence, and must still recover.

    With no baseline there is nothing to compare a later read against, so the
    first successful read BECOMES the baseline and the block holds one more
    round.  Without that adoption the terminal's deliveries would be wedged
    until the process restarted — a worse failure than the double-submission the
    rule exists to prevent, and one no lease or ``dead_by`` would clear.
    """
    client = FakeClient(
        submissions=[_submission(AttemptOutcome.DELIVERED, "herdr:working", 22)],
        states=[_state(20), _state(21)],
    )
    injector = wired(client)
    injector._mark_unresolved("t-1", None)  # what the TimeoutError arm records

    first = injector.inject(terminal_id="t-1", line="one")
    assert first.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
    assert first.detail == "herdr:unresolved_baseline"
    assert client.prompts == []

    second = injector.inject(terminal_id="t-1", line="two")
    assert second.outcome is AttemptOutcome.DELIVERED
    assert client.prompts == [("%3", "two")]
