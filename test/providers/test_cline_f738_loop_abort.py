"""F738 (#595) — cline self-abort ("aborted by another client").

Root cause (cline core 0.0.82): five consecutive byte-identical tool calls trip
cline's loop detector, which forces the consecutive-mistake counter to its limit
and self-aborts the run from inside the same process. The pane blames "another
client"; there is none, and no hub is involved.

Two arms:
  * Part 1 — the loop guard is injected by the PROVIDER into every cline
    worker's system prompt, so no profile can omit it.
  * Part 2 — the abort pane line classifies to a typed, re-dispatchable F611
    condition instead of being swallowed as a silent IDLE.

The Part 2 fixture is byte-exact from the live repro documented in
/data/cao-scratch/f738-build-report.md §4 Arm A.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.providers.cline_cli import (
    ABORT_LINE,
    LOOP_GUARD_CLAUSE,
    ClineCliProvider,
)
from cli_agent_orchestrator.providers.condition import (
    Condition,
    ConditionKind,
    Confidence,
    PolicyAction,
    classify_condition,
    policy_for_condition,
    should_deliver,
)

_FIX = Path(__file__).parent / "fixtures" / "conditions"


def _system_arg(provider: ClineCliProvider) -> str:
    """Return the -s value from the built base args."""
    parts = shlex.split(provider._build_base_args())
    assert "-s" in parts, "provider must always pass -s (it carries the loop guard)"
    return parts[parts.index("-s") + 1]


# ─── Part 1: the injected prompt carries the clause ───────────────────────────


class TestLoopGuardInjected:
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_clause_injected_without_profile(self, mock_load, mock_defaults) -> None:
        """A worker with NO agent profile still carries the loop guard.

        This is the whole point of injecting in the provider rather than in a
        profile: there is no configuration in which it can be missing.
        """
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass"}

        assert _system_arg(ClineCliProvider("t1234567", "sess", "win0")) == LOOP_GUARD_CLAUSE

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_clause_appended_after_profile_prompt(self, mock_load, mock_defaults) -> None:
        """A profile prompt is preserved verbatim; the guard is appended after."""
        mock_defaults.return_value = {"api_provider": "cline-pass"}
        profile = MagicMock()
        profile.system_prompt = "You are a grunt worker."
        profile.model = None
        profile.name = "grunt"
        mock_load.return_value = profile

        system_arg = _system_arg(
            ClineCliProvider("t1234567", "sess", "win0", agent_profile="grunt")
        )

        assert system_arg.startswith("You are a grunt worker.")
        assert system_arg.endswith(LOOP_GUARD_CLAUSE)

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_clause_survives_skill_prompt(self, mock_load, mock_defaults) -> None:
        """A skill prompt does not displace the guard."""
        mock_defaults.return_value = {"api_provider": "cline-pass"}
        profile = MagicMock()
        profile.system_prompt = "Base."
        profile.model = None
        profile.name = "agent"
        mock_load.return_value = profile

        system_arg = _system_arg(
            ClineCliProvider(
                "t1234567",
                "sess",
                "win0",
                agent_profile="agent",
                skill_prompt="Skill instructions.",
            )
        )

        assert "Base." in system_arg
        assert "Skill instructions." in system_arg
        assert LOOP_GUARD_CLAUSE in system_arg

    def test_clause_states_the_operative_facts(self) -> None:
        """The clause must name the count and the consequence, or it teaches
        nothing actionable. Guards the wording against a well-meaning trim."""
        assert "five consecutive byte-identical tool calls" in LOOP_GUARD_CLAUSE
        assert "never issue the same shell command twice in a row" in LOOP_GUARD_CLAUSE


# ─── Part 2: the abort line is a typed, re-dispatchable condition ─────────────


class TestSelfAbortCondition:
    def _classify(self) -> Condition:
        pane = (_FIX / "cline-cli-self-abort-1.txt").read_text(encoding="utf-8")
        assert ABORT_LINE in pane, "fixture must contain the verbatim abort line"
        cond = classify_condition(pane, "cline_cli")
        assert cond is not None, "the abort pane must classify to a condition"
        return cond

    def test_abort_line_classifies_to_typed_condition(self) -> None:
        cond = self._classify()
        assert cond.kind is ConditionKind.TRANSIENT_OVERLOAD
        assert cond.subtype == "self_abort_loop_limit"
        assert cond.provider == "cline_cli"
        assert cond.confidence is Confidence.HIGH
        assert cond.evidence == ABORT_LINE

    def test_abort_outranks_the_busy_churn_above_it(self) -> None:
        """The same pane still shows `[run_commands]` rows, which match the
        cline BUSY anchor. The abort is what happened; BUSY (precedence 7) must
        not mask it (precedence 6)."""
        cond = self._classify()
        assert cond.kind is not ConditionKind.BUSY

    def test_condition_is_re_dispatchable_not_a_lane_event(self) -> None:
        """It must surface (so the run is not silently lost) but carry NO policy
        action: a self-abort is recovered by re-dispatching the same message,
        never by rebinding a lane or stopping to ask."""
        cond = self._classify()
        assert should_deliver(cond) is True
        assert policy_for_condition(cond, position="dev") is PolicyAction.NONE
        assert cond.reset_hint is not None
        assert "re-dispatch" in cond.reset_hint

    def test_other_providers_do_not_match_the_cline_anchor(self) -> None:
        """The anchor is cline-scoped; it must not leak into another provider's
        taxonomy."""
        pane = (_FIX / "cline-cli-self-abort-1.txt").read_text(encoding="utf-8")
        for provider in ("codex", "kiro_cli", "claude_code", "grok_cli"):
            cond = classify_condition(pane, provider)
            assert cond is None or cond.subtype != "self_abort_loop_limit"
