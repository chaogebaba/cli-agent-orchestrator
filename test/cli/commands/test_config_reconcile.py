"""Acceptance coverage for ``cao config reconcile`` and redeploy config reset."""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import config_reconcile as command
from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.utils import agent_profiles

_TEMPLATE = b'[codex]\nmodel = "gpt-current"\nreasoning_effort = "high"\n'


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_profile(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {name}\nprovider: codex\n---\nPrompt for {name}.\n",
        encoding="utf-8",
    )


@pytest.fixture
def reconcile_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    workspace = tmp_path / "workspace"
    profiles = workspace / "profiles"
    profiles.mkdir(parents=True)
    template = workspace / "providers.toml.default"
    template.write_bytes(_TEMPLATE)
    cao_home = tmp_path / "cao-home"
    store = cao_home / "agent-store"
    context = cao_home / "agent-context"
    settings = cao_home / "settings.json"

    monkeypatch.setattr(command, "CAO_HOME_DIR", cao_home)
    monkeypatch.setattr(command, "_workspace_root", lambda: workspace)
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", store)
    monkeypatch.setattr(settings_service, "CAO_HOME_DIR", cao_home)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings)
    monkeypatch.setattr(
        settings_service,
        "_DEFAULTS",
        {
            "kiro_cli": str(tmp_path / "kiro-agents"),
            "claude_code": str(store),
            "codex": str(store),
            "cao_installed": str(context),
        },
    )
    return {
        "workspace": workspace,
        "profiles": profiles,
        "template": template,
        "cao_home": cao_home,
        "target": cao_home / "providers.toml",
        "store": store,
        "context": context,
    }


def test_help_surfaces_and_plain_reconcile_preserves_existing_bytes(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "hand-edited"\n')
    before = _digest(target)

    config_help = CliRunner().invoke(cli, ["config", "reconcile", "--help"])
    redeploy_help = CliRunner().invoke(cli, ["redeploy", "--help"])
    result = CliRunner().invoke(cli, ["config", "reconcile"])

    assert config_help.exit_code == 0
    assert "--force-providers" in config_help.output
    assert "--audit-only" in config_help.output
    assert redeploy_help.exit_code == 0
    assert "--force-providers" in redeploy_help.output
    assert result.exit_code == 0
    assert _digest(target) == before
    assert not list(target.parent.glob("providers.toml.bak.*"))


def test_force_backs_up_old_bytes_and_publishes_template(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    old_bytes = b'[codex]\nmodel = "old"\n'
    target.write_bytes(old_bytes)
    monkeypatch.setattr(command, "_backup_timestamp", lambda: "20260725T120000Z")

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    backup = target.with_name("providers.toml.bak.20260725T120000Z")
    assert result.exit_code == 0
    assert backup.read_bytes() == old_bytes
    assert target.read_bytes() == reconcile_env["template"].read_bytes()


def test_backup_collision_uses_dash_two_without_overwriting(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    old_bytes = b'[codex]\nmodel = "old"\n'
    target.write_bytes(old_bytes)
    monkeypatch.setattr(command, "_backup_timestamp", lambda: "20260725T120000Z")
    first = target.with_name("providers.toml.bak.20260725T120000Z")
    first.write_bytes(b"keep me")

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code == 0
    assert first.read_bytes() == b"keep me"
    assert first.with_name(f"{first.name}-2").read_bytes() == old_bytes


def test_malformed_template_fails_before_backup_or_live_mutation(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)
    reconcile_env["template"].write_text("[codex\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code != 0
    assert "invalid providers template" in result.output
    assert _digest(target) == before
    assert not list(target.parent.glob("providers.toml.bak.*"))


def test_replace_failure_leaves_live_file_whole_and_cleans_temp(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)

    def fail_replace(source, destination):
        raise OSError("publish failed")

    monkeypatch.setattr(command.os, "replace", fail_replace)
    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    source = inspect.getsource(command)
    assert result.exit_code != 0
    assert _digest(target) == before
    assert not list(target.parent.glob("providers.toml.*.tmp"))
    assert "shutil.copyfile" not in source


def test_force_with_absent_target_is_an_ordinary_seed_without_backup(reconcile_env):
    target = reconcile_env["target"]

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code == 0
    assert target.read_bytes() == reconcile_env["template"].read_bytes()
    assert not list(target.parent.glob("providers.toml.bak.*"))


def test_orphan_audit_reports_context_and_store_without_deleting(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(_TEMPLATE)
    context_orphan = reconcile_env["context"] / "context_orphan.md"
    store_orphan = reconcile_env["store"] / "store_orphan.md"
    _write_profile(context_orphan, "context_orphan")
    _write_profile(store_orphan, "store_orphan")

    result = CliRunner().invoke(cli, ["config", "reconcile"])

    assert result.exit_code == 0
    assert f"orphan profile context_orphan in {reconcile_env['context']}" in result.stderr
    assert f"orphan profile store_orphan in {reconcile_env['store']}" in result.stderr
    assert "orphan profile developer" not in result.stderr
    assert "orphan profile reviewer" not in result.stderr
    assert context_orphan.exists()
    assert store_orphan.exists()


def test_dead_stanza_warns_but_loadable_builtin_stanza_does_not(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_text(
        '[codex]\nmodel = "gpt-current"\n'
        '[codex.profiles.does_not_exist]\nreasoning_effort = "high"\n'
        '[codex.profiles.developer]\nreasoning_effort = "high"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["config", "reconcile"])

    assert result.exit_code == 0
    assert "[codex.profiles.does_not_exist] names no known profile" in result.stderr
    assert "[codex.profiles.developer] names" not in result.stderr


def test_template_drift_names_changed_key_and_identical_mapping_is_quiet(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "old"\nreasoning_effort = "high"\n')

    drifted = CliRunner().invoke(cli, ["config", "reconcile"])
    target.write_bytes(b"# hand comment\n" + _TEMPLATE)
    identical = CliRunner().invoke(cli, ["config", "reconcile"])

    assert drifted.exit_code == 0
    assert "providers.toml differs at codex.model" in drifted.stderr
    assert "--force-providers" in drifted.stderr
    assert identical.exit_code == 0
    assert "providers.toml differs" not in identical.stderr


def test_sandbox_guard_runs_before_reconcile_or_mutation(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)
    monkeypatch.setenv("CAO_INSTANCE_ID", "sandbox-test")

    def mutation_sink(*args, **kwargs):
        pytest.fail("reconcile reached before sandbox guard")

    monkeypatch.setattr(command, "_reconcile_config", mutation_sink)
    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code != 0
    assert "sandbox mutation forbidden" in str(result.exception)
    assert _digest(target) == before
    assert not list(target.parent.glob("providers.toml.bak.*"))


def test_root_installer_delegates_without_toml_or_stanza_parsing():
    install_script = Path(__file__).resolve().parents[4] / "install.sh"
    contents = install_script.read_text(encoding="utf-8")

    assert contents.count("cao config reconcile") == 1
    assert "toml" not in contents.lower()


def test_read_only_cao_home_aborts_before_live_mutation(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)
    (target.parent / "providers.toml.lock").touch()
    target.parent.chmod(0o500)

    try:
        result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])
    finally:
        target.parent.chmod(0o700)

    assert result.exit_code != 0, "read-only CAO_HOME unexpectedly allowed a backup"
    assert _digest(target) == before


def test_live_flock_contender_fails_without_touching_target(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)
    lock_path = target.parent / "providers.toml.lock"

    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code != 0
    assert "another redeploy holds the config lock" in result.output
    assert _digest(target) == before


def test_flock_is_released_when_holder_is_sigkilled(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    lock_path = target.parent / "providers.toml.lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, signal, sys; "
                "stream=open(sys.argv[1], 'a+b'); "
                "fcntl.flock(stream.fileno(), fcntl.LOCK_EX); "
                "print('ready', flush=True); signal.pause()"
            ),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        child.kill()
        child.wait(timeout=5)
        result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    assert result.exit_code == 0
    assert target.read_bytes() == _TEMPLATE
    assert "os.kill" not in inspect.getsource(command)


def test_unloadable_and_unknown_stanzas_are_reported_distinctly(reconcile_env):
    broken = reconcile_env["context"] / "broken.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("---\nname: [broken\n---\n", encoding="utf-8")
    target = reconcile_env["target"]
    target.write_text(
        '[codex]\nmodel = "gpt-current"\n'
        '[codex.profiles.broken]\nreasoning_effort = "high"\n'
        '[codex.profiles.missing]\nreasoning_effort = "high"\n'
        '[codex.profiles.developer]\nreasoning_effort = "high"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["config", "reconcile"])

    assert result.exit_code == 0
    assert "[codex.profiles.broken] names a profile that fails to load" in result.stderr
    assert "[codex.profiles.missing] names no known profile" in result.stderr
    assert "[codex.profiles.developer] names" not in result.stderr


def test_builtin_context_shadow_warns_but_nonbuiltin_duplicate_does_not(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(_TEMPLATE)
    _write_profile(reconcile_env["context"] / "developer.md", "developer")
    _write_profile(reconcile_env["store"] / "owned.md", "owned")
    _write_profile(reconcile_env["context"] / "owned.md", "owned")
    _write_profile(reconcile_env["profiles"] / "owned.md", "owned")

    result = CliRunner().invoke(cli, ["config", "reconcile"])

    assert result.exit_code == 0
    assert "SHADOW profile developer" in result.stderr
    assert str(reconcile_env["context"]) in result.stderr
    assert "orphan profile developer" not in result.stderr
    assert "SHADOW profile owned" not in result.stderr


def test_force_diff_is_emitted_before_atomic_replace(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "old"\nreasoning_effort = "high"\n')
    events: list[str] = []
    original_emit = command._emit_template_drift
    original_replace = command.os.replace

    def record_diff(*args, **kwargs):
        original_emit(*args, **kwargs)
        events.append("diff")

    def record_replace(source, destination):
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(command, "_emit_template_drift", record_diff)
    monkeypatch.setattr(command.os, "replace", record_replace)
    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code == 0
    assert "providers.toml differs at codex.model" in result.stderr
    assert events == ["diff", "replace"]


def test_audit_only_never_seeds_or_takes_the_writer_lock(reconcile_env):
    lock_path = reconcile_env["cao_home"] / "providers.toml.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = CliRunner().invoke(
            cli,
            ["config", "reconcile", "--audit-only", "--force-providers"],
        )

    assert result.exit_code == 0
    assert "providers.toml is missing" in result.stderr
    assert not reconcile_env["target"].exists()


def test_force_preserves_symlink_and_publishes_to_resolved_target(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    real_target = reconcile_env["workspace"] / "dotfiles" / "providers.toml"
    real_target.parent.mkdir(parents=True)
    real_target.write_bytes(b'[codex]\nmodel = "old"\n')
    target.symlink_to(real_target)

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code == 0
    assert target.is_symlink()
    assert real_target.read_bytes() == _TEMPLATE
    assert list(real_target.parent.glob("providers.toml.bak.*"))
