"""F838 (#695) — assign seam refuses (no spawn) on provider substitution.

A legacy alias stub (``pi_cli_empirical_reviewer_lite``) that declares a provider
but resolves to no valid provider must NOT spawn as the caller's provider. The
assign seam pre-resolves the provider strictly from the stub and, on failure or
mismatch, returns a typed refusal WITHOUT calling ``_create_terminal``.

r3 (codex EMPIRICAL-GATE-NO r2 Blocker B): the r2 guard spanned TWO mutable store
reads — once for intent (``_stub_declared_provider_safe``) and again inside
``resolve_provider`` — so an empty-provider stub that DISAPPEARED between them was
reclassified ABSENT on the second read and fell back to the caller's provider,
which was then carried into creation. The r3 guard reads+classifies EXACTLY ONCE
(``_classify_stub_intent``) and resolves from that SAME immutable classification
(``_resolve_provider_from_classification``). These tests exercise the guard
through REAL on-disk store files (the reviewer's probe made permanent) rather
than preprogramming the resolver to raise — the verdict's explicit requirement
for the disappearance case.
"""

from __future__ import annotations

import shutil
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch as _patch

import pytest

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.mcp_server.server import _assign_impl
from cli_agent_orchestrator.utils import agent_profiles as ap
from cli_agent_orchestrator.utils.agent_profiles import E_PROVIDER_UNRESOLVED

_CREATE = "cli_agent_orchestrator.mcp_server.server._create_terminal"


def patch_object(target, attr, value):
    return _patch.object(target, attr, value)


def patch_dotted(path, **kw):
    return _patch(path, **kw)


class _CallerResponse:
    """Minimal cao_http.get response for the current-terminal metadata lookup:
    the caller is a ``claude_code`` supervisor — the provider a substitution
    would leak into the spawn."""

    status_code = 200

    @staticmethod
    def json() -> dict[str, object]:
        return {
            "provider": "claude_code",
            "session_name": "scratch-session",
            "allowed_tools": None,
        }

    @staticmethod
    def raise_for_status() -> None:
        return None


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A real scratch agent-store dir wired into the profile lookup, with every
    other configured agent dir disabled so ONLY the files we write can match.
    ``_create_terminal`` and the caller-metadata HTTP GET are stubbed so the
    guard runs against real files but no real spawn/HTTP happens."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    store_dir = tmp_path / "agent-store"
    store_dir.mkdir(parents=True, exist_ok=True)
    stack = ExitStack()
    stack.enter_context(patch_object(ap, "LOCAL_AGENT_STORE_DIR", store_dir))
    stack.enter_context(
        patch_dotted(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            return_value={},
        )
    )
    stack.enter_context(
        patch_dotted(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            return_value=[],
        )
    )
    stack.enter_context(
        patch_dotted(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
            return_value=[],
        )
    )
    stack.enter_context(_patch.object(server.cao_http, "get", return_value=_CallerResponse()))
    stack.enter_context(
        patch_dotted(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value=None,
        )
    )
    try:
        yield store_dir
    finally:
        stack.close()
        if store_dir.exists():
            shutil.rmtree(store_dir, ignore_errors=True)


def _write(store_dir: Path, name: str, raw: str) -> Path:
    path = store_dir / f"{name}.md"
    path.write_text(raw, encoding="utf-8", newline="")
    return path


def test_assign_refuses_when_declared_stub_resolves_unresolved(store):
    """A real declaring stub (``provider:`` empty) that resolves to no valid
    provider → typed refusal, NO terminal created. Uses the REAL resolver."""
    _write(store, "f838_alias", "---\nprovider:\n---\nbody\n")
    with _patch(_CREATE) as create:
        result = _assign_impl("f838_alias", "task", working_directory="/repo")
    assert result["success"] is False
    assert E_PROVIDER_UNRESOLVED in result["message"]
    assert "no spawn" in result["message"].lower()
    create.assert_not_called()


def test_assign_refuses_on_provider_mismatch(store):
    """A real declaring stub names ``pi_cli`` but the composed profile resolves
    to a DIFFERENT valid provider (the silent-substitution shape) → typed
    refusal, no spawn. Force the mismatch by composing to claude_code while the
    stub declared pi_cli."""
    _write(store, "f838_alias", "---\nprovider: pi_cli\n---\nbody\n")
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    with (
        _patch(
            "cli_agent_orchestrator.utils.agent_profiles.resolve_agent_profile",
            return_value=AgentProfile(name="f838_alias", description="d", provider="claude_code"),
        ),
        _patch(_CREATE) as create,
    ):
        result = _assign_impl("f838_alias", "task", working_directory="/repo")
    assert result["success"] is False
    assert E_PROVIDER_UNRESOLVED in result["message"]
    assert "claude_code" in result["message"] and "pi_cli" in result["message"]
    create.assert_not_called()


def test_assign_validates_declared_provider_on_success(store):
    """Healthy declaring stub resolving to pi_cli → seam validates and the spawn
    proceeds; the guard-checked provider (pi_cli) is CARRIED into
    _create_terminal (never a second re-resolution)."""
    _write(store, "f838_alias", "---\nprovider: pi_cli\n---\nbody\n")
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    captured: dict[str, object] = {}

    def fake_create(agent_profile, *a, **k):
        captured["provider"] = k.get("provider")
        captured["agent_profile"] = agent_profile
        return ("worker1", "pi_cli")

    with (
        _patch(
            "cli_agent_orchestrator.utils.agent_profiles.resolve_agent_profile",
            return_value=AgentProfile(name="f838_alias", description="d", provider="pi_cli"),
        ),
        _patch(_CREATE, side_effect=fake_create) as create,
    ):
        result = _assign_impl("f838_alias", "task", working_directory="/repo")
    assert result["success"] is True
    create.assert_called_once()
    assert captured["agent_profile"] == "f838_alias"
    assert captured["provider"] == "pi_cli"


def test_assign_carries_guard_checked_provider_across_store_replacement(store):
    """codex probe VERBATIM (real files): a healthy ``pi_cli`` validation, then
    the store file is REPLACED so a SECOND resolution would yield claude_code.
    The provider PASSED TO _create_terminal must equal the guard-checked value
    (pi_cli) and MUST NEVER become claude_code — proving creation does not
    re-resolve from the mutated store. Counts raw store reads to pin the
    single-read invariant."""
    path = _write(store, "f838_alias", "---\nprovider: pi_cli\n---\nbody\n")
    original_read = ap.read_agent_profile_source
    raw_reads = {"n": 0}
    captured: dict[str, object] = {}

    def read_then_replace(profile_name: str) -> str:
        raw = original_read(profile_name)
        raw_reads["n"] += 1
        # After the guard's single classify read, mutate the on-disk stub so any
        # LATER read at create time would see claude_code.
        if raw_reads["n"] == 1:
            path.write_text("---\nprovider: claude_code\n---\nbody\n", encoding="utf-8")
        return raw

    def fake_create(agent_profile, *a, **k):
        captured["provider"] = k.get("provider")
        captured["disk_now"] = path.read_text(encoding="utf-8").splitlines()[1]
        return ("worker1", "pi_cli")

    with (
        _patch.object(ap, "read_agent_profile_source", side_effect=read_then_replace),
        _patch(_CREATE, side_effect=fake_create),
    ):
        result = _assign_impl("f838_alias", "task", working_directory="/repo")

    assert result["success"] is True
    # The decisive assertion: guard_checked pi_cli reaches create as pi_cli even
    # though the disk now says claude_code.
    assert captured["provider"] == "pi_cli"
    assert captured["provider"] != "claude_code"
    assert captured["disk_now"] == "provider: claude_code"


def test_assign_refuses_when_empty_provider_disappears_between_guard_reads(store):
    """codex r2 Blocker B, REAL resolver (verdict: "the disappearance test must
    run the real resolver rather than preprogramming it to raise"): a
    ``provider:``-empty stub is renamed away right after the guard's FIRST raw
    read. In r2 the guard read a SECOND time inside resolve_provider, saw the
    gone file as ABSENT, and fell back to the caller's claude_code — which
    reached _create_terminal (success=True). In r3 the guard classifies ONCE and
    resolves from those same bytes (DECLARED, empty provider → refuse), so a
    mid-flight disappearance cannot flip the verdict: typed refusal, NO spawn."""
    path = _write(store, "f838_race_empty", "---\nprovider:\n---\nbody\n")
    moved = path.with_suffix(".moved")
    original_read = ap.read_agent_profile_source
    raw_reads = {"n": 0}

    def read_then_remove(profile_name: str) -> str:
        raw = original_read(profile_name)
        raw_reads["n"] += 1
        # Remove the file immediately after the FIRST successful read.
        if raw_reads["n"] == 1 and path.exists():
            path.rename(moved)
        return raw

    with (
        _patch.object(ap, "read_agent_profile_source", side_effect=read_then_remove),
        _patch(_CREATE) as create,
    ):
        result = _assign_impl("f838_race_empty", "task", working_directory="/repo")

    assert result["success"] is False, result
    assert E_PROVIDER_UNRESOLVED in result["message"]
    assert "no spawn" in result["message"].lower()
    create.assert_not_called()
    # Single-read invariant: the guard read the raw store EXACTLY ONCE. If it had
    # read twice, the second read would have hit the removed file (ABSENT) and
    # fallen back — the r2 defect.
    assert raw_reads["n"] == 1, f"guard must read once; read {raw_reads['n']} times"


def test_assign_refuses_on_unreadable_dangling_symlink(store):
    """codex r2 Blocker A at the public seam (real file): a DANGLING SYMLINK
    store entry is UNKNOWN (present-but-unreadable), routed through the
    fail-closed resolver → typed refusal, no spawn. In r2 this classified ABSENT
    and fell back to claude_code."""
    (store / "f838_dangling.md").symlink_to(store / "missing-target.md")
    with _patch(_CREATE) as create:
        result = _assign_impl("f838_dangling", "task", working_directory="/repo")
    assert result["success"] is False
    assert E_PROVIDER_UNRESOLVED in result["message"]
    create.assert_not_called()


def test_assign_refuses_on_truncated_and_bom_stubs(store):
    """codex r2 Blocker A at the public seam (real files): a TRUNCATED stub
    (opener, no closing delimiter) and a BOM-prefixed stub both classify UNKNOWN
    and refuse — in r2 both were PLAIN and fell back to claude_code."""
    _write(store, "f838_partial", "---\nprovider: pi_cli\n")
    _write(store, "f838_bom", "\ufeff---\nprovider: pi_cli\n---\nbody\n")
    for name in ("f838_partial", "f838_bom"):
        with _patch(_CREATE) as create:
            result = _assign_impl(name, "task", working_directory="/repo")
        assert result["success"] is False, name
        assert E_PROVIDER_UNRESOLVED in result["message"], name
        create.assert_not_called()


def test_assign_legacy_plain_no_intent_unchanged(store):
    """A genuine legacy plain profile (declares no provider/composition intent)
    is NOT pre-resolved by the F838 seam — passthrough to _create_terminal
    unchanged (provider stays None; _create_terminal's own resolve handles it)."""
    _write(store, "f838_plain", "---\ndescription: plain legacy\n---\nbody\n")
    captured: dict[str, object] = {}

    def fake_create(agent_profile, *a, **k):
        captured["provider"] = k.get("provider")
        captured["agent_profile"] = agent_profile
        return ("worker1", "kiro_cli")

    with _patch(_CREATE, side_effect=fake_create) as create:
        result = _assign_impl("f838_plain", "task", working_directory="/repo")
    assert result["success"] is True
    create.assert_called_once()
    assert captured["agent_profile"] == "f838_plain"
    # Not pre-resolved by the guard; legacy passthrough leaves provider None.
    assert captured["provider"] is None
