from pathlib import Path

from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services import verification_service as svc


def test_stamp_round_trip_and_stale_detection(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    log = tmp_path / "suite.log"
    stamp = {"commit": "abc", "dirty": {"x.py": "123"}, "timestamp": "2026-07-11T12:00:00+00:00", "cwd": str(root)}
    with log.open("w") as stream:
        svc.write_stamp(stream, stamp)
        stream.write("12 passed, 2 skipped in 1.0s\n")
    monkeypatch.setattr(svc, "git_root", lambda cwd=None: root)
    monkeypatch.setattr(svc, "tree_stamp", lambda value: stamp)
    passed, reasons, _ = svc.verify_suite_log(log)
    assert passed and reasons == []

    monkeypatch.setattr(svc, "tree_stamp", lambda value: {**stamp, "commit": "def"})
    passed, reasons, _ = svc.verify_suite_log(log)
    assert not passed
    assert "HEAD differs" in reasons


def test_verify_suite_log_command_failure(tmp_path):
    log = tmp_path / "bad.log"
    log.write_text("not stamped\n")
    result = CliRunner().invoke(cli, ["verify", "suite-log", str(log)])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def _stamped_log(path, root, body, **overrides):
    stamp = {"commit": "abc", "dirty": {}, "timestamp": "2026-07-11T12:00:00+00:00", "cwd": str(root)}
    stamp.update(overrides)
    with path.open("w") as stream:
        svc.write_stamp(stream, stamp)
        stream.write(body)


def test_suite_log_rejects_errors_interruption_and_missing_summary(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(svc, "git_root", lambda cwd=None: root)
    monkeypatch.setattr(svc, "tree_stamp", lambda value: {
        "commit": "abc", "dirty": {}, "timestamp": "later", "cwd": str(root)
    })
    cases = {
        "error": "3 passed, 1 error in 1.0s\n",
        "errors": "3 passed, 2 errors in 1.0s\n",
        "interrupted": "3 passed in 1.0s\nKeyboardInterrupt\n",
        "missing": "collected 3 items\n...\n",
    }
    for name, body in cases.items():
        log = tmp_path / f"{name}.log"
        _stamped_log(log, root, body)
        passed, reasons, _ = svc.verify_suite_log(log)
        assert not passed, name
        assert reasons, name


def test_suite_log_rejects_adversarial_late_and_multiple_outcomes(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(svc, "git_root", lambda cwd=None: root)
    monkeypatch.setattr(svc, "tree_stamp", lambda value: {
        "commit": "abc", "dirty": {}, "timestamp": "2026-07-11T12:01:00+00:00",
        "cwd": str(root),
    })
    adversarial = (
        "4 passed in 1.0s\nERROR collecting late.py\n1 error\n",
        "1 error in 0.2s\n4 passed in 1.0s\n",
    )
    for index, body in enumerate(adversarial):
        log = tmp_path / f"adversarial-{index}.log"
        _stamped_log(log, root, body)
        passed, reasons, _ = svc.verify_suite_log(log)
        assert not passed
        assert reasons


def test_suite_log_rejects_pytest_failure_and_error_markers(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(svc, "git_root", lambda cwd=None: root)
    monkeypatch.setattr(svc, "tree_stamp", lambda value: {
        "commit": "abc", "dirty": {}, "timestamp": "2026-07-11T12:01:00+00:00",
        "cwd": str(root),
    })
    marker_tampers = (
        "4 passed in 1.0s\nERROR collecting evil.py\n",
        "FAILED test_x.py::test_y - AssertionError\n4 passed in 1.0s\n",
    )
    for index, body in enumerate(marker_tampers):
        log = tmp_path / f"marker-{index}.log"
        _stamped_log(log, root, body)
        passed, reasons, _ = svc.verify_suite_log(log)
        assert not passed
        assert "suite output contains a pytest failure/error marker" in reasons


def test_suite_log_allows_lowercase_error_words_in_passing_output(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    stamp = {
        "commit": "abc", "dirty": {}, "timestamp": "2026-07-11T12:01:00+00:00",
        "cwd": str(root),
    }
    monkeypatch.setattr(svc, "git_root", lambda cwd=None: root)
    monkeypatch.setattr(svc, "tree_stamp", lambda value: stamp)
    log = tmp_path / "passing-error-name.log"
    _stamped_log(
        log, root,
        "test_error_handler.py::test_error_message PASSED\n"
        "warnings summary\n4 passed, 1 warning in 1.0s\n",
    )
    passed, reasons, _ = svc.verify_suite_log(log)
    assert passed
    assert reasons == []


def test_pytest_summary_accepts_optional_wall_clock_suffix():
    assert svc.pytest_summary_error(
        "4481 passed, 13 skipped, 101 deselected in 60.03s (0:01:00)\n"
    ) is None
    assert svc.pytest_summary_error(
        "4481 passed in 86400.00s (12:34:56)\n"
    ) is None


def test_suffixed_pytest_summary_still_rejects_failure_tampers():
    counted = "4481 passed, 2 failed in 60.03s (0:01:00)\n"
    marker = (
        "4481 passed in 60.03s (0:01:00)\n"
        "FAILED test_x.py::test_y - AssertionError\n"
    )
    assert svc.pytest_summary_error(counted) == (
        "suite output contains a nonzero failed/error outcome"
    )
    assert svc.pytest_summary_error(marker) == (
        "suite output contains a pytest failure/error marker"
    )


def test_suite_log_rejects_invalid_types_and_foreign_cwd(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(svc, "git_root", lambda cwd=None: root)
    monkeypatch.setattr(svc, "tree_stamp", lambda value: {
        "commit": "abc", "dirty": {}, "timestamp": "now", "cwd": str(root)
    })
    invalid = tmp_path / "invalid.log"
    _stamped_log(invalid, root, "3 passed in 1.0s\n", dirty=[])
    passed, reasons, _ = svc.verify_suite_log(invalid)
    assert not passed
    assert "invalid stamp field type: dirty" in reasons

    foreign = tmp_path / "foreign.log"
    _stamped_log(foreign, root, "3 passed in 1.0s\n", cwd=str(tmp_path / "other"))
    passed, reasons, _ = svc.verify_suite_log(foreign)
    assert not passed
    assert "stamped cwd differs from current repository root" in reasons

    bad_time = tmp_path / "bad-time.log"
    _stamped_log(bad_time, root, "3 passed in 1.0s\n", timestamp="not-a-time")
    passed, reasons, _ = svc.verify_suite_log(bad_time)
    assert not passed
    assert "invalid stamp timestamp" in reasons


class FakeProcess:
    def __init__(self, lines, code):
        self.stdout = iter(lines)
        self.code = code

    def wait(self):
        return self.code


def test_run_suite_success_atomically_writes_stamp(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(svc, "git_root", lambda cwd=None: root)
    monkeypatch.setattr(svc, "tree_stamp", lambda value: {
        "commit": "abc", "dirty": {}, "timestamp": "2026-07-11T12:00:00+00:00", "cwd": str(root)
    })
    monkeypatch.setattr(svc.subprocess, "Popen", lambda *a, **k: FakeProcess(["3 passed in .1s\n"], 0))
    output = __import__("io").StringIO()
    code, path, summary = svc.run_suite("demo", output)
    assert code == 0
    assert summary == "3 passed in .1s"
    stamp, body = svc.parse_stamp(path)
    assert stamp["commit"] == "abc"
    assert "3 passed" in body


def test_run_suite_failure_preserves_existing_log(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    target = root / "tmp/orch/suite-demo.log"
    target.parent.mkdir(parents=True)
    target.write_text("old")
    monkeypatch.setattr(svc, "git_root", lambda cwd=None: root)
    monkeypatch.setattr(svc, "tree_stamp", lambda value: {
        "commit": "abc", "dirty": {}, "timestamp": "2026-07-11T12:00:00+00:00", "cwd": str(root)
    })
    monkeypatch.setattr(svc.subprocess, "Popen", lambda *a, **k: FakeProcess(["1 failed in .1s\n"], 1))
    code, _, _ = svc.run_suite("demo", __import__("io").StringIO())
    assert code == 1
    assert target.read_text() == "old"


def test_verify_deploy_reports_states(monkeypatch):
    import cli_agent_orchestrator.cli.commands.verify as command

    monkeypatch.setattr(command, "git_root", lambda: Path("/repo"))
    monkeypatch.setattr(command, "installed_package_root", lambda: Path("/installed"))
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    monkeypatch.setattr(command, "compare_installed", lambda *args: ("stale", 2, 200.0))
    monkeypatch.setattr(command, "listening_pid", lambda port: 42)
    monkeypatch.setattr(command, "process_start_time", lambda pid: 100.0)
    result = CliRunner().invoke(cli, ["verify", "deploy"])
    assert result.exit_code == 1
    assert "CLI path: stale (2 files differ)" in result.output
    assert "server: restart-needed" in result.output


def test_verify_deploy_current_is_only_success(monkeypatch):
    import cli_agent_orchestrator.cli.commands.verify as command

    monkeypatch.setattr(command, "git_root", lambda: Path("/repo"))
    monkeypatch.setattr(command, "installed_package_root", lambda: Path("/installed"))
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    monkeypatch.setattr(command, "compare_installed", lambda *args: ("current", 0, 100.0))
    monkeypatch.setattr(command, "listening_pid", lambda port: 42)
    monkeypatch.setattr(command, "process_start_time", lambda pid: 200.0)
    result = CliRunner().invoke(cli, ["verify", "deploy"])
    assert result.exit_code == 0
    assert "server: current" in result.output


def test_verify_deploy_unknown_and_install_not_found_fail(monkeypatch):
    import cli_agent_orchestrator.cli.commands.verify as command

    monkeypatch.setattr(command, "git_root", lambda: Path("/repo"))
    monkeypatch.setattr(command, "installed_package_root", lambda: Path("/installed"))
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    monkeypatch.setattr(command, "compare_installed", lambda *args: ("current", 0, 100.0))
    monkeypatch.setattr(command, "listening_pid", lambda port: 42)
    monkeypatch.setattr(command, "process_start_time", lambda pid: None)
    result = CliRunner().invoke(cli, ["verify", "deploy"])
    assert result.exit_code == 1
    assert "server: unknown" in result.output

    monkeypatch.setattr(command, "installed_package_root", lambda: None)
    monkeypatch.setattr(command, "listening_pid", lambda port: None)
    result = CliRunner().invoke(cli, ["verify", "deploy"])
    assert result.exit_code == 1
    assert "CLI path: not-found" in result.output
    assert "server: not-running" in result.output


def test_verify_scope_happy_and_failure(tmp_path, monkeypatch):
    import cli_agent_orchestrator.cli.commands.verify as command

    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(command, "git_root", lambda: root)
    monkeypatch.setattr(command, "changed_files", lambda value: ["a.py"])
    assert CliRunner().invoke(cli, ["verify", "scope", "a.py"]).exit_code == 0
    result = CliRunner().invoke(cli, ["verify", "scope", "b.py"])
    assert result.exit_code == 1
    assert "unexpected changes: a.py" in result.output
    assert "missing expected: b.py" in result.output


def test_ledger_check_warns_for_drained_header_and_counts_pending(tmp_path, monkeypatch):
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_text(
        "## POST-RESTART RE-ENTRY\nFeature Alpha needs work\n\n"
        "## Live ledger\n"
        "| feature | assertion | commit | probe | status | notes |\n"
        "|---|---|---|---|---|---|\n"
        "| Feature Alpha | x | c | p | drained-pass | n |\n"
        "| Feature Beta | x | c | p | pending | n |\n"
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ledger", "check"])
    assert result.exit_code == 0
    assert "warning:" in result.output
    assert "pending-row count: 1" in result.output


def test_ledger_check_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ledger", "check"])
    assert result.exit_code != 0
    assert "HANDOFF.md not found" in result.output
