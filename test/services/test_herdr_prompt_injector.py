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
    assert result.detail.startswith("herdr:no_target:")


# --------------------------------------------------- the happy projection


def test_a_delivered_submission_is_a_delivered_injection(wired) -> None:  # type: ignore[no-untyped-def]
    client = FakeClient([_submission(AttemptOutcome.DELIVERED, "herdr:working", 5)])
    injector = wired(client)
    result = injector.inject(terminal_id="t-1", line="digest")
    assert result.outcome is AttemptOutcome.DELIVERED
    # r1 wrote ``herdr:herdr:working`` — the client prefixes, and the injector
    # prefixed again (review r1 §5).  One prefix, applied in the client only.
    assert result.detail == "herdr:working"
    assert client.prompts == [("%3", "digest")]


# ------------------------------------------- the injector holds NO state now


def test_the_injector_keeps_nothing_between_injections(wired) -> None:  # type: ignore[no-untyped-def]
    """r2: the no-second-submission rule moved to the store, per ID.

    Review r1 §2 showed why it could not live here: this port is handed
    ``(terminal_id, line)`` and never learns a ``msg_id``, the digest it submits
    covers many ids at once, and r1's per-terminal marker cleared itself on the
    very evidence that the first copy had LANDED.  Blueprint amendment (7) rules
    the quarantine is per id and must hold through any path, so it belongs to
    ``reclaim``.

    Asserted as an absence, because an absence is what closes both wedges of
    review r1 §4: no marker, no lock, no clearing condition, nothing to persist
    and nothing a restart forgets.
    """
    injector = wired(FakeClient([]))
    assert not [a for a in vars(injector) if not a.startswith("__")]
    for name in ("_unresolved", "_blocked_by_unresolved", "_mark_unresolved", "_clear_unresolved"):
        assert not hasattr(injector, name), f"{name} survived the r2 correction"


def test_repeated_injections_each_submit_exactly_once(wired) -> None:  # type: ignore[no-untyped-def]
    """The injector never suppresses a call; suppression is the store's job.

    Stated so the division of labour is testable from this side too: whatever
    the previous outcome was, an injection that reaches this class submits, and
    the reason a duplicate cannot happen is that ``reclaim`` never offers the
    row again.
    """
    client = FakeClient(
        [
            _submission(AttemptOutcome.SUBMISSION_UNCERTAIN, "herdr:agent_prompt_stalled"),
            _submission(AttemptOutcome.DELIVERED, "herdr:working", 9),
        ]
    )
    injector = wired(client)
    first = injector.inject(terminal_id="t-1", line="one")
    second = injector.inject(terminal_id="t-1", line="two")
    assert first.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
    assert second.outcome is AttemptOutcome.DELIVERED
    assert client.prompts == [("%3", "one"), ("%3", "two")]
    assert client.state_reads == []


def test_an_injection_timeout_is_uncertain_and_leaves_no_marker(
    monkeypatch: pytest.MonkeyPatch, wired  # type: ignore[no-untyped-def]
) -> None:
    """A submission that outlived its bound may have landed, so it is uncertain.

    r1 additionally recorded a per-terminal marker here, with no sequence, and
    that recording is what wedged the terminal (review r1 §4 and its sibling).
    The outcome is unchanged; the bookkeeping is gone, and the store's per-id
    quarantine covers the row.
    """
    import cli_agent_orchestrator.services.queue_carrier as qc

    injector = wired(FakeClient([]))

    def boom(coro: object, timeout_s: float) -> object:
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[attr-defined]
        raise TimeoutError("did not finish")

    monkeypatch.setattr(qc, "_run_blocking", boom)
    result = injector.inject(terminal_id="t-1", line="one")
    assert result.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
    assert result.detail == "herdr:inject_timeout"
    assert not [a for a in vars(injector) if not a.startswith("__")]
