"""Tests for F137 systemd tmux-scope activation preflight service.

Covers: AC1 (prefix prohibition), AC2 (version behavior), AC3 (aggregate readback),
AC4 (non-mutation proof), AC6 (bounded failure), AC8 (no per-scope mutation),
AC9 (F131 defense — indirect), AC11 (regression/mutation ledger).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.systemd_tmux_preflight import (
    ActivationPreflightCheck,
    ActivationPreflightResult,
    DefaultCommandRunner,
    _DENIED_COMMANDS,
    _DENIED_SYSTEMCTL_VERBS,
    _EXPECTED_MANAGED_OOM_SWAP,
    _EXPECTED_MEMORY_HIGH,
    _EXPECTED_MEMORY_MAX,
    _EXPECTED_MEMORY_SWAP_MAX,
    _enforce_allowlist,
    _normalize_bytes,
    _parse_property_value,
    _parse_unit_paths,
    _parse_version,
    _scan_prefix_dropins,
    run_activation_preflight,
)


# ---------------------------------------------------------------------------
# Fake command runner for pure tests
# ---------------------------------------------------------------------------


class FakeRunner:
    """Controllable command runner for test injection."""

    def __init__(self):
        self.responses: dict[str, subprocess.CompletedProcess[str]] = {}
        self.calls: list[list[str]] = []
        self.timeouts: set[str] = set()  # commands that should timeout
        self.errors: dict[str, Exception] = {}  # commands that should raise

    def set_response(self, key: str, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.responses[key] = subprocess.CompletedProcess(
            args=key, returncode=returncode, stdout=stdout, stderr=stderr
        )

    def set_timeout(self, key: str):
        self.timeouts.add(key)

    def set_error(self, key: str, exc: Exception):
        self.errors[key] = exc

    def _key(self, args: Sequence[str]) -> str:
        return " ".join(args)

    def run(self, args: Sequence[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        key = self._key(args)
        if key in self.timeouts:
            raise subprocess.TimeoutExpired(cmd=key, timeout=timeout)
        if key in self.errors:
            raise self.errors[key]
        if key in self.responses:
            return self.responses[key]
        # Default: empty success
        return subprocess.CompletedProcess(args=key, returncode=0, stdout="", stderr="")


def _healthy_runner(tmp_path: Path | None = None) -> FakeRunner:
    """Return a runner pre-configured for a healthy system."""
    r = FakeRunner()
    r.set_response("systemctl --version", stdout="systemd 256 (256.11-1.fc42)\n+PAM +AUDIT")
    r.set_response(
        "systemd-analyze --user unit-paths",
        stdout="/home/user/.config/systemd/user\n/etc/systemd/user\n/usr/lib/systemd/user\n",
    )
    r.set_response(
        "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
        stdout=(
            f"MemoryHigh={_EXPECTED_MEMORY_HIGH}\n"
            f"MemoryMax={_EXPECTED_MEMORY_MAX}\n"
            f"MemorySwapMax={_EXPECTED_MEMORY_SWAP_MAX}\n"
            f"ManagedOOMSwap=auto\n"
        ),
    )
    r.set_response(
        "systemctl --user list-jobs --no-pager",
        stdout="No jobs pending.\n",
    )
    return r


# ---------------------------------------------------------------------------
# Unit tests: version parsing (AC2)
# ---------------------------------------------------------------------------


class TestVersionParse:
    def test_259_8_reports_known_unsafe(self):
        ver, policy, ok = _parse_version("systemd 259 (259.8-1.fc44)\n+PAM")
        assert ver == "259"
        assert policy == "known_unsafe"
        assert ok is True

    def test_259_dot_variant(self):
        ver, policy, ok = _parse_version("systemd 259.8 (259.8-1.fc44)\n+PAM")
        assert ver == "259.8"
        assert policy == "known_unsafe"
        assert ok is True

    def test_256_reports_unqualified(self):
        ver, policy, ok = _parse_version("systemd 256 (256.11-1.fc42)\n+PAM")
        assert ver == "256"
        assert policy == "unqualified"
        assert ok is True

    def test_260_still_unqualified_not_enabler(self):
        """Version >= 260 does NOT enable per-scope mutation."""
        ver, policy, ok = _parse_version("systemd 260 (260.1-1.fc45)\n+PAM")
        assert ver == "260"
        assert policy == "unqualified"
        assert ok is True

    def test_malformed_output_fails(self):
        ver, policy, ok = _parse_version("garbage output\n")
        assert ver is None
        assert policy == "unqualified"
        assert ok is False

    def test_empty_output_fails(self):
        ver, policy, ok = _parse_version("")
        assert ver is None
        assert policy == "unqualified"
        assert ok is False


# ---------------------------------------------------------------------------
# Unit tests: unit path parsing
# ---------------------------------------------------------------------------


class TestUnitPaths:
    def test_normal_paths(self):
        paths = _parse_unit_paths("/a\n/b\n/c\n")
        assert paths == ["/a", "/b", "/c"]

    def test_empty_returns_none(self):
        assert _parse_unit_paths("") is None
        assert _parse_unit_paths("\n  \n") is None

    def test_strips_whitespace(self):
        paths = _parse_unit_paths("  /a  \n  /b  \n")
        assert paths == ["/a", "/b"]


# ---------------------------------------------------------------------------
# Unit tests: prefix drop-in scan (AC1)
# ---------------------------------------------------------------------------


class TestPrefixDropinScan:
    def test_no_dropin_dir_is_clean(self, tmp_path):
        assert _scan_prefix_dropins([str(tmp_path)]) == ()

    def test_active_conf_blocks(self, tmp_path):
        dropin = tmp_path / "tmux-spawn-.scope.d"
        dropin.mkdir()
        (dropin / "50-oomguard.conf").write_text("[Scope]\nMemoryMax=8G\n")
        result = _scan_prefix_dropins([str(tmp_path)])
        assert len(result) == 1
        assert result[0].endswith("50-oomguard.conf")

    def test_disabled_conf_ignored(self, tmp_path):
        dropin = tmp_path / "tmux-spawn-.scope.d"
        dropin.mkdir()
        (dropin / "50-oomguard.conf.disabled").write_text("[Scope]\nMemoryMax=8G\n")
        assert _scan_prefix_dropins([str(tmp_path)]) == ()

    def test_symlinked_conf_detected(self, tmp_path):
        dropin = tmp_path / "tmux-spawn-.scope.d"
        dropin.mkdir()
        target = tmp_path / "real.conf"
        target.write_text("[Scope]\nMemoryMax=8G\n")
        (dropin / "99-linked.conf").symlink_to(target)
        result = _scan_prefix_dropins([str(tmp_path)])
        assert len(result) == 1
        assert "99-linked.conf" in result[0]

    def test_multiple_roots_scanned(self, tmp_path):
        root1 = tmp_path / "r1"
        root2 = tmp_path / "r2"
        root1.mkdir()
        root2.mkdir()
        d1 = root1 / "tmux-spawn-.scope.d"
        d1.mkdir()
        (d1 / "a.conf").write_text("")
        d2 = root2 / "tmux-spawn-.scope.d"
        d2.mkdir()
        (d2 / "b.conf").write_text("")
        result = _scan_prefix_dropins([str(root1), str(root2)])
        assert len(result) == 2

    def test_results_sorted_deterministically(self, tmp_path):
        dropin = tmp_path / "tmux-spawn-.scope.d"
        dropin.mkdir()
        (dropin / "z.conf").write_text("")
        (dropin / "a.conf").write_text("")
        result = _scan_prefix_dropins([str(tmp_path)])
        assert result == tuple(sorted(result))

    def test_unreadable_dir_fails_closed(self, tmp_path):
        dropin = tmp_path / "tmux-spawn-.scope.d"
        dropin.mkdir()
        dropin.chmod(0o000)
        try:
            result = _scan_prefix_dropins([str(tmp_path)])
            assert len(result) == 1
            assert "<unreadable>" in result[0]
        finally:
            dropin.chmod(0o755)


# ---------------------------------------------------------------------------
# Unit tests: property parsing (AC3)
# ---------------------------------------------------------------------------


class TestPropertyParse:
    def test_normal_parse(self):
        output = "MemoryHigh=10737418240\nMemoryMax=12884901888\n"
        assert _parse_property_value(output, "MemoryHigh") == "10737418240"
        assert _parse_property_value(output, "MemoryMax") == "12884901888"

    def test_missing_property(self):
        assert _parse_property_value("MemoryHigh=100\n", "MemoryMax") is None

    def test_infinity_normalize(self):
        assert _normalize_bytes("infinity") is None
        assert _normalize_bytes("18446744073709551615") == 18446744073709551615

    def test_normal_normalize(self):
        assert _normalize_bytes("10737418240") == 10737418240


# ---------------------------------------------------------------------------
# Integration tests: full preflight (AC1-AC8)
# ---------------------------------------------------------------------------


class TestRunActivationPreflight:
    def test_healthy_system_passes(self, tmp_path):
        runner = _healthy_runner(tmp_path)
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.ok is True
        assert result.mode == "aggregate_only"
        assert result.version == "256"
        assert result.version_policy == "unqualified"
        assert result.enabled_prefix_dropins == ()
        for check in result.checks:
            assert check.ok is True, f"check {check.code} failed: {check.detail}"

    def test_259_version_still_passes_aggregate_only(self, tmp_path):
        """259.* is known_unsafe but aggregate_only still works."""
        runner = _healthy_runner(tmp_path)
        runner.set_response("systemctl --version", stdout="systemd 259 (259.8-1.fc44)\n+PAM")
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.ok is True
        assert result.version_policy == "known_unsafe"
        assert result.mode == "aggregate_only"

    def test_active_prefix_dropin_fails(self, tmp_path):
        runner = _healthy_runner(tmp_path)
        runner.set_response(
            "systemd-analyze --user unit-paths",
            stdout=f"{tmp_path}\n",
        )
        dropin = tmp_path / "tmux-spawn-.scope.d"
        dropin.mkdir()
        (dropin / "50-oomguard.conf").write_text("[Scope]\nMemoryMax=8G\n")
        result = run_activation_preflight(runner=runner)
        assert result.ok is False
        prefix_check = next(c for c in result.checks if c.code == "prefix_dropin_scan")
        assert prefix_check.ok is False
        assert len(result.enabled_prefix_dropins) == 1

    def test_memory_high_mismatch_fails(self):
        runner = _healthy_runner()
        runner.set_response(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            stdout=(
                "MemoryHigh=8589934592\n"
                f"MemoryMax={_EXPECTED_MEMORY_MAX}\n"
                f"MemorySwapMax={_EXPECTED_MEMORY_SWAP_MAX}\n"
                "ManagedOOMSwap=auto\n"
            ),
        )
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.ok is False
        mh_check = next(c for c in result.checks if c.code == "aggregate_memory_high")
        assert mh_check.ok is False
        assert mh_check.observed == 8589934592
        assert mh_check.expected == _EXPECTED_MEMORY_HIGH

    def test_infinity_memory_fails(self):
        runner = _healthy_runner()
        runner.set_response(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            stdout=(
                "MemoryHigh=infinity\n"
                f"MemoryMax={_EXPECTED_MEMORY_MAX}\n"
                f"MemorySwapMax={_EXPECTED_MEMORY_SWAP_MAX}\n"
                "ManagedOOMSwap=auto\n"
            ),
        )
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.ok is False
        mh_check = next(c for c in result.checks if c.code == "aggregate_memory_high")
        assert mh_check.ok is False
        assert "infinity" in (mh_check.detail or "")

    def test_managed_oom_swap_mismatch_fails(self):
        runner = _healthy_runner()
        runner.set_response(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            stdout=(
                f"MemoryHigh={_EXPECTED_MEMORY_HIGH}\n"
                f"MemoryMax={_EXPECTED_MEMORY_MAX}\n"
                f"MemorySwapMax={_EXPECTED_MEMORY_SWAP_MAX}\n"
                "ManagedOOMSwap=kill\n"
            ),
        )
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.ok is False
        oom_check = next(c for c in result.checks if c.code == "aggregate_managed_oom_swap")
        assert oom_check.ok is False
        assert oom_check.observed == "kill"

    def test_manager_timeout_fails_closed(self):
        runner = FakeRunner()
        runner.set_response("systemctl --version", stdout="systemd 256 (256.11-1.fc42)\n+PAM")
        runner.set_response("systemd-analyze --user unit-paths", stdout="/tmp\n")
        runner.set_timeout(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap"
        )
        runner.set_response("systemctl --user list-jobs --no-pager", stdout="No jobs pending.\n")
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.ok is False
        unresponsive = [c for c in result.checks if c.code == "manager_unresponsive"]
        assert len(unresponsive) >= 1
        assert unresponsive[0].ok is False

    def test_jobs_present_is_warning_only(self):
        runner = _healthy_runner()
        runner.set_response(
            "systemctl --user list-jobs --no-pager",
            stdout="123 app-timer.timer start waiting\n456 cleanup.service stop running\n",
        )
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.ok is True  # jobs are warning-only
        jobs_check = next(c for c in result.checks if c.code == "jobs_diagnostic")
        assert jobs_check.ok is True
        assert jobs_check.observed is True  # has_jobs=True

    def test_version_parse_timeout_marks_unresponsive(self):
        runner = FakeRunner()
        runner.set_timeout("systemctl --version")
        runner.set_response("systemd-analyze --user unit-paths", stdout="/tmp\n")
        runner.set_response(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            stdout=(
                f"MemoryHigh={_EXPECTED_MEMORY_HIGH}\n"
                f"MemoryMax={_EXPECTED_MEMORY_MAX}\n"
                f"MemorySwapMax={_EXPECTED_MEMORY_SWAP_MAX}\n"
                "ManagedOOMSwap=auto\n"
            ),
        )
        runner.set_response("systemctl --user list-jobs --no-pager", stdout="No jobs pending.\n")
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.ok is False
        codes = [c.code for c in result.checks if not c.ok]
        assert "version_parse" in codes
        assert "manager_unresponsive" in codes

    def test_command_not_found_fails_closed(self):
        runner = FakeRunner()
        runner.set_error("systemctl --version", FileNotFoundError("systemctl not found"))
        runner.set_error(
            "systemd-analyze --user unit-paths", FileNotFoundError("systemd-analyze not found")
        )
        runner.set_error(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            FileNotFoundError("systemctl not found"),
        )
        runner.set_error(
            "systemctl --user list-jobs --no-pager",
            FileNotFoundError("systemctl not found"),
        )
        result = run_activation_preflight(runner=runner)
        assert result.ok is False
        for check in result.checks:
            assert check.ok is False

    def test_result_is_serializable(self):
        """AC7: result must be JSON-serializable."""
        import json
        from dataclasses import asdict

        runner = _healthy_runner()
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        d = asdict(result)
        d["enabled_prefix_dropins"] = list(d["enabled_prefix_dropins"])
        d["checks"] = [
            {k: list(v) if isinstance(v, tuple) else v for k, v in c.items()}
            for c in d["checks"]
        ]
        serialized = json.dumps(d)
        assert serialized  # doesn't throw


# ---------------------------------------------------------------------------
# Mutation denial tests (AC4, AC8)
# ---------------------------------------------------------------------------


class TestCommandDenylist:
    """Prove no mutation command can pass through the allowlist."""

    @pytest.mark.parametrize("cmd", sorted(_DENIED_COMMANDS))
    def test_denied_commands_raise(self, cmd):
        with pytest.raises(PermissionError, match="F137: denied command"):
            _enforce_allowlist([cmd, "arg1"])

    @pytest.mark.parametrize("verb", sorted(_DENIED_SYSTEMCTL_VERBS))
    def test_denied_systemctl_verbs_raise(self, verb):
        with pytest.raises(PermissionError, match="F137: denied systemctl verb"):
            _enforce_allowlist(["systemctl", "--user", verb, "some.service"])

    def test_allowed_systemctl_show_passes(self):
        _enforce_allowlist(["systemctl", "--user", "show", "app.slice", "-pMemoryHigh"])

    def test_allowed_systemctl_list_jobs_passes(self):
        _enforce_allowlist(["systemctl", "--user", "list-jobs", "--no-pager"])

    def test_empty_command_raises(self):
        with pytest.raises(ValueError, match="empty command"):
            _enforce_allowlist([])

    def test_default_runner_enforces_denylist(self):
        """DefaultCommandRunner calls _enforce_allowlist before subprocess."""
        runner = DefaultCommandRunner()
        with pytest.raises(PermissionError):
            runner.run(["systemd-run", "--user", "--scope", "true"])

    def test_no_mutation_in_preflight_calls(self):
        """Record all commands issued by a full preflight run and assert none mutate."""
        runner = _healthy_runner()
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            run_activation_preflight(runner=runner)
        for call in runner.calls:
            cmd = Path(call[0]).name if call else ""
            assert cmd not in _DENIED_COMMANDS, f"preflight issued denied cmd: {call}"
            if cmd == "systemctl":
                for arg in call[1:]:
                    if arg.startswith("-"):
                        continue
                    assert arg not in _DENIED_SYSTEMCTL_VERBS, (
                        f"preflight issued denied verb: {call}"
                    )
                    break


# ---------------------------------------------------------------------------
# Mutant tests (AC11 — killed mutants)
# ---------------------------------------------------------------------------


class TestMutantKills:
    """Each test kills a specific mutant from the AC11 ledger."""

    def test_mutant_ignore_active_prefix(self, tmp_path):
        """If we ignore an active .conf, preflight wrongly passes."""
        runner = _healthy_runner()
        runner.set_response("systemd-analyze --user unit-paths", stdout=f"{tmp_path}\n")
        dropin = tmp_path / "tmux-spawn-.scope.d"
        dropin.mkdir()
        (dropin / "50-oomguard.conf").write_text("[Scope]\nMemoryMax=8G\n")
        result = run_activation_preflight(runner=runner)
        assert result.ok is False

    def test_mutant_trust_disabled_as_active(self, tmp_path):
        """A .conf.disabled must NOT block activation."""
        runner = _healthy_runner()
        runner.set_response("systemd-analyze --user unit-paths", stdout=f"{tmp_path}\n")
        dropin = tmp_path / "tmux-spawn-.scope.d"
        dropin.mkdir()
        (dropin / "50-oomguard.conf.disabled").write_text("[Scope]\n")
        result = run_activation_preflight(runner=runner)
        assert result.ok is True

    def test_mutant_version_ge_enablement(self):
        """A >=260 version must NOT enable any mutation mode."""
        runner = _healthy_runner()
        runner.set_response("systemctl --version", stdout="systemd 261 (261.0-1.fc46)\n")
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.mode == "aggregate_only"

    def test_mutant_tolerate_aggregate_mismatch(self):
        """A mismatched aggregate value must fail."""
        runner = _healthy_runner()
        runner.set_response(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            stdout=(
                f"MemoryHigh={_EXPECTED_MEMORY_HIGH}\n"
                "MemoryMax=999\n"
                f"MemorySwapMax={_EXPECTED_MEMORY_SWAP_MAX}\n"
                "ManagedOOMSwap=auto\n"
            ),
        )
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.ok is False
        mm_check = next(c for c in result.checks if c.code == "aggregate_memory_max")
        assert mm_check.ok is False

    def test_mutant_skip_managed_oom_swap_check(self):
        """Missing ManagedOOMSwap must fail — can't skip this check."""
        runner = _healthy_runner()
        runner.set_response(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            stdout=(
                f"MemoryHigh={_EXPECTED_MEMORY_HIGH}\n"
                f"MemoryMax={_EXPECTED_MEMORY_MAX}\n"
                f"MemorySwapMax={_EXPECTED_MEMORY_SWAP_MAX}\n"
            ),
        )
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        assert result.ok is False
        oom_check = next(c for c in result.checks if c.code == "aggregate_managed_oom_swap")
        assert oom_check.ok is False

    def test_mutant_invoke_mutation(self):
        """No mutation command can be issued — _enforce_allowlist blocks them all."""
        for cmd in _DENIED_COMMANDS:
            with pytest.raises(PermissionError):
                _enforce_allowlist([cmd])
        for verb in _DENIED_SYSTEMCTL_VERBS:
            with pytest.raises(PermissionError):
                _enforce_allowlist(["systemctl", "--user", verb, "x"])

    def test_mutant_retry_on_timeout(self):
        """Timeout must fail immediately — no retry loop."""
        runner = FakeRunner()
        runner.set_timeout("systemctl --version")
        runner.set_response("systemd-analyze --user unit-paths", stdout="/tmp\n")
        runner.set_response(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            stdout=(
                f"MemoryHigh={_EXPECTED_MEMORY_HIGH}\n"
                f"MemoryMax={_EXPECTED_MEMORY_MAX}\n"
                f"MemorySwapMax={_EXPECTED_MEMORY_SWAP_MAX}\n"
                "ManagedOOMSwap=auto\n"
            ),
        )
        runner.set_response("systemctl --user list-jobs --no-pager", stdout="")
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        # Only one call to "systemctl --version" — no retry
        version_calls = [c for c in runner.calls if c == ["systemctl", "--version"]]
        assert len(version_calls) == 1
        assert result.ok is False


# ---------------------------------------------------------------------------
# F137 empirical-gate fold: regression tests for N1 and N2 findings
# ---------------------------------------------------------------------------


class TestEmpiricalGateFoldRegressions:
    """Regression tests for findings from empirical-review-fx137-r2.

    N1: Version 2590+ must NOT be classified as known_unsafe.
    N2: Duplicate scalar property must fail closed (return None).
    """

    # -- N1: Version prefix false-positive regression --

    def test_version_2590_not_known_unsafe(self):
        """Version 2590 must NOT match known_unsafe (was startswith bug)."""
        ver, policy, ok = _parse_version("systemd 2590 (2590.1-1.fc99)\n+PAM")
        assert ok is True
        assert ver == "2590"
        assert policy == "unqualified"

    def test_version_25900_not_known_unsafe(self):
        """Version 25900 must NOT match known_unsafe."""
        ver, policy, ok = _parse_version("systemd 25900 (25900.0-1)\n")
        assert ok is True
        assert policy == "unqualified"

    def test_version_259_exact_is_known_unsafe(self):
        """Version 259 (bare major) is known_unsafe."""
        ver, policy, ok = _parse_version("systemd 259 (259-1.fc44)\n")
        assert ok is True
        assert ver == "259"
        assert policy == "known_unsafe"

    def test_version_259_with_minor_is_known_unsafe(self):
        """Version 259.8 is known_unsafe (major is still 259)."""
        ver, policy, ok = _parse_version("systemd 259.8 (259.8-1.fc44)\n+PAM")
        assert ok is True
        assert ver == "259.8"
        assert policy == "known_unsafe"

    # -- N2: Duplicate scalar property fail-closed regression --

    def test_duplicate_property_returns_none(self):
        """Duplicate scalar property must fail closed (return None)."""
        output = "MemoryHigh=10737418240\nMemoryHigh=999\n"
        result = _parse_property_value(output, "MemoryHigh")
        assert result is None

    def test_duplicate_property_different_values_returns_none(self):
        """Duplicate with different values must still fail closed."""
        output = "MemoryMax=12884901888\nMemorySwapMax=6442450944\nMemoryMax=0\n"
        result = _parse_property_value(output, "MemoryMax")
        assert result is None

    def test_single_property_still_works(self):
        """A single occurrence still returns the value normally."""
        output = "MemoryHigh=10737418240\nMemoryMax=12884901888\n"
        result = _parse_property_value(output, "MemoryHigh")
        assert result == "10737418240"

    def test_no_property_returns_none(self):
        """Missing property returns None."""
        output = "MemoryMax=12884901888\n"
        result = _parse_property_value(output, "MemoryHigh")
        assert result is None


# ---------------------------------------------------------------------------
# F137 G7-B1 regression: version probe binary path (Fedora 44 PATH contract)
# ---------------------------------------------------------------------------


class TestVersionProbeBinaryPath:
    """Regression guard: version probe MUST use systemctl, not bare systemd.

    On Fedora 44, bare 'systemd' is absent from PATH (/usr/lib/systemd/systemd
    exists but isn't linked into /usr/bin). The standard command is
    'systemctl --version' which is always at /usr/bin/systemctl and returns
    identical version output.

    These tests model the real PATH contract and kill any reversion to
    ["systemd", "--version"].
    """

    def test_version_probe_uses_systemctl(self):
        """The version probe command must be ['systemctl', '--version']."""
        runner = FakeRunner()
        # Only register systemctl --version (simulating systemd NOT in PATH)
        runner.set_response("systemctl --version", stdout="systemd 259 (259.8-1.fc44)\n+PAM")
        runner.set_response("systemd-analyze --user unit-paths", stdout="/tmp\n")
        runner.set_response(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            stdout=(
                f"MemoryHigh={_EXPECTED_MEMORY_HIGH}\n"
                f"MemoryMax={_EXPECTED_MEMORY_MAX}\n"
                f"MemorySwapMax={_EXPECTED_MEMORY_SWAP_MAX}\n"
                "ManagedOOMSwap=auto\n"
            ),
        )
        runner.set_response("systemctl --user list-jobs --no-pager", stdout="No jobs pending.\n")
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        # Must successfully parse version and classify as known_unsafe
        assert result.version == "259"
        assert result.version_policy == "known_unsafe"
        version_check = next(c for c in result.checks if c.code == "version_parse")
        assert version_check.ok is True
        # Verify the actual command issued was systemctl, not systemd
        assert ["systemctl", "--version"] in runner.calls
        assert ["systemd", "--version"] not in runner.calls

    def test_bare_systemd_not_in_path_still_classifies_correctly(self):
        """When bare 'systemd' would FileNotFoundError, systemctl works fine.

        Models Fedora 44 where /usr/bin/systemd does not exist.
        The FakeRunner returns empty (default) for unregistered keys,
        but here we explicitly set systemd --version to raise FileNotFoundError
        to prove the code never tries it.
        """
        runner = FakeRunner()
        # Bare systemd raises — but code should never call it
        runner.set_error("systemd --version", FileNotFoundError("No such file or directory: 'systemd'"))
        # systemctl works fine
        runner.set_response("systemctl --version", stdout="systemd 259 (259.8-1.fc44)\n+PAM")
        runner.set_response("systemd-analyze --user unit-paths", stdout="/tmp\n")
        runner.set_response(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            stdout=(
                f"MemoryHigh={_EXPECTED_MEMORY_HIGH}\n"
                f"MemoryMax={_EXPECTED_MEMORY_MAX}\n"
                f"MemorySwapMax={_EXPECTED_MEMORY_SWAP_MAX}\n"
                "ManagedOOMSwap=auto\n"
            ),
        )
        runner.set_response("systemctl --user list-jobs --no-pager", stdout="No jobs pending.\n")
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        # Preflight passes and correctly identifies version
        assert result.ok is True
        assert result.version == "259"
        assert result.version_policy == "known_unsafe"

    def test_systemctl_unavailable_fails_closed(self):
        """When systemctl itself is unavailable, preflight fails closed."""
        runner = FakeRunner()
        runner.set_error("systemctl --version", FileNotFoundError("No such file or directory: 'systemctl'"))
        runner.set_response("systemd-analyze --user unit-paths", stdout="/tmp\n")
        runner.set_error(
            "systemctl --user show app.slice -pMemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap",
            FileNotFoundError("systemctl not found"),
        )
        runner.set_error(
            "systemctl --user list-jobs --no-pager",
            FileNotFoundError("systemctl not found"),
        )
        with patch(
            "cli_agent_orchestrator.services.systemd_tmux_preflight._scan_prefix_dropins",
            return_value=(),
        ):
            result = run_activation_preflight(runner=runner)
        # Fail-closed: version_parse fails, aggregates fail
        assert result.ok is False
        assert result.version is None
        assert result.version_policy == "unqualified"
        version_check = next(c for c in result.checks if c.code == "version_parse")
        assert version_check.ok is False
        assert "unavailable" in (version_check.detail or "")
