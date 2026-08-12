"""Tests for ``cao config preflight --activation`` CLI command (F137).

Covers: AC5 (install ordering — indirect), AC7 (stable CLI contract),
AC9 (F131 defense retained — by not touching it).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services.systemd_tmux_preflight import (
    ActivationPreflightCheck,
    ActivationPreflightResult,
)


@pytest.fixture
def runner():
    return CliRunner()


def _pass_result() -> ActivationPreflightResult:
    return ActivationPreflightResult(
        ok=True,
        mode="aggregate_only",
        version="259.8",
        version_policy="known_unsafe",
        enabled_prefix_dropins=(),
        checks=(
            ActivationPreflightCheck(code="version_parse", ok=True, observed="259.8"),
            ActivationPreflightCheck(code="prefix_dropin_scan", ok=True, observed=()),
            ActivationPreflightCheck(
                code="aggregate_memory_high", ok=True, observed=10737418240, expected=10737418240
            ),
            ActivationPreflightCheck(
                code="aggregate_memory_max", ok=True, observed=12884901888, expected=12884901888
            ),
            ActivationPreflightCheck(
                code="aggregate_memory_swap_max",
                ok=True,
                observed=6442450944,
                expected=6442450944,
            ),
            ActivationPreflightCheck(
                code="aggregate_managed_oom_swap", ok=True, observed="auto", expected="auto"
            ),
            ActivationPreflightCheck(
                code="jobs_diagnostic", ok=True, observed=False, detail="no pending jobs"
            ),
        ),
    )


def _fail_result() -> ActivationPreflightResult:
    return ActivationPreflightResult(
        ok=False,
        mode="aggregate_only",
        version="259.8",
        version_policy="known_unsafe",
        enabled_prefix_dropins=("/home/user/.config/systemd/user/tmux-spawn-.scope.d/50-oomguard.conf",),
        checks=(
            ActivationPreflightCheck(code="version_parse", ok=True, observed="259.8"),
            ActivationPreflightCheck(
                code="prefix_dropin_scan",
                ok=False,
                observed=("/home/user/.config/systemd/user/tmux-spawn-.scope.d/50-oomguard.conf",),
                expected=(),
                detail="active prefix drop-ins block activation",
            ),
        ),
    )


class TestPreflightHumanOutput:
    """Human-readable output starts with PASS/FAIL."""

    def test_pass_output_starts_with_pass(self, runner):
        with patch(
            "cli_agent_orchestrator.cli.commands.config.run_activation_preflight",
            return_value=_pass_result(),
        ):
            result = runner.invoke(cli, ["config", "preflight", "--activation"])
        assert result.exit_code == 0
        assert result.output.startswith("PASS")

    def test_fail_output_starts_with_fail(self, runner):
        with patch(
            "cli_agent_orchestrator.cli.commands.config.run_activation_preflight",
            return_value=_fail_result(),
        ):
            result = runner.invoke(cli, ["config", "preflight", "--activation"])
        assert result.exit_code == 1
        assert result.output.startswith("FAIL")

    def test_fail_names_failed_invariant(self, runner):
        with patch(
            "cli_agent_orchestrator.cli.commands.config.run_activation_preflight",
            return_value=_fail_result(),
        ):
            result = runner.invoke(cli, ["config", "preflight", "--activation"])
        assert "prefix_dropin_scan" in result.output


class TestPreflightJsonOutput:
    """JSON output has stable keys and parseable structure."""

    def test_json_pass_exit_zero(self, runner):
        with patch(
            "cli_agent_orchestrator.cli.commands.config.run_activation_preflight",
            return_value=_pass_result(),
        ):
            result = runner.invoke(cli, ["config", "preflight", "--activation", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True

    def test_json_fail_exit_nonzero(self, runner):
        with patch(
            "cli_agent_orchestrator.cli.commands.config.run_activation_preflight",
            return_value=_fail_result(),
        ):
            result = runner.invoke(cli, ["config", "preflight", "--activation", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["ok"] is False

    def test_json_has_stable_keys(self, runner):
        with patch(
            "cli_agent_orchestrator.cli.commands.config.run_activation_preflight",
            return_value=_pass_result(),
        ):
            result = runner.invoke(cli, ["config", "preflight", "--activation", "--json"])
        data = json.loads(result.output)
        assert "ok" in data
        assert "mode" in data
        assert "version" in data
        assert "version_policy" in data
        assert "enabled_prefix_dropins" in data
        assert "checks" in data
        assert data["mode"] == "aggregate_only"

    def test_json_checks_have_required_fields(self, runner):
        with patch(
            "cli_agent_orchestrator.cli.commands.config.run_activation_preflight",
            return_value=_pass_result(),
        ):
            result = runner.invoke(cli, ["config", "preflight", "--activation", "--json"])
        data = json.loads(result.output)
        for check in data["checks"]:
            assert "code" in check
            assert "ok" in check

    def test_json_human_agree_on_pass_fail(self, runner):
        """AC7: human and JSON modes agree on pass/fail."""
        for factory in (_pass_result, _fail_result):
            mock_result = factory()
            with patch(
                "cli_agent_orchestrator.cli.commands.config.run_activation_preflight",
                return_value=mock_result,
            ):
                human = runner.invoke(cli, ["config", "preflight", "--activation"])
            with patch(
                "cli_agent_orchestrator.cli.commands.config.run_activation_preflight",
                return_value=mock_result,
            ):
                json_out = runner.invoke(cli, ["config", "preflight", "--activation", "--json"])
            data = json.loads(json_out.output)
            if mock_result.ok:
                assert human.exit_code == 0
                assert json_out.exit_code == 0
                assert data["ok"] is True
            else:
                assert human.exit_code == 1
                assert json_out.exit_code == 1
                assert data["ok"] is False


class TestPreflightNoMutation:
    """The CLI command performs no reconciliation or mutation."""

    def test_activation_flag_required(self, runner):
        """Without --activation, the command errors."""
        result = runner.invoke(cli, ["config", "preflight"])
        assert result.exit_code != 0
