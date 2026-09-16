"""Per-terminal injector selection for WP-HERDR Seam B (H2-S3).

The 2026-09-16 amendment (3) is what this file tests.  §4 of the blueprint says
Seam B's injector is "selected per certified terminal in the one place adapters
are named", but ``WakeService`` holds ONE injector for ALL terminals, so the
selection cannot live where the blueprint pointed.  It lives in a dispatching
``PaneInjector`` built in the composition root, and these are the properties
that makes it safe to merge:

* switch OFF gives back the SAME object the tick got before H2, not a wrapper;
* an uncertified terminal reaches the paste with byte-identical results;
* a certified terminal reaches herdr;
* a supervisor receiver never reaches either, because the refusal is upstream
  in ``wake.py`` and re-asserted inside both injectors.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.core.delivery import AttemptOutcome, InjectionResult
from cli_agent_orchestrator.core.ports import PaneInjector


class RecordingInjector:
    """A ``PaneInjector`` that records what it was asked to inject."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, str]] = []

    def inject(self, *, terminal_id: str, line: str) -> InjectionResult:
        self.calls.append((terminal_id, line))
        return InjectionResult(outcome=AttemptOutcome.DELIVERED, detail=self.name)


# ------------------------------------------------------------- the switch off


def test_switch_off_returns_the_pane_injector_itself() -> None:
    """The rollback property as code, not as behaviour.

    Off is not "the dispatch chooses paste every time" — there is no dispatch.
    A reviewer can see that the object graph is what it was at ``ad4339e9``.
    """
    pane = RecordingInjector("pane")

    def factory() -> PaneInjector:  # pragma: no cover — must never run
        raise AssertionError("the herdr injector was built with the switch off")

    assert bootstrap._build_injector(pane, factory) is pane


def test_switch_off_never_consults_the_certification_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off costs one environment read and nothing else."""
    monkeypatch.delenv(bootstrap.HERDR_DELIVERY_ENV_VAR, raising=False)
    asked: list[str] = []

    built = bootstrap._build_injector(
        RecordingInjector("pane"),
        lambda: RecordingInjector("herdr"),
        predicate=lambda t: (asked.append(t), True)[1],
    )
    built.inject(terminal_id="t-1", line="hello")
    assert asked == []


# --------------------------------------------------------------- the dispatch


def _dispatch(
    monkeypatch: pytest.MonkeyPatch, certified: set[str]
) -> tuple[Any, RecordingInjector, list[RecordingInjector]]:
    monkeypatch.setenv(bootstrap.HERDR_DELIVERY_ENV_VAR, "1")
    pane = RecordingInjector("pane")
    built: list[RecordingInjector] = []

    def factory() -> PaneInjector:
        made = RecordingInjector("herdr")
        built.append(made)
        return made

    dispatch = bootstrap._build_injector(
        pane, factory, predicate=lambda terminal_id: terminal_id in certified
    )
    return dispatch, pane, built


def test_an_uncertified_terminal_reaches_the_paste(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parity: same injector, same arguments, same result."""
    dispatch, pane, built = _dispatch(monkeypatch, certified=set())
    result = dispatch.inject(terminal_id="t-plain", line="digest line")
    assert pane.calls == [("t-plain", "digest line")]
    assert result.outcome is AttemptOutcome.DELIVERED
    assert result.detail == "pane"
    # And nothing herdr-shaped was even constructed for it.
    assert built == []


def test_a_certified_terminal_reaches_herdr(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch, pane, built = _dispatch(monkeypatch, certified={"t-certified"})
    result = dispatch.inject(terminal_id="t-certified", line="digest line")
    assert pane.calls == []
    assert len(built) == 1
    assert built[0].calls == [("t-certified", "digest line")]
    assert result.detail == "herdr"


def test_one_process_holds_at_most_one_herdr_injector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unresolved-submission marker is per-injector state, so a second
    instance would be a second memory and the no-second-submission rule would
    stop holding across terminals."""
    dispatch, _pane, built = _dispatch(monkeypatch, certified={"a", "b"})
    dispatch.inject(terminal_id="a", line="x")
    dispatch.inject(terminal_id="b", line="y")
    dispatch.inject(terminal_id="a", line="z")
    assert len(built) == 1


def test_the_two_cohorts_are_decided_per_terminal_not_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mixed fleet is the WHOLE point: certification is a cell fact."""
    dispatch, pane, built = _dispatch(monkeypatch, certified={"t-certified"})
    dispatch.inject(terminal_id="t-certified", line="1")
    dispatch.inject(terminal_id="t-plain", line="2")
    assert pane.calls == [("t-plain", "2")]
    assert built[0].calls == [("t-certified", "1")]


# ------------------------------------------------------------ the predicate


def test_the_switch_is_structural_and_the_predicate_is_per_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r2: the switch is answered ONCE, by which object the tick holds.

    Review r1 §1/§10.6: r1 read ``os.environ`` again inside every ``inject()``,
    so the two readings could disagree — unsetting the variable in a running
    process silently moved every terminal back to paste, while setting it did
    nothing. The switch decides whether a dispatch EXISTS; the predicate only
    answers the per-terminal half, and is never reached when the switch is off.
    """
    from cli_agent_orchestrator.utils import herdr_runtime_gate

    herdr_runtime_gate.reset_gate()
    herdr_runtime_gate.bind_terminal("t-1", True)

    # Off: no dispatch at all, so the predicate is structurally unreachable.
    monkeypatch.delenv(bootstrap.HERDR_DELIVERY_ENV_VAR, raising=False)
    pane = RecordingInjector("pane")
    assert bootstrap._build_injector(pane, lambda: RecordingInjector("herdr")) is pane

    # On: the dispatch exists, and the predicate answers the certification half
    # alone — no second environment read.
    monkeypatch.setenv(bootstrap.HERDR_DELIVERY_ENV_VAR, "1")
    assert bootstrap._seam_b_selected("t-1") is True
    herdr_runtime_gate.reset_gate()
    assert bootstrap._seam_b_selected("t-1") is False


def test_the_predicate_does_not_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinned as an absence, because the cost r1 paid was a lookup per injection."""
    from cli_agent_orchestrator.utils import herdr_runtime_gate

    herdr_runtime_gate.reset_gate()
    herdr_runtime_gate.bind_terminal("t-1", True)
    monkeypatch.delenv(bootstrap.HERDR_DELIVERY_ENV_VAR, raising=False)
    # The variable is UNSET and the answer is still True: the predicate's job is
    # the certification row, not the switch.
    assert bootstrap._seam_b_selected("t-1") is True


def test_an_unbound_terminal_is_never_on_seam_b(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: unknown keeps the composer paste, the pre-H2 behaviour."""
    from cli_agent_orchestrator.utils import herdr_runtime_gate

    herdr_runtime_gate.reset_gate()
    monkeypatch.setenv(bootstrap.HERDR_DELIVERY_ENV_VAR, "1")
    assert bootstrap._seam_b_selected("never-seen") is False


def test_an_unreadable_predicate_falls_back_to_the_paste(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boot that cannot answer the question answers it the safe way."""
    import cli_agent_orchestrator.utils.herdr_runtime_gate as gate

    monkeypatch.setenv(bootstrap.HERDR_DELIVERY_ENV_VAR, "1")
    monkeypatch.setattr(
        gate, "terminal_certified", lambda _t: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert bootstrap._seam_b_selected("t-1") is False


# ------------------------------------------------------------- the port shape


def test_the_pane_injector_port_is_not_widened() -> None:
    """H2 adds an implementation, never a parameter (wp-acp-plane §10).

    A widened port would be a change to what ``app`` knows about terminals, and
    every implementation and every caller would have to move together.  Pinned
    as a static check so a later lane cannot add "just one" keyword.
    """
    sig = inspect.signature(PaneInjector.inject)
    assert list(sig.parameters) == ["self", "terminal_id", "line"]
    for name in ("terminal_id", "line"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters[name].annotation == "str"
    assert sig.return_annotation == "InjectionResult"


def test_the_dispatch_and_both_carriers_satisfy_the_same_port() -> None:
    from cli_agent_orchestrator.services.queue_carrier import (
        HerdrPromptInjector,
        PaneWorkerInjector,
    )

    for impl in (PaneWorkerInjector(), HerdrPromptInjector()):
        assert isinstance(impl, PaneInjector)
        sig = inspect.signature(impl.inject)
        assert list(sig.parameters) == ["terminal_id", "line"]

    dispatch = bootstrap._CertifiedInjectorDispatch(
        RecordingInjector("pane"), lambda: RecordingInjector("herdr"), lambda _t: False
    )
    assert isinstance(dispatch, PaneInjector)
