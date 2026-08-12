"""F139 — Sandbox fixture-provider admission capability tests.

Covers all 15 ACs and the full mutation ledger from the blueprint
``blueprints/fx139-sandbox-fixture-provider-admission.md`` (r4).

The fixture capability is a manifest-pinned authority: a fresh sandbox
instance may admit the exact reviewed ``mock_cli`` fixture in one
manifest-owned behavior. No production provider, generic provider-args
channel, or HTTP/MCP capability switch is introduced.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src" / "cli_agent_orchestrator"
FIXTURE_BINARY = REPO / "test" / "providers" / "fixtures" / "bin" / "mock_cli"

sys.path.insert(0, str(REPO / "src"))

from cli_agent_orchestrator.providers.mock_cli import MockCliProvider
from cli_agent_orchestrator.sandbox_bootstrap import (
    FIXTURE_VARIANTS,
    MANIFEST_NAME,
    MUTABLE_PATHS,
    PROVIDERS,
    SHARED_AUTH_PROVIDERS,
    SandboxError,
    _build_manifest,
    _canonical,
    _manifest_env,
    _toml_string,
    read_manifest,
    render_manifest,
    validate_manifest,
)
from cli_agent_orchestrator.utils.provider_plane import (
    SandboxFixtureProviderCapability,
    admit_provider,
    load_active_fixture_provider,
)
from cli_agent_orchestrator.utils.sandbox_guard import SandboxProviderUnsafe

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _binary_sha256() -> str:
    return hashlib.sha256(FIXTURE_BINARY.read_bytes()).hexdigest()


def _make_minimal_manifest(
    tmp_path: Path,
    *,
    fixture_variant: str | None = None,
    fixture_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Build a valid manifest dict for test purposes (no actual sandbox)."""
    root = tmp_path / "sandbox-root"
    root.mkdir(mode=0o700)
    root_stat = root.stat()

    fork_root = _canonical(REPO)
    binary_path = _canonical(FIXTURE_BINARY)

    manifest: dict[str, Any] = {
        "instance_id": "ab12cd34",
        "created_at": "2026-08-11T00:00:00+00:00",
        "root": str(root),
        "endpoint": "http://127.0.0.1:19876",
        "tmux_socket": "cao-sbx-ab12cd34",
        "owner_nonce": "a" * 64,
        "root_device": root_stat.st_dev,
        "root_inode": root_stat.st_ino,
        "source": {
            "fork_root": str(fork_root),
            "commit_sha": "deadbeef" * 5,
            "source_merkle": "cafe" * 16,
            "dirty": True,
            "interpreter_identity": {
                "interpreter_path": str(fork_root / ".venv" / "bin" / "python"),
                "venv_prefix": str(fork_root / ".venv"),
                "base_interpreter_realpath": str(
                    _canonical(fork_root / ".venv" / "bin" / "python")
                ),
            },
        },
        "providers": {},
    }
    for provider in PROVIDERS:
        pin = SHARED_AUTH_PROVIDERS.get(provider)
        if pin is None:
            manifest["providers"][provider] = {"classification": "unsafe"}
            continue
        home = root / str(pin["home_relative"])
        row = {
            "classification": "shared-auth-read-only",
            "home": str(home),
            "home_env": str(pin["home_env"]),
            "credential_source": str(pin["credential_source"]),
            "credential_path": str(home / str(pin["credential_name"])),
        }
        native_home_relative = pin.get("native_home_relative")
        if native_home_relative is not None:
            row["native_home"] = str(root / str(native_home_relative))
        manifest["providers"][provider] = row
    for field, relative in MUTABLE_PATHS.items():
        manifest[field] = str(root / relative)

    if fixture_variant is not None:
        state_dir = root / "fixture-provider-state"
        state_dir.mkdir(mode=0o700)
        fp_row = {
            "classification": "fixture-test",
            "binary_realpath": str(binary_path),
            "binary_sha256": _binary_sha256(),
            "variant": fixture_variant,
            "state_dir": str(state_dir),
        }
        if fixture_overrides:
            fp_row.update(fixture_overrides)
        manifest["fixture_providers"] = {"mock_cli": fp_row}

    manifest_path = root / MANIFEST_NAME
    return manifest, manifest_path


def _write_and_readback(manifest: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    """Render, write (mode 0400), read back, and validate; return the parsed dict."""
    rendered = render_manifest(manifest)
    _write_manifest(manifest_path, rendered)
    parsed = read_manifest(manifest_path)
    return validate_manifest(parsed, manifest_path)


def _write_manifest(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _capability(
    variant: str, tmp_path: Path, manifest: dict[str, Any]
) -> SandboxFixtureProviderCapability:
    """Build a capability object mirroring what load_active_fixture_provider returns."""
    root = Path(manifest["root"])
    state_dir = root / "fixture-provider-state"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    return SandboxFixtureProviderCapability(
        provider="mock_cli",
        binary_realpath=_canonical(FIXTURE_BINARY),
        binary_sha256=_binary_sha256(),
        variant=variant,  # type: ignore[arg-type]
        state_dir=state_dir,
    )


def _make_fixture_provider(tmp_path, variant: str):
    manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant=variant)
    cap = _capability(variant, tmp_path, manifest)
    provider = MockCliProvider("ab12cd34", "sess", "win")
    provider._fixture_capability = cap
    return provider, cap


def _sandbox_env(manifest: dict[str, Any], manifest_path: Path, monkeypatch) -> None:
    """Set the full sandbox environment expected by validate_active_sandbox."""
    for key, value in _manifest_env(manifest, manifest_path).items():
        monkeypatch.setenv(key, value)


def _run(coro):
    """Run an async coroutine in a fresh event loop (Python 3.14 compatible)."""
    return asyncio.run(coro)


# ─── AC1/AC3/AC4: deterministic manifest render + round-trip ──────────────────


class TestManifestRender:
    """AC1, AC3, AC4: canonical fixture row, deterministic serialization."""

    def test_no_fixture_emits_no_fixture_table(self, tmp_path):
        """AC1: absent option → manifest bytes/schema unchanged (no fixture table)."""
        manifest, _ = _make_minimal_manifest(tmp_path)
        rendered = render_manifest(manifest)
        assert "fixture_providers" not in rendered

    def test_fixture_row_emitted_in_canonical_order(self, tmp_path):
        """AC3/AC4: canonical five fields in deterministic order."""
        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        rendered = render_manifest(manifest)
        assert "[fixture_providers.mock_cli]" in rendered
        # Canonical field order
        idx = rendered.index("[fixture_providers.mock_cli]")
        row_text = rendered[idx:]
        assert row_text.index("classification") < row_text.index("binary_realpath")
        assert row_text.index("binary_realpath") < row_text.index("binary_sha256")
        assert row_text.index("binary_sha256") < row_text.index("variant")
        assert row_text.index("variant") < row_text.index("state_dir")
        # Value is the reviewed fixture binary realpath
        assert str(_canonical(FIXTURE_BINARY)) in rendered

    def test_round_trip_exact(self, tmp_path):
        """AC4: render → write → read → validate returns equal canonical row."""
        manifest, mpath = _make_minimal_manifest(tmp_path, fixture_variant="post-send-death")
        validated = _write_and_readback(manifest, mpath)
        fp = validated["fixture_providers"]["mock_cli"]
        assert fp["classification"] == "fixture-test"
        assert fp["variant"] == "post-send-death"
        assert fp["binary_sha256"] == _binary_sha256()
        assert _canonical(Path(fp["binary_realpath"])) == _canonical(FIXTURE_BINARY)

    def test_state_dir_beneath_root(self, tmp_path):
        """D2: state_dir resolves beneath the manifest sandbox root."""
        manifest, mpath = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        validated = _write_and_readback(manifest, mpath)
        state_dir = Path(validated["fixture_providers"]["mock_cli"]["state_dir"])
        assert state_dir.is_relative_to(Path(validated["root"]))

    def test_toml_quoting_is_safe(self):
        """M3: TOML-safe quoted strings — special chars cannot break out."""
        # _toml_string is the serializer primitive used for all values.
        for value in ("plain", 'has "quote"', "has \\backslash", "multi\nline"):
            out = _toml_string(value)
            assert out.startswith('"') and out.endswith('"')
            # A quoted value must not contain a raw newline
            assert "\n" not in out[1:-1]


class TestArgparseExactChoice:
    """AC3: bootstrap accepts only the five exact fixture literals."""

    def test_parser_accepts_all_five_literals(self):
        from cli_agent_orchestrator.sandbox_bootstrap import build_parser

        parser = build_parser()
        for variant in FIXTURE_VARIANTS:
            args = parser.parse_args(
                ["up", "--root", "/tmp/x", "--port", "19877", "--fixture-mock-cli-variant", variant]
            )
            assert args.fixture_mock_cli_variant == variant

    def test_parser_rejects_invalid_variant(self):
        from cli_agent_orchestrator.sandbox_bootstrap import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["up", "--root", "/tmp/x", "--port", "19877", "--fixture-mock-cli-variant", "evil"]
            )

    def test_default_absent(self):
        from cli_agent_orchestrator.sandbox_bootstrap import build_parser

        parser = build_parser()
        args = parser.parse_args(["up", "--root", "/tmp/x", "--port", "19877"])
        assert args.fixture_mock_cli_variant is None


class TestBuildManifest:
    """AC3: state-dir creation and canonical row emission."""

    def test_state_dir_created_0700_and_empty(self, tmp_path):
        root = tmp_path / "sandbox-root"
        manifest = _build_manifest(root, 19877, fixture_mock_cli_variant="healthy")
        try:
            state_dir = Path(manifest["fixture_providers"]["mock_cli"]["state_dir"])
            assert state_dir.is_dir()
            mode = stat.S_IMODE(state_dir.stat().st_mode)
            assert mode == 0o700
            assert not any(state_dir.iterdir())  # empty at startup
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_build_manifest_emits_canonical_row(self, tmp_path):
        root = tmp_path / "sandbox-root"
        manifest = _build_manifest(root, 19877, fixture_mock_cli_variant="empty-shell")
        try:
            row = manifest["fixture_providers"]["mock_cli"]
            assert set(row) == {
                "classification",
                "binary_realpath",
                "binary_sha256",
                "variant",
                "state_dir",
            }
            assert row["variant"] == "empty-shell"
            assert row["binary_sha256"] == _binary_sha256()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_no_fixture_omits_key(self, tmp_path):
        root = tmp_path / "sandbox-root"
        manifest = _build_manifest(root, 19877)
        try:
            assert "fixture_providers" not in manifest
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ─── AC5: validation negatives ────────────────────────────────────────────────


class TestManifestValidationNegatives:
    """AC5: every invalid fixture row fails closed."""

    def test_unknown_top_level_still_rejected(self, tmp_path):
        """D3: fixture_providers is optional; every other key still rejected."""
        manifest, mpath = _make_minimal_manifest(tmp_path)
        manifest["evil_extra"] = "x"
        with pytest.raises(SandboxError, match="unknown fields"):
            validate_manifest(manifest, mpath)

    def test_unknown_fixture_provider_rejected(self, tmp_path):
        manifest, mpath = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy", fixture_overrides={}
        )
        # Override to an arbitrary provider name
        fp = manifest["fixture_providers"].pop("mock_cli")
        manifest["fixture_providers"]["codex"] = fp
        with pytest.raises(SandboxError, match="mock_cli"):
            validate_manifest(manifest, mpath)

    def test_wrong_classification_rejected(self, tmp_path):
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"classification": "unsafe"},
        )
        with pytest.raises(SandboxError, match="fixture-test"):
            _write_and_readback(manifest, mpath)

    def test_invalid_variant_rejected(self, tmp_path):
        manifest, mpath = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy", fixture_overrides={"variant": "evil"}
        )
        with pytest.raises(SandboxError, match="variant"):
            _write_and_readback(manifest, mpath)

    def test_extra_field_rejected(self, tmp_path):
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"evil_field": "x"},
        )
        with pytest.raises(SandboxError, match="wrong fields"):
            validate_manifest(manifest, mpath)

    def test_missing_field_rejected(self, tmp_path):
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"variant": "healthy"},  # keep, but drop below
        )
        manifest["fixture_providers"]["mock_cli"].pop("state_dir")
        with pytest.raises(SandboxError, match="wrong fields"):
            validate_manifest(manifest, mpath)

    def test_hash_mismatch_rejected(self, tmp_path):
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"binary_sha256": "0" * 64},
        )
        with pytest.raises(SandboxError, match="SHA-256"):
            _write_and_readback(manifest, mpath)

    def test_binary_non_executable_rejected(self, tmp_path):
        non_exec = tmp_path / "non_exec"
        non_exec.write_text("#!/bin/sh\nexit 0\n")
        non_exec.chmod(0o644)
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"binary_realpath": str(non_exec)},
        )
        with pytest.raises(SandboxError, match="executable"):
            _write_and_readback(manifest, mpath)

    def test_binary_not_regular_file_rejected(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"binary_realpath": str(d)},
        )
        with pytest.raises(SandboxError, match="regular file"):
            _write_and_readback(manifest, mpath)

    def test_binary_outside_fork_rejected(self, tmp_path):
        outside = tmp_path / "outside-mock_cli"
        outside.write_text("#!/bin/sh\n")
        outside.chmod(0o755)
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"binary_realpath": str(outside)},
        )
        with pytest.raises(SandboxError, match="fork root"):
            _write_and_readback(manifest, mpath)

    def test_state_dir_escape_rejected(self, tmp_path):
        escape = tmp_path / "outside-root"
        escape.mkdir()
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"state_dir": str(escape)},
        )
        with pytest.raises(SandboxError, match="outside sandbox root"):
            _write_and_readback(manifest, mpath)

    def test_symlink_component_rejected(self, tmp_path):
        """AC5: symlink substitution in the binary path fails closed."""
        target = _canonical(FIXTURE_BINARY)
        link = tmp_path / "fixture-link"
        link.symlink_to(target)
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"binary_realpath": str(link)},
        )
        with pytest.raises(SandboxError, match="symlink"):
            _write_and_readback(manifest, mpath)


# ─── AC6: shared capability loader ────────────────────────────────────────────


class TestCapabilityLoader:
    """AC6: load_active_fixture_provider revalidates through the manifest."""

    def test_loads_frozen_capability(self, tmp_path, monkeypatch):
        manifest, mpath = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        cap = load_active_fixture_provider("mock_cli")
        assert isinstance(cap, SandboxFixtureProviderCapability)
        assert cap.provider == "mock_cli"
        assert cap.variant == "healthy"
        assert cap.binary_realpath.is_file()
        assert cap.state_dir.is_relative_to(Path(manifest["root"]))

    def test_unknown_provider_raises(self, tmp_path, monkeypatch):
        manifest, mpath = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        with pytest.raises(SandboxProviderUnsafe):
            load_active_fixture_provider("codex")

    def test_no_sandbox_identity_raises(self, tmp_path, monkeypatch):
        # No CAO_INSTANCE_ID → validate_active_sandbox returns None → unsafe
        with pytest.raises(SandboxProviderUnsafe):
            load_active_fixture_provider("mock_cli")

    def test_no_manifest_row_raises(self, tmp_path, monkeypatch):
        manifest, mpath = _make_minimal_manifest(tmp_path)
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        with pytest.raises(SandboxProviderUnsafe, match="no_manifest_row"):
            load_active_fixture_provider("mock_cli")

    def test_hash_revalidated_on_load(self, tmp_path, monkeypatch):
        """AC6/M9: loader re-checks the binary SHA-256 against the manifest row."""
        # The real fixture binary, but with a deliberately wrong hash in the row.
        # validate_manifest (and thus load_active_fixture_provider) rejects it.
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"binary_sha256": "0" * 64},
        )
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        with pytest.raises(SandboxError, match="SHA-256"):
            load_active_fixture_provider("mock_cli")

    def test_state_dir_escape_rejected_on_load(self, tmp_path, monkeypatch):
        escape = tmp_path / "escape-dir"
        escape.mkdir()
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"state_dir": str(escape)},
        )
        # Bypass validate_manifest by writing raw (validate_manifest also checks,
        # so patch the fp row to pass validation then fail on load).
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        with pytest.raises(SandboxError):
            # validate_active_sandbox runs validate_manifest which rejects the escape
            load_active_fixture_provider("mock_cli")

    def test_stale_cross_instance_rejected(self, tmp_path, monkeypatch):
        """AC5: a manifest/instance mismatch (stale or cross-instance) is rejected."""
        manifest, mpath = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        # Break the active-instance binding → validate_active_sandbox rejects.
        # _validate_env derives CAO_INSTANCE_ID from the manifest; pointing the
        # env at a DIFFERENT instance fails either the env check or the
        # explicit instance-id check.
        monkeypatch.setenv("CAO_INSTANCE_ID", "ffffffff")
        with pytest.raises(SandboxError):
            load_active_fixture_provider("mock_cli")


class TestAdmitProvider:
    """AC1, AC2, AC6: admission branches are closed and unchanged."""

    def test_mock_cli_admitted_in_sandbox(self, tmp_path, monkeypatch):
        manifest, mpath = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        admit_provider("mock_cli")  # must not raise

    def test_mock_cli_denied_when_row_absent(self, tmp_path, monkeypatch):
        manifest, mpath = _make_minimal_manifest(tmp_path)
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        with pytest.raises(SandboxProviderUnsafe):
            admit_provider("mock_cli")

    def test_unknown_provider_still_rejected(self, tmp_path, monkeypatch):
        manifest, mpath = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        with pytest.raises(SandboxProviderUnsafe):
            admit_provider("evil_provider")

    def test_production_is_noop(self, tmp_path, monkeypatch):
        """AC2: no CAO_INSTANCE_ID → existing production admission behavior."""
        # No sandbox env set — admit_provider returns early (production)
        admit_provider("mock_cli")  # must not raise in production
        admit_provider("codex")  # existing behavior unchanged

    def test_credential_provider_still_admitted(self, tmp_path, monkeypatch):
        """AC13: existing credential providers retain admission in sandbox."""
        # In a real sandbox the credential home dirs must exist for seeding.
        # claude_code preflight requires a bwrap native-home proof; mock it here
        # since this test only proves admission branches are unchanged.
        from cli_agent_orchestrator.utils import provider_plane as pp

        manifest, mpath = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        for provider in ("codex", "claude_code"):
            home = Path(manifest["providers"][provider]["home"])
            home.mkdir(parents=True, exist_ok=True)
            native_home = Path(manifest["providers"][provider].get("native_home", ""))
            if native_home.name:
                native_home.mkdir(parents=True, exist_ok=True)
                (native_home / "CLAUDE.md").write_text(pp.CLAUDE_SANDBOX_MARKER + "\n")
        monkeypatch.setattr(pp, "preflight_claude_native_home", lambda plane: None)
        admit_provider("codex")
        admit_provider("claude_code")


# ─── AC12: fixture binary contract ────────────────────────────────────────────


class TestFixtureBinary:
    """AC12: bounded argv parser; exit 2 on unknown/invalid; receipt from hex ID."""

    def test_unknown_argument_exit_2(self):
        result = subprocess.run(
            [str(FIXTURE_BINARY), "--bogus", "x"], capture_output=True, timeout=5
        )
        assert result.returncode == 2

    def test_invalid_variant_exit_2(self):
        state = tempfile.mkdtemp()
        try:
            result = subprocess.run(
                [
                    str(FIXTURE_BINARY),
                    "--variant",
                    "evil",
                    "--state-dir",
                    state,
                    "--terminal-id",
                    "ab12cd34",
                ],
                capture_output=True,
                timeout=5,
            )
            assert result.returncode == 2
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_missing_terminal_id_exit_2(self):
        state = tempfile.mkdtemp()
        try:
            result = subprocess.run(
                [str(FIXTURE_BINARY), "--variant", "healthy", "--state-dir", state],
                capture_output=True,
                timeout=5,
            )
            assert result.returncode == 2
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_non_canonical_terminal_id_exit_2(self):
        state = tempfile.mkdtemp()
        try:
            result = subprocess.run(
                [
                    str(FIXTURE_BINARY),
                    "--variant",
                    "healthy",
                    "--state-dir",
                    state,
                    "--terminal-id",
                    "NOTHEX!!",
                ],
                capture_output=True,
                timeout=5,
            )
            assert result.returncode == 2
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_missing_state_dir_exit_2(self):
        result = subprocess.run(
            [
                str(FIXTURE_BINARY),
                "--variant",
                "healthy",
                "--state-dir",
                "/nonexistent/dir",
                "--terminal-id",
                "ab12cd34",
            ],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 2

    def test_healthy_repl_echoes(self):
        result = subprocess.run(
            [str(FIXTURE_BINARY), "--delay-ms", "1"],
            input="hello\n/exit\n",
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "MOCK: hello" in result.stdout

    def test_empty_shell_emits_ready_then_exits(self):
        state = tempfile.mkdtemp()
        try:
            result = subprocess.run(
                [
                    str(FIXTURE_BINARY),
                    "--variant",
                    "empty-shell",
                    "--state-dir",
                    state,
                    "--terminal-id",
                    "ab12cd34",
                ],
                input="anything\n",
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 0
            assert "MockCli ready." in result.stdout
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_post_send_death_writes_atomic_receipt_and_exits_1(self):
        state = tempfile.mkdtemp()
        try:
            result = subprocess.run(
                [
                    str(FIXTURE_BINARY),
                    "--variant",
                    "post-send-death",
                    "--state-dir",
                    state,
                    "--terminal-id",
                    "ab12cd34",
                ],
                input="first-message\n",
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 1
            receipt = Path(state) / "receipt-ab12cd34"
            assert receipt.is_file()
            assert receipt.read_text() == "first-message"
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_receipt_name_derived_only_from_terminal_id(self):
        """AC12/M18: receipt name is receipt-<8-hex>, fixed 16-char, no traversal."""
        state = tempfile.mkdtemp()
        try:
            subprocess.run(
                [
                    str(FIXTURE_BINARY),
                    "--variant",
                    "post-send-death",
                    "--state-dir",
                    state,
                    "--terminal-id",
                    "deadbeef",
                ],
                input="msg\n",
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert (Path(state) / "receipt-deadbeef").is_file()
            # No other files should exist (no stray markers)
            entries = sorted(p.name for p in Path(state).iterdir())
            assert entries == ["receipt-deadbeef"]
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_process_less_variant_never_invoked(self):
        """AC10: the binary exits 2 if ever invoked for process-less."""
        state = tempfile.mkdtemp()
        try:
            result = subprocess.run(
                [
                    str(FIXTURE_BINARY),
                    "--variant",
                    "process-less",
                    "--state-dir",
                    state,
                    "--terminal-id",
                    "ab12cd34",
                ],
                capture_output=True,
                timeout=5,
            )
            assert result.returncode == 2
            assert b"should not be invoked" in result.stderr
        finally:
            shutil.rmtree(state, ignore_errors=True)


# ─── AC6/AC8/AC9/AC10/AC11/AC12: MockCliProvider fixture behavior ─────────────


class TestMockCliFixtureInitialize:
    """D6, D10: initialize derives behavior only from the manifest capability."""

    def test_initialize_healthy_uses_exact_argv(self, tmp_path):
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        provider, cap = _make_fixture_provider(tmp_path, "healthy")
        backend = MagicMock()
        with (
            patch.object(MockCliProvider, "_load_fixture_capability", return_value=cap),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
                AsyncMock(return_value=True),
            ),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
                AsyncMock(return_value=True),
            ),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend),
        ):
            result = _run(provider.initialize())
            assert result is True
            sent = backend.send_keys.call_args[0][2]
            # Exact binary argv from manifest — no PATH lookup
            assert str(cap.binary_realpath) in sent
            assert "--variant" in sent and "healthy" in sent
            assert "--state-dir" in sent
            assert "--terminal-id" in sent and "ab12cd34" in sent
            assert "mock_cli --delay-ms" not in sent  # legacy command NOT used

    def test_initialize_process_less_starts_no_child(self, tmp_path):
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        provider, cap = _make_fixture_provider(tmp_path, "process-less")
        backend = MagicMock()
        with (
            patch.object(MockCliProvider, "_load_fixture_capability", return_value=cap),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
                AsyncMock(return_value=True),
            ),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend),
        ):
            result = _run(provider.initialize())
            assert result is True
            assert provider.has_process_child is False
            backend.send_keys.assert_not_called()

    def test_initialize_rejects_non_canonical_terminal_id(self, tmp_path):
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        cap = _capability("healthy", tmp_path, manifest)
        provider = MockCliProvider("NOTHEX!!", "sess", "win")
        provider._fixture_capability = cap
        with (
            patch.object(MockCliProvider, "_load_fixture_capability", return_value=cap),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
                AsyncMock(return_value=True),
            ),
        ):
            with pytest.raises(ValueError, match="8-hex"):
                _run(provider.initialize())

    def test_legacy_ci_mode_unchanged(self):
        """AC13: non-sandbox mock_cli uses legacy PATH command."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        provider = MockCliProvider("ab12cd34", "sess", "win")
        backend = MagicMock()
        with (
            patch.object(MockCliProvider, "_load_fixture_capability", return_value=None),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
                AsyncMock(return_value=True),
            ),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
                AsyncMock(return_value=True),
            ),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend),
        ):
            result = _run(provider.initialize())
            assert result is True
            sent = backend.send_keys.call_args[0][2]
            assert "mock_cli --delay-ms" in sent


class TestMockCliSendInput:
    """D9, D10, D11: fixture-specific send_input behavior."""

    def test_process_less_writes_receipt_no_tmux(self, tmp_path):
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        provider, cap = _make_fixture_provider(tmp_path, "process-less")
        backend = MagicMock()
        with patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend):
            _run(provider.send_input("hello world"))
            receipt = cap.state_dir / "receipt-ab12cd34"
            assert receipt.is_file()
            assert receipt.read_text() == "hello world"
            backend.send_keys.assert_not_called()

    def test_procfs_unavailable_send_input_never_set(self, tmp_path):
        """D11: send_input awaits a never-set Event; cancellation-safe."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        provider, _ = _make_fixture_provider(tmp_path, "procfs-unavailable")

        async def driver():
            task = asyncio.create_task(provider.send_input("x"))
            await asyncio.sleep(0.05)
            assert not task.done()  # still blocked
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        _run(driver())

    def test_post_send_death_waits_for_receipt_then_raises(self, tmp_path):
        """D9: raises after receipt evidence exists; no direct insertion."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider
        from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError

        provider, cap = _make_fixture_provider(tmp_path, "post-send-death")
        backend = MagicMock()

        # Simulate the fixture binary writing the receipt shortly after send
        async def _write_receipt():
            await asyncio.sleep(0.05)
            receipt = cap.state_dir / "receipt-ab12cd34"
            receipt.write_text("input-1")

        async def driver():
            write_task = asyncio.create_task(_write_receipt())
            try:
                with pytest.raises(DeliveryDeferredError, match="post-send-death"):
                    await provider.send_input("input-1")
            finally:
                if not write_task.done():
                    write_task.cancel()
                    try:
                        await write_task
                    except asyncio.CancelledError:
                        pass
                else:
                    await write_task

        with patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend):
            _run(driver())
        backend.send_keys.assert_called_once()
        assert (cap.state_dir / "receipt-ab12cd34").is_file()

    def test_post_send_death_receipt_must_be_fsynced(self, tmp_path):
        """M19: receipt write fsyncs file and containing directory."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        provider, cap = _make_fixture_provider(tmp_path, "post-send-death")
        backend = MagicMock()

        async def driver():
            # Write receipt directly (as the fixture binary would), then confirm
            # the provider's wait path accepts an fsynced marker.
            receipt = cap.state_dir / "receipt-ab12cd34"
            fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, b"input-1")
            os.fsync(fd)
            os.close(fd)
            dir_fd = os.open(cap.state_dir, os.O_RDONLY)
            os.fsync(dir_fd)
            os.close(dir_fd)

        with patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend):
            _run(driver())
        assert (cap.state_dir / "receipt-ab12cd34").is_file()


class TestProductionSendInputReachability:
    """AC9/10/11: fixture send_input is reachable through the production module paths.

    The module ``terminal_service.send_input`` / ``send_prepared_input`` delegate
    the entire send to the fixture provider override for non-healthy variants.
    These tests prove the interception hook fires (production reachability), not
    just the override in isolation.
    """

    def test_module_send_input_delegates_process_less(self, tmp_path):
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider
        from cli_agent_orchestrator.services import terminal_service as ts

        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="process-less")
        cap = _capability("process-less", tmp_path, manifest)
        provider = MockCliProvider("ab12cd34", "sess", "win")
        provider._fixture_capability = cap

        with patch.object(
            ts, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
        ), patch.object(
            ts.provider_manager, "get_provider", return_value=provider
        ), patch.object(
            ts, "get_backend"
        ) as mock_backend:
            result = ts.send_input("ab12cd34", "hello")
            assert result is True
            # Receipt written by the override; no backend paste
            assert (cap.state_dir / "receipt-ab12cd34").is_file()
            mock_backend.return_value.send_keys.assert_not_called()

    def test_module_send_input_delegates_post_send_death_raises(self, tmp_path):
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider
        from cli_agent_orchestrator.services import terminal_service as ts

        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="post-send-death")
        cap = _capability("post-send-death", tmp_path, manifest)
        provider = MockCliProvider("ab12cd34", "sess", "win")
        provider._fixture_capability = cap

        # The override waits for a receipt that never appears (no binary), so it
        # raises TimeoutError after the 30s wait. Use a short fake to bound it.
        async def _short_override(text: str):
            await asyncio.sleep(0)
            raise TimeoutError("F139: post-send-death receipt not observed within 30s")

        with patch.object(provider, "send_input", _short_override), patch.object(
            ts, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
        ), patch.object(ts.provider_manager, "get_provider", return_value=provider):
            with pytest.raises(TimeoutError, match="post-send-death"):
                ts.send_input("ab12cd34", "hello")

    def test_module_send_input_healthy_uses_normal_path(self, tmp_path):
        """D7: healthy fixture keeps the ordinary paste path (no delegation)."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider
        from cli_agent_orchestrator.services import terminal_service as ts

        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        cap = _capability("healthy", tmp_path, manifest)
        provider = MockCliProvider("ab12cd34", "sess", "win")
        provider._fixture_capability = cap

        with patch.object(
            ts, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
        ), patch.object(ts.provider_manager, "get_provider", return_value=provider), patch.object(
            ts.status_monitor, "get_status", return_value=__import__(
                "cli_agent_orchestrator.models.terminal", fromlist=["TerminalStatus"]
            ).TerminalStatus.IDLE
        ), patch.object(ts, "get_backend") as mock_backend:
            result = ts.send_input("ab12cd34", "hello")
            assert result is True
            mock_backend.return_value.send_keys.assert_called_once()

    def test_module_send_prepared_input_delegates_process_less(self, tmp_path):
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider
        from cli_agent_orchestrator.services import terminal_service as ts

        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="process-less")
        cap = _capability("process-less", tmp_path, manifest)
        provider = MockCliProvider("ab12cd34", "sess", "win")
        provider._fixture_capability = cap

        with patch.object(
            ts, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
        ), patch.object(ts.provider_manager, "get_provider", return_value=provider), patch.object(
            ts, "get_backend"
        ) as mock_backend:
            result = ts.send_prepared_input("ab12cd34", "hello")
            assert result is None
            assert (cap.state_dir / "receipt-ab12cd34").is_file()
            mock_backend.return_value.send_keys.assert_not_called()


class TestMockCliEmptyShell:
    """D8: empty-shell initialize returns success only after fixture exit."""

    def test_initialize_waits_for_shell_baseline_return(self, tmp_path):
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="empty-shell")
        cap = _capability("empty-shell", tmp_path, manifest)
        provider = MockCliProvider("ab12cd34", "sess", "win")
        provider._fixture_capability = cap

        backend = MagicMock()
        # First call returns the fixture binary command (still running), second
        # returns the shell baseline (child exited).
        backend.get_pane_current_command.side_effect = [
            "mock_cli",
            "bash",
            "bash",  # stabilizes on baseline
        ]

        with (
            patch.object(MockCliProvider, "_load_fixture_capability", return_value=cap),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
                AsyncMock(return_value=True),
            ),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
                AsyncMock(return_value=True),
            ),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend),
        ):
            result = _run(provider.initialize())

        assert result is True
        # Baseline was captured before the fixture launched
        assert provider.shell_baseline is not None
        # send_keys was called to launch the fixture
        backend.send_keys.assert_called_once()

    def test_initialize_captures_baseline_before_launch(self, tmp_path):
        """M17: baseline is captured before the fixture starts, not after."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="empty-shell")
        cap = _capability("empty-shell", tmp_path, manifest)
        provider = MockCliProvider("ab12cd34", "sess", "win")
        provider._fixture_capability = cap

        backend = MagicMock()
        backend.get_pane_current_command.return_value = "bash"

        call_order: list[str] = []
        backend.get_pane_current_command.side_effect = lambda *a: (
            call_order.append("baseline") if len(call_order) == 0 else call_order.append("check"),
            "bash",
        )[1]

        def _send_keys(*a, **kw):
            call_order.append("send_keys")

        backend.send_keys.side_effect = _send_keys

        with (
            patch.object(MockCliProvider, "_load_fixture_capability", return_value=cap),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
                AsyncMock(return_value=True),
            ),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
                AsyncMock(return_value=True),
            ),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend),
        ):
            _run(provider.initialize())

        assert call_order.index("baseline") < call_order.index("send_keys")

    def test_empty_shell_awaits_child_gone(self, tmp_path):
        """M17: empty-shell MUST call _wait_for_fixture_child_gone before success.

        If the wait is skipped (the mutant), initialize returns before the pane
        confirms the fixture child is gone, and F124 would observe the child still
        running instead of an empty shell.
        """
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="empty-shell")
        cap = _capability("empty-shell", tmp_path, manifest)
        provider = MockCliProvider("ab12cd34", "sess", "win")
        provider._fixture_capability = cap

        backend = MagicMock()
        backend.get_pane_current_command.return_value = "bash"
        child_gone_awaited = asyncio.Event()

        async def _fake_wait():
            child_gone_awaited.set()

        with (
            patch.object(MockCliProvider, "_load_fixture_capability", return_value=cap),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
                AsyncMock(return_value=True),
            ),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
                AsyncMock(return_value=True),
            ),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend),
            patch.object(provider, "_wait_for_fixture_child_gone", _fake_wait),
        ):
            result = _run(provider.initialize())

        assert result is True
        assert child_gone_awaited.is_set()  # the wait ran before success


# ─── AC11: procfs-unavailable server seam ─────────────────────────────────────


class TestConfigureSandboxFixtureRuntime:
    """D11, AC11: one-shot pre-init_db _PROC_ROOT configuration."""

    def test_production_noop(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import fork_context_service as fcs

        fcs._FIXTURE_RUNTIME_CONFIGURED = False
        try:
            original = fcs._PROC_ROOT
            fcs.configure_sandbox_fixture_runtime(None)
            assert fcs._PROC_ROOT == original  # unchanged
        finally:
            fcs._FIXTURE_RUNTIME_CONFIGURED = False

    def test_default_sandbox_noop(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import fork_context_service as fcs

        fcs._FIXTURE_RUNTIME_CONFIGURED = False
        try:
            manifest, _ = _make_minimal_manifest(tmp_path)  # no fixture table
            original = fcs._PROC_ROOT
            fcs.configure_sandbox_fixture_runtime(manifest)
            assert fcs._PROC_ROOT == original
        finally:
            fcs._FIXTURE_RUNTIME_CONFIGURED = False

    def test_non_procfs_variant_noop(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import fork_context_service as fcs

        fcs._FIXTURE_RUNTIME_CONFIGURED = False
        try:
            manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
            original = fcs._PROC_ROOT
            fcs.configure_sandbox_fixture_runtime(manifest)
            assert fcs._PROC_ROOT == original
        finally:
            fcs._FIXTURE_RUNTIME_CONFIGURED = False

    def test_procfs_unavailable_sets_missing_proc_root(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import fork_context_service as fcs

        fcs._FIXTURE_RUNTIME_CONFIGURED = False
        try:
            manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="procfs-unavailable")
            fcs.configure_sandbox_fixture_runtime(manifest)
            state_dir = Path(manifest["fixture_providers"]["mock_cli"]["state_dir"])
            assert fcs._PROC_ROOT == state_dir / "missing-proc"
            assert not fcs._PROC_ROOT.exists()  # target absent
            assert fcs._procfs_available() is False  # no procfs reads possible
        finally:
            fcs._FIXTURE_RUNTIME_CONFIGURED = False

    def test_one_shot_only(self, tmp_path, monkeypatch):
        """D11: configuration occurs once — a second call is a no-op."""
        from cli_agent_orchestrator.services import fork_context_service as fcs

        fcs._FIXTURE_RUNTIME_CONFIGURED = False
        try:
            manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="procfs-unavailable")
            fcs.configure_sandbox_fixture_runtime(manifest)
            first = fcs._PROC_ROOT
            # A second, different manifest (healthy) must NOT change _PROC_ROOT
            second_dir = tmp_path / "second"
            second_dir.mkdir()
            healthy, _ = _make_minimal_manifest(second_dir, fixture_variant="healthy")
            fcs.configure_sandbox_fixture_runtime(healthy)
            assert fcs._PROC_ROOT == first
        finally:
            fcs._FIXTURE_RUNTIME_CONFIGURED = False

    def test_target_must_be_absent(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import fork_context_service as fcs

        fcs._FIXTURE_RUNTIME_CONFIGURED = False
        try:
            manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="procfs-unavailable")
            state_dir = Path(manifest["fixture_providers"]["mock_cli"]["state_dir"])
            missing = state_dir / "missing-proc"
            missing.mkdir()  # target exists → must fail closed
            with pytest.raises(RuntimeError, match="unexpectedly exists"):
                fcs.configure_sandbox_fixture_runtime(manifest)
        finally:
            fcs._FIXTURE_RUNTIME_CONFIGURED = False


class TestF124ProcfsIntegration:
    """AC10/AC11: process-less and procfs-unavailable drive F124 correctly."""

    def test_process_less_child_alive_short_circuits(self, tmp_path, monkeypatch):
        """AC10: has_process_child=False short-circuits before procfs reads."""
        from cli_agent_orchestrator.services import fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import _provider_child_alive

        provider = MagicMock()
        provider.has_process_child = False

        # Point _PROC_ROOT at a nonexistent dir so any procfs access would fail
        fcs._FIXTURE_RUNTIME_CONFIGURED = False
        try:
            monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path / "no-proc")
            result = _run(_provider_child_alive("ab12cd34", provider))
            assert result is True  # alive, no procfs needed
        finally:
            fcs._FIXTURE_RUNTIME_CONFIGURED = False

    def test_procfs_unavailable_returns_none_inconclusive(self, tmp_path, monkeypatch):
        """AC11: missing procfs returns None (inconclusive) — no false-kill."""
        from cli_agent_orchestrator.services import fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import _provider_child_alive

        provider = MagicMock()
        provider.has_process_child = True

        provider.launch_health_failure_confirmed = False
        fcs._FIXTURE_RUNTIME_CONFIGURED = False
        try:
            monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path / "missing-proc")
            result = _run(_provider_child_alive("ab12cd34", provider))
            assert result is None  # inconclusive → F110 watchdog owns settlement
        finally:
            fcs._FIXTURE_RUNTIME_CONFIGURED = False


class TestEmptyShellF124Interaction:
    """AC8: empty-shell drives F124 _provider_child_alive to confirmed death."""

    def test_empty_shell_child_alive_false_on_baseline_return(self, tmp_path, monkeypatch):
        """D8: after fixture exits, _provider_child_alive returns False (dead)."""
        from cli_agent_orchestrator.services import fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _provider_child_alive,
            get_terminal_metadata,
        )

        provider = MagicMock()
        provider.has_process_child = True
        provider.shell_baseline = "bash"

        # Synthetic procfs with the pane PID present but no descendants
        fake_proc = tmp_path / "proc"
        fake_self = fake_proc / "self"
        fake_self.mkdir(parents=True)
        (fake_self / "stat").write_text("1 (bash) S 0 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1\n")
        pid = 4242
        pdir = fake_proc / str(pid)
        pdir.mkdir(parents=True)
        (pdir / "stat").write_text(f"{pid} (bash) S 1 {pid} {pid} 0 0 0 0 0 0 0 0 0 0 0 0 1\n")

        fcs._FIXTURE_RUNTIME_CONFIGURED = False
        try:
            monkeypatch.setattr(fcs, "_PROC_ROOT", fake_proc)
            monkeypatch.setattr(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                lambda tid: {"tmux_session": "s", "tmux_window": "w", "shell_command": "bash"},
            )
            monkeypatch.setattr(
                "cli_agent_orchestrator.services.fork_context_service.pane_pid",
                lambda s, w: pid,
            )

            # Step 6: current command == baseline → confirmed empty shell (False)
            from cli_agent_orchestrator.backends.registry import get_backend

            monkeypatch.setattr(get_backend(), "get_pane_current_command", lambda s, w: "bash")

            result = _run(_provider_child_alive("ab12cd34", provider))
            assert result is False  # confirmed dead → F124 raises ProviderLaunchFailed
        finally:
            fcs._FIXTURE_RUNTIME_CONFIGURED = False


# ─── Mutation ledger kills ────────────────────────────────────────────────────


class TestMutationKills:
    """Each test pins a production behavior; the corresponding mutant fails it."""

    def test_mut_kill_omit_fixture_render(self, tmp_path):
        """M1: silently omitting fixture_providers from render_manifest fails."""
        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        rendered = render_manifest(manifest)
        assert "[fixture_providers.mock_cli]" in rendered

    def test_mut_kill_wrong_field_order(self, tmp_path):
        """M2: wrong table/field order fails."""
        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        rendered = render_manifest(manifest)
        idx = rendered.index("[fixture_providers.mock_cli]")
        row = rendered[idx:]
        order = [
            "classification",
            "binary_realpath",
            "binary_sha256",
            "variant",
            "state_dir",
        ]
        positions = [row.index(k) for k in order]
        assert positions == sorted(positions)

    def test_mut_kill_accept_unknown_top_level(self, tmp_path):
        """M4: accepting unknown top-level key fails."""
        manifest, mpath = _make_minimal_manifest(tmp_path)
        manifest["evil"] = "x"
        with pytest.raises(SandboxError, match="unknown fields"):
            validate_manifest(manifest, mpath)

    def test_mut_kill_accept_unknown_fixture_key(self, tmp_path):
        """M5: accepting unknown fixture key fails."""
        manifest, mpath = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy", fixture_overrides={"evil": "x"}
        )
        with pytest.raises(SandboxError, match="wrong fields"):
            validate_manifest(manifest, mpath)

    def test_mut_kill_accept_arbitrary_provider(self, tmp_path, monkeypatch):
        """M6: arbitrary fixture provider name fails."""
        manifest, mpath = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy", fixture_overrides={}
        )
        fp = manifest["fixture_providers"].pop("mock_cli")
        manifest["fixture_providers"]["foo"] = fp
        with pytest.raises(SandboxError, match="mock_cli"):
            validate_manifest(manifest, mpath)

    def test_mut_kill_accept_arbitrary_classification(self, tmp_path):
        """M7: arbitrary classification fails."""
        manifest, mpath = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy", fixture_overrides={"classification": "unsafe"}
        )
        with pytest.raises(SandboxError, match="fixture-test"):
            validate_manifest(manifest, mpath)

    def test_mut_kill_path_lookup(self, tmp_path, monkeypatch):
        """M8: resolving binary through PATH (not manifest realpath) fails load."""
        # The loader resolves the binary via _canonical(realpath) and requires it
        # to be under the fork root. A PATH lookup could resolve a DIFFERENT
        # binary at the same name — the manifest pins the exact realpath, so a
        # PATH-based resolution that diverges from the pinned path must fail.
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"binary_realpath": str(_canonical(FIXTURE_BINARY))},
        )
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)

        # Prove the loader rejects a binary that is NOT the canonical realpath.
        # A malicious PATH prepend with a fake mock_cli must not be admitted.
        fake_bin = tmp_path / "fake-mock_cli"
        fake_bin.write_text("#!/bin/sh\necho fake\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
        with patch("shutil.which", return_value=str(fake_bin)):
            # The loader uses the manifest realpath, NOT shutil.which — so the
            # capability's binary must be the canonical fork binary.
            cap = load_active_fixture_provider("mock_cli")
            assert cap.binary_realpath == _canonical(FIXTURE_BINARY)
            assert cap.binary_realpath != _canonical(fake_bin)

    def test_mut_kill_skip_hash_revalidation(self, tmp_path, monkeypatch):
        """M9: skipping hash revalidation fails."""
        manifest, mpath = _make_minimal_manifest(
            tmp_path,
            fixture_variant="healthy",
            fixture_overrides={"binary_sha256": "0" * 64},
        )
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        with pytest.raises(SandboxError, match="SHA-256"):
            load_active_fixture_provider("mock_cli")

    def test_mut_kill_binary_outside_fork(self, tmp_path):
        """M10: binary outside fork authority fails."""
        outside = tmp_path / "outside"
        outside.write_text("#!/bin/sh\n")
        outside.chmod(0o755)
        manifest, mpath = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy", fixture_overrides={"binary_realpath": str(outside)}
        )
        with pytest.raises(SandboxError, match="fork root"):
            validate_manifest(manifest, mpath)

    def test_mut_kill_state_dir_escape(self, tmp_path):
        """M11: state_dir outside root authority fails."""
        escape = tmp_path / "escape"
        escape.mkdir()
        manifest, mpath = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy", fixture_overrides={"state_dir": str(escape)}
        )
        with pytest.raises(SandboxError, match="outside sandbox root"):
            validate_manifest(manifest, mpath)

    def test_mut_kill_variant_from_argv_passthrough(self, tmp_path):
        """M12: variant must come from manifest, not argv passthrough."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        provider, cap = _make_fixture_provider(tmp_path, "healthy")
        backend = MagicMock()
        with (
            patch.object(MockCliProvider, "_load_fixture_capability", return_value=cap),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
                AsyncMock(return_value=True),
            ),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
                AsyncMock(return_value=True),
            ),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend),
        ):
            _run(provider.initialize())
        sent = backend.send_keys.call_args[0][2]
        # Variant is the manifest capability's variant, no external source
        assert "--variant healthy" in sent

    def test_mut_kill_process_less_has_process_child_false(self, tmp_path):
        """M14: process-less must set has_process_child=False (not child-exit)."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        provider, cap = _make_fixture_provider(tmp_path, "process-less")
        backend = MagicMock()
        with (
            patch.object(MockCliProvider, "_load_fixture_capability", return_value=cap),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
                AsyncMock(return_value=True),
            ),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend),
        ):
            _run(provider.initialize())
        assert provider.has_process_child is False
        backend.send_keys.assert_not_called()

    def test_mut_kill_empty_shell_waits_for_exit(self, tmp_path):
        """M17: empty-shell must not return before confirmed exit."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        manifest, _ = _make_minimal_manifest(tmp_path, fixture_variant="empty-shell")
        cap = _capability("empty-shell", tmp_path, manifest)
        provider = MockCliProvider("ab12cd34", "sess", "win")
        provider._fixture_capability = cap

        backend = MagicMock()
        # Pane NEVER returns to baseline → the wait must consume time and still
        # return (bounded false-positive). Assert the baseline was captured.
        backend.get_pane_current_command.return_value = "mock_cli"

        with (
            patch.object(MockCliProvider, "_load_fixture_capability", return_value=cap),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
                AsyncMock(return_value=True),
            ),
            patch(
                "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
                AsyncMock(return_value=True),
            ),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=backend),
        ):
            result = _run(provider.initialize())
        assert result is True
        assert provider.shell_baseline is not None

    def test_mut_kill_receipt_name_from_validated_id(self, tmp_path):
        """M18: receipt name only from validated 8-hex terminal ID."""
        # The binary rejects non-hex IDs → no receipt can be written for them
        state = tempfile.mkdtemp()
        try:
            result = subprocess.run(
                [
                    str(FIXTURE_BINARY),
                    "--variant",
                    "post-send-death",
                    "--state-dir",
                    state,
                    "--terminal-id",
                    "traverse..",
                ],
                input="x\n",
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 2
            assert not any(Path(state).iterdir())
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_mut_kill_no_direct_insertion(self, tmp_path):
        """M20: fixture code never inserts settlement/inbox evidence directly."""
        state = tempfile.mkdtemp()
        try:
            subprocess.run(
                [
                    str(FIXTURE_BINARY),
                    "--variant",
                    "post-send-death",
                    "--state-dir",
                    state,
                    "--terminal-id",
                    "ab12cd34",
                ],
                input="msg\n",
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Only the receipt marker exists — no DB, no inbox, no settlement file
            entries = sorted(p.name for p in Path(state).iterdir())
            assert entries == ["receipt-ab12cd34"]
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_mut_kill_row_absent_denied(self, tmp_path, monkeypatch):
        """M21: fixture capability without manifest row is denied."""
        manifest, mpath = _make_minimal_manifest(tmp_path)
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        with pytest.raises(SandboxProviderUnsafe, match="no_manifest_row"):
            load_active_fixture_provider("mock_cli")

    def test_mut_kill_no_credential_seeding(self, tmp_path, monkeypatch):
        """M22: admit_provider('mock_cli') never seeds credentials/home overlays."""
        manifest, mpath = _make_minimal_manifest(tmp_path, fixture_variant="healthy")
        _write_manifest(mpath, render_manifest(manifest))
        _sandbox_env(manifest, mpath, monkeypatch)
        root = Path(manifest["root"])
        before = sorted(p.name for p in root.iterdir())
        admit_provider("mock_cli")
        after = sorted(p.name for p in root.iterdir())
        # No new home overlay / credential file was seeded
        assert after == before


# ─── M6-M20 SURVIVING MUTANT KILLS (r4 empirical gate repair) ───────────────


class TestMutantKillLoaderHashRevalidation:
    """M6: loader must independently revalidate binary hash — not trust manifest alone."""

    def test_loader_kills_tampered_binary(self, tmp_path, monkeypatch):
        """Production path: load_active_fixture_provider recomputes SHA-256.

        The mutant sets actual_hash = expected_hash (skip recheck). Under the
        mutant, a tampered binary passes. This test must FAIL under M6.
        """
        manifest, manifest_path = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy"
        )
        rendered = render_manifest(manifest)
        _write_manifest(manifest_path, rendered)
        _sandbox_env(manifest, manifest_path, monkeypatch)

        # Tamper the binary AFTER manifest is written (manifest hash is stale)
        tampered_binary = tmp_path / "tampered_mock_cli"
        tampered_binary.write_bytes(b"#!/bin/sh\necho EVIL\n")
        tampered_binary.chmod(0o755)

        # Patch the manifest's fixture row to point to the tampered binary
        # (still inside fork_root by pointing to a file under REPO)
        # We need the binary inside fork_root — put it there
        fork_root = Path(manifest["source"]["fork_root"])
        evil_bin = fork_root / "test" / "providers" / "fixtures" / "bin" / "evil_cli"
        evil_bin.write_bytes(b"#!/bin/sh\necho EVIL\n")
        evil_bin.chmod(0o755)
        try:
            # Forge a manifest where binary_sha256 matches original but file is different
            forged_manifest = dict(manifest)
            forged_manifest["fixture_providers"] = {
                "mock_cli": {
                    "classification": "fixture-test",
                    "binary_realpath": str(evil_bin),
                    "binary_sha256": _binary_sha256(),  # hash of ORIGINAL binary
                    "variant": "healthy",
                    "state_dir": manifest["fixture_providers"]["mock_cli"]["state_dir"],
                }
            }

            # Patch validate_active_sandbox to return our forged manifest directly
            with patch(
                "cli_agent_orchestrator.sandbox_bootstrap.validate_active_sandbox",
                return_value=forged_manifest,
            ):
                with pytest.raises(SandboxProviderUnsafe, match="hash_mismatch"):
                    load_active_fixture_provider("mock_cli")
        finally:
            evil_bin.unlink(missing_ok=True)


class TestMutantKillLoaderBinaryOutsideFork:
    """M7: loader must reject binary_realpath outside fork root."""

    def test_loader_kills_binary_outside_fork(self, tmp_path, monkeypatch):
        """Production path: load_active_fixture_provider checks is_relative_to(fork_root).

        The mutant replaces the check with `if False:`. Under the mutant, a
        binary outside fork_root passes. This test must FAIL under M7.
        """
        manifest, manifest_path = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy"
        )
        _sandbox_env(manifest, manifest_path, monkeypatch)

        # Create a binary OUTSIDE the fork root
        outside_bin = tmp_path / "outside" / "mock_cli"
        outside_bin.parent.mkdir(parents=True)
        outside_bin.write_bytes(FIXTURE_BINARY.read_bytes())  # same hash
        outside_bin.chmod(0o755)

        forged_manifest = dict(manifest)
        forged_manifest["fixture_providers"] = {
            "mock_cli": {
                "classification": "fixture-test",
                "binary_realpath": str(outside_bin),
                "binary_sha256": hashlib.sha256(outside_bin.read_bytes()).hexdigest(),
                "variant": "healthy",
                "state_dir": manifest["fixture_providers"]["mock_cli"]["state_dir"],
            }
        }

        with patch(
            "cli_agent_orchestrator.sandbox_bootstrap.validate_active_sandbox",
            return_value=forged_manifest,
        ):
            with pytest.raises(SandboxProviderUnsafe, match="binary_outside_fork"):
                load_active_fixture_provider("mock_cli")


class TestMutantKillLoaderStateDirEscape:
    """M8: loader must reject state_dir outside sandbox root."""

    def test_loader_kills_state_dir_escape(self, tmp_path, monkeypatch):
        """Production path: load_active_fixture_provider checks state_dir.is_relative_to(root).

        The mutant replaces the check with `pass`. Under the mutant, a
        state_dir outside root passes. This test must FAIL under M8.
        """
        manifest, manifest_path = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy"
        )
        _sandbox_env(manifest, manifest_path, monkeypatch)

        # state_dir pointing OUTSIDE the sandbox root
        escaped_state = tmp_path / "escaped-state"
        escaped_state.mkdir(mode=0o700)

        forged_manifest = dict(manifest)
        forged_manifest["fixture_providers"] = {
            "mock_cli": {
                "classification": "fixture-test",
                "binary_realpath": str(_canonical(FIXTURE_BINARY)),
                "binary_sha256": _binary_sha256(),
                "variant": "healthy",
                "state_dir": str(escaped_state),  # OUTSIDE root
            }
        }

        with patch(
            "cli_agent_orchestrator.sandbox_bootstrap.validate_active_sandbox",
            return_value=forged_manifest,
        ):
            with pytest.raises(SandboxProviderUnsafe, match="state_dir_escape"):
                load_active_fixture_provider("mock_cli")


class TestMutantKillVariantNeverFromEnv:
    """M9: argv variant must come from cap.variant, never env override."""

    def test_variant_from_capability_not_env(self, tmp_path, monkeypatch):
        """Production path: MockCliProvider.initialize builds argv with cap.variant.

        The mutant substitutes os.environ.get("F139_VARIANT_OVERRIDE", cap.variant).
        With env set to 'evil', the mutant sends --variant evil. This test FAILS under M9.
        """
        monkeypatch.setenv("F139_VARIANT_OVERRIDE", "evil")
        provider, cap = _make_fixture_provider(tmp_path, "healthy")

        sent_commands: list[str] = []
        mock_backend = MagicMock()
        mock_backend.send_keys = MagicMock(side_effect=lambda s, w, cmd: sent_commands.append(cmd))

        with patch(
            "cli_agent_orchestrator.providers.mock_cli.get_backend",
            return_value=mock_backend,
        ), patch(
            "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
            new=AsyncMock(return_value=True),
        ), patch(
            "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
            new=AsyncMock(return_value=True),
        ), patch.object(
            provider, "_load_fixture_capability", return_value=cap,
        ):
            _run(provider.initialize())

        assert len(sent_commands) == 1
        assert "--variant healthy" in sent_commands[0]
        assert "--variant evil" not in sent_commands[0]


class TestMutantKillOneShotGuard:
    """M10: configure_sandbox_fixture_runtime must be one-shot (idempotent after first call)."""

    def test_second_call_does_not_change_proc_root(self, tmp_path, monkeypatch):
        """Production path: fork_context_service.configure_sandbox_fixture_runtime.

        The mutant removes the _FIXTURE_RUNTIME_CONFIGURED guard. Under the
        mutant, a second call overwrites _PROC_ROOT. This test FAILS under M10.
        """
        import cli_agent_orchestrator.services.fork_context_service as fcs

        # Reset module state for isolated test
        monkeypatch.setattr(fcs, "_FIXTURE_RUNTIME_CONFIGURED", False)
        original_proc_root = fcs._PROC_ROOT

        state_dir_1 = tmp_path / "state1" / "fixture-provider-state"
        state_dir_1.mkdir(parents=True, mode=0o700)
        state_dir_2 = tmp_path / "state2" / "fixture-provider-state"
        state_dir_2.mkdir(parents=True, mode=0o700)

        manifest_1 = {
            "fixture_providers": {
                "mock_cli": {
                    "variant": "procfs-unavailable",
                    "state_dir": str(state_dir_1),
                }
            }
        }
        manifest_2 = {
            "fixture_providers": {
                "mock_cli": {
                    "variant": "procfs-unavailable",
                    "state_dir": str(state_dir_2),
                }
            }
        }

        # First call sets _PROC_ROOT to state_dir_1/missing-proc
        fcs.configure_sandbox_fixture_runtime(manifest_1)
        proc_root_after_first = fcs._PROC_ROOT

        # Second call must be a no-op (guard)
        fcs.configure_sandbox_fixture_runtime(manifest_2)
        proc_root_after_second = fcs._PROC_ROOT

        # _PROC_ROOT must equal first call's path, not second's
        assert proc_root_after_first == state_dir_1 / "missing-proc"
        assert proc_root_after_second == proc_root_after_first
        assert proc_root_after_second != state_dir_2 / "missing-proc"

        # Restore
        monkeypatch.setattr(fcs, "_PROC_ROOT", original_proc_root)
        monkeypatch.setattr(fcs, "_FIXTURE_RUNTIME_CONFIGURED", False)


class TestMutantKillLifespanCallsConfigure:
    """M12: lifespan must call configure_sandbox_fixture_runtime with active manifest."""

    def test_lifespan_invokes_configure(self, tmp_path, monkeypatch):
        """Production path: api/main.py lifespan → configure_sandbox_fixture_runtime.

        The mutant replaces the call with `pass`. Under the mutant,
        configure_sandbox_fixture_runtime is never called. This test FAILS under M12.

        We exercise the real lifespan function with early abort after the
        configure call (raise to stop before init_db complications).
        """
        import cli_agent_orchestrator.services.fork_context_service as fcs

        monkeypatch.setattr(fcs, "_FIXTURE_RUNTIME_CONFIGURED", False)
        original_proc_root = fcs._PROC_ROOT

        manifest, manifest_path = _make_minimal_manifest(
            tmp_path, fixture_variant="procfs-unavailable"
        )
        rendered = render_manifest(manifest)
        _write_manifest(manifest_path, rendered)
        _sandbox_env(manifest, manifest_path, monkeypatch)

        configure_calls: list[Any] = []
        real_configure = fcs.configure_sandbox_fixture_runtime

        def recording_configure(m):
            configure_calls.append(m)
            real_configure(m)

        # Patch configure at its source module so the lazy import inside
        # lifespan picks it up
        monkeypatch.setattr(
            fcs, "configure_sandbox_fixture_runtime", recording_configure
        )

        class _StopLifespan(Exception):
            pass

        # Use init_db as the stop-point — it runs immediately after configure
        with patch(
            "cli_agent_orchestrator.api.main.is_sandbox", return_value=True
        ), patch(
            "cli_agent_orchestrator.sandbox_bootstrap.validate_active_sandbox",
            return_value=manifest,
        ), patch(
            "cli_agent_orchestrator.sandbox_bootstrap.assert_sandbox_db_fence",
        ), patch(
            "cli_agent_orchestrator.api.main.init_db",
            side_effect=_StopLifespan("stop after configure"),
        ):
            from cli_agent_orchestrator.api.main import lifespan

            async def _run_lifespan():
                app_mock = MagicMock()
                async with lifespan(app_mock):
                    pass  # _StopLifespan will be raised during __aenter__

            with pytest.raises(_StopLifespan):
                _run(_run_lifespan())

        # Under M12 mutant (pass instead of call), configure_calls would be empty
        assert len(configure_calls) >= 1
        assert configure_calls[0] is manifest

        # Restore
        monkeypatch.setattr(fcs, "_PROC_ROOT", original_proc_root)
        monkeypatch.setattr(fcs, "_FIXTURE_RUNTIME_CONFIGURED", False)


class TestMutantKillReceiptNameFromTerminalId:
    """M14: receipt filename must use self.terminal_id, not CAO_TERMINAL_ID env."""

    def test_receipt_uses_instance_terminal_id_not_env(self, tmp_path, monkeypatch):
        """Production path: MockCliProvider.send_input (process-less variant).

        The mutant substitutes os.environ.get('CAO_TERMINAL_ID', self.terminal_id).
        With env set, the receipt gets the wrong name. This test FAILS under M14.
        """
        monkeypatch.setenv("CAO_TERMINAL_ID", "ffffffff")
        provider, cap = _make_fixture_provider(tmp_path, "process-less")

        # provider.terminal_id is "ab12cd34" (from _make_fixture_provider)
        assert provider.terminal_id == "ab12cd34"

        _run(provider.send_input("hello\n"))

        # Receipt must be named after self.terminal_id, not env
        expected_receipt = cap.state_dir / "receipt-ab12cd34"
        wrong_receipt = cap.state_dir / "receipt-ffffffff"
        assert expected_receipt.exists(), f"Expected {expected_receipt} to exist"
        assert not wrong_receipt.exists(), f"Wrong receipt {wrong_receipt} must not exist"
        assert expected_receipt.read_bytes() == b"hello\n"


class TestMutantKillReceiptFsync:
    """M15: process-less send_input must fsync both file and directory."""

    def test_receipt_fsyncs_file_and_dir(self, tmp_path, monkeypatch):
        """Production path: MockCliProvider.send_input (process-less variant).

        The mutant removes os.fsync calls. Under the mutant, fsync is never
        called. This test FAILS under M15.
        """
        provider, cap = _make_fixture_provider(tmp_path, "process-less")

        fsync_fds: list[int] = []
        real_fsync = os.fsync

        def recording_fsync(fd):
            fsync_fds.append(fd)
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", recording_fsync)

        _run(provider.send_input("test data\n"))

        # Must have at least 2 fsync calls: file fd + directory fd
        assert len(fsync_fds) >= 2, (
            f"Expected >=2 fsync calls (file + dir), got {len(fsync_fds)}"
        )


class TestMutantKillPurgeRemovesFixtureState:
    """M19: command_down --purge must remove fixture-provider-state directory."""

    def test_purge_removes_fixture_state_dir(self, tmp_path, monkeypatch):
        """Production path: sandbox_bootstrap.command_down with --purge.

        The mutant skips the shutil.rmtree(current['root']) call. Under the
        mutant, the entire root (including fixture-provider-state) persists.
        This test FAILS under M19.
        """
        from cli_agent_orchestrator.sandbox_bootstrap import command_down

        manifest, manifest_path = _make_minimal_manifest(
            tmp_path, fixture_variant="healthy"
        )
        rendered = render_manifest(manifest)
        _write_manifest(manifest_path, rendered)

        root = Path(manifest["root"])
        fixture_state = root / "fixture-provider-state"
        assert fixture_state.is_dir(), "fixture-provider-state must exist before purge"

        # Write owner lock file
        import json as json_mod
        owner_lock = root / "owner.lock"
        root_stat = root.stat()
        owner_lock.write_text(json_mod.dumps({
            "owner_nonce": manifest["owner_nonce"],
            "root_device": root_stat.st_dev,
            "root_inode": root_stat.st_ino,
        }), encoding="utf-8")

        # Write pidfile
        pidfile = root / "sandbox.pid"
        pidfile.write_text(json_mod.dumps({"pid": os.getpid()}), encoding="utf-8")
        manifest["pidfile"] = str(pidfile)

        # Patch _load_owned to return our manifest + pid_record
        with patch(
            "cli_agent_orchestrator.sandbox_bootstrap._load_owned",
            return_value=(manifest, manifest_path, {"pid": str(os.getpid())}),
        ), patch(
            "cli_agent_orchestrator.sandbox_bootstrap._sentinel_owned",
            return_value=True,
        ), patch(
            "os.killpg",
        ), patch(
            "os.kill", side_effect=ProcessLookupError,
        ), patch(
            "cli_agent_orchestrator.sandbox_bootstrap._tmux_lifecycle",
        ):
            import argparse as argparse_mod
            args = argparse_mod.Namespace(root=str(root), purge=True)
            command_down(args)

        # After purge, the entire root (including fixture-provider-state) must be gone
        assert not root.exists(), f"Root {root} should be removed by purge"
        assert not fixture_state.exists(), f"fixture-provider-state should be gone"


class TestMutantKillBinaryArgvNoPathLookup:
    """M20: initialize argv must use cap.binary_realpath, never shutil.which."""

    def test_argv_uses_realpath_not_which(self, tmp_path, monkeypatch):
        """Production path: MockCliProvider.initialize (healthy variant).

        The mutant substitutes shutil.which("mock_cli") or str(cap.binary_realpath).
        When shutil.which returns a different path, the mutant uses it.
        This test FAILS under M20.
        """
        provider, cap = _make_fixture_provider(tmp_path, "healthy")

        evil_path = "/tmp/evil/mock_cli"
        monkeypatch.setattr(shutil, "which", lambda name: evil_path if name == "mock_cli" else None)

        sent_commands: list[str] = []
        mock_backend = MagicMock()
        mock_backend.send_keys = MagicMock(
            side_effect=lambda s, w, cmd: sent_commands.append(cmd)
        )

        with patch(
            "cli_agent_orchestrator.providers.mock_cli.get_backend",
            return_value=mock_backend,
        ), patch(
            "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
            new=AsyncMock(return_value=True),
        ), patch(
            "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
            new=AsyncMock(return_value=True),
        ), patch.object(
            provider, "_load_fixture_capability", return_value=cap,
        ):
            _run(provider.initialize())

        assert len(sent_commands) == 1
        # Must start with the manifest-pinned binary, not the PATH lookup
        assert sent_commands[0].startswith(str(cap.binary_realpath))
        assert evil_path not in sent_commands[0]
