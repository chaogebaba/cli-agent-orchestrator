"""F613 #469 + F786 D11 + F870 #726 — non-gate cell composition + provider threading.

F613's original fix resolved the non-gate general fallback through a flat-store
alias-stub scan (``_find_alias_for_cell``) and raised ``E-ALIAS-MISSING`` when no
stub existed. F786 D11 replaced that scan with a pure ``general-<provider>``
derivation. F870 #726 then DELETES the cross-position general substitution
entirely: a non-PASS non-gate cell now binds the position's OWN
``<position>-<provider>`` composition (``uncertified_cell=True``), never
``general-<provider>``. The provider's ``general`` PASS row (checked first) still
gates the provider. The Bug-2 provider-threading behaviour (``_assign_impl``
passes ``_resolved_provider`` to ``_create_terminal``) is unchanged and still
covered below.
"""

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# --------------------------------------------------------------------------
# F786 D11 — the general fallback derives general-<provider> (no stub scan)
# --------------------------------------------------------------------------
def _routing_fallback_env(tmp_path, monkeypatch, provider):
    """Build a positions store with a non-gate 'dev' cell that is UNCERTIFIED so
    resolve_routing_binding takes the general-fallback path (D11 derivation)."""
    from test.mcp_server.test_f497_routing_d9 import _CLAUSES_TOML, _GENERAL_BODY, _certify, _write

    positions = tmp_path / "agent-store" / "positions"
    overlays = tmp_path / "agent-store" / "overlays"
    _write(
        positions / "_clauses.toml",
        _CLAUSES_TOML.replace("[budget]", 'dev = ["callback-contract", "containment"]\n\n[budget]')
        + "dev = 6000\n",
    )
    _write(positions / "general.md", _GENERAL_BODY)
    _write(
        positions / "dev.md",
        "# DEV\nwork.\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n",
    )
    _write(overlays / f"{provider}.md", f"## Provider notes ({provider})\nq.\n")
    _certify(positions, "general", provider, "PASS")
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    return positions


@pytest.mark.parametrize("provider", ["cline_cli", "kiro_cli", "grok_cli", "codex"])
def test_non_gate_uncertified_uses_own_composition(tmp_path, monkeypatch, provider):
    """F870 #726 — a non-PASS NON-gate cell binds the position's OWN composed
    name ``<position>-<provider>`` (here ``dev-<provider>``). F786 D11's
    cross-position ``general-<provider>`` substitution is DELETED: a non-gate
    uncertified cell never silently runs as ``general``. ``uncertified_cell`` is
    set and ``fallback_profile`` names the SAME-position composition."""
    from cli_agent_orchestrator.utils import routing

    positions = _routing_fallback_env(tmp_path, monkeypatch, provider)
    table = routing.bindings_to_table(
        [routing.Binding(position="dev", provider=provider, kind="cao")]
    )
    res = routing.resolve_routing_binding("dev", provider, table=table, positions_dir=positions)
    assert res.spawn_profile == f"dev-{provider}"
    assert res.spawn_profile != f"general-{provider}"
    assert res.fallback_profile == f"dev-{provider}"
    assert res.uncertified_cell is True
    assert res.spawn_profile != f"{provider}_general"


def test_find_alias_for_cell_and_alias_missing_are_deleted():
    """D11 removes the stub-scan helper; E-ALIAS-MISSING is never raised now."""
    from cli_agent_orchestrator.utils import agent_profiles, routing

    assert not hasattr(agent_profiles, "_find_alias_for_cell")
    # The constant is kept (stable code) but the fallback path no longer raises it.
    assert routing.E_ALIAS_MISSING == "E-ALIAS-MISSING"


# --------------------------------------------------------------------------
# Bug 2 — _assign_impl threads _resolved_provider into _create_terminal, and the
# HTTP terminal-create call carries provider=cline_cli. (Unchanged by F786.)
# --------------------------------------------------------------------------


def test_assign_impl_threads_resolved_provider_to_create_terminal(tmp_path, monkeypatch):
    """A secretary-style position assign resolves provider=cline_cli and passes
    it to _create_terminal (mock), so the server never re-derives to claude_code."""
    from test.mcp_server.test_f497_routing_d9 import _CLAUSES_TOML, _GENERAL_BODY, _certify, _write

    home = tmp_path / "cao-home"
    positions = home / "agent-store" / "positions"
    overlays = home / "agent-store" / "overlays"
    # 'secretary' position bound to cline_cli, CERTIFIED (so it binds directly).
    _write(
        positions / "_clauses.toml",
        _CLAUSES_TOML.replace(
            "[budget]",
            'secretary = ["callback-contract", "containment"]\n\n[budget]',
        )
        + "secretary = 6000\n",
    )
    _write(positions / "general.md", _GENERAL_BODY)
    _write(
        positions / "secretary.md",
        "# SECRETARY\n---\nproviders: [cline_cli]\n---\nwork.\n"
        "<!-- clause:callback-contract -->\n<!-- clause:containment -->\n",
    )
    _write(overlays / "cline_cli.md", "## Provider notes (cline_cli)\nq.\n")
    _certify(positions, "general", "cline_cli", "PASS")
    _certify(positions, "secretary", "cline_cli", "PASS")

    rt = tmp_path / "routing.toml"
    _write(
        rt,
        """\
        [[binding]]
        position = "secretary"
        provider = "cline_cli"
        kind = "cao"
        """,
    )
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

    from cli_agent_orchestrator.mcp_server.server import _assign_impl

    captured = {}

    def fake_create(agent_profile, working_directory, *a, **k):
        captured["agent_profile"] = agent_profile
        captured["provider"] = k.get("provider")
        return ("worker_x", "cline_cli")

    with patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ):
        result = _assign_impl("secretary", "task", working_directory="/repo")

    assert result["success"] is True, result
    # The resolved provider (cline_cli) was threaded to _create_terminal.
    assert captured["provider"] == "cline_cli"
    # D2b: the effective spawn name is the composed <position>-<provider>.
    assert captured["agent_profile"] == "secretary-cline_cli"


def test_create_terminal_supplied_provider_wins_and_reaches_http(monkeypatch):
    """_create_terminal(provider='cline_cli') must place provider=cline_cli in
    the terminal-create HTTP params, NOT re-derive to the supervisor provider."""
    from cli_agent_orchestrator.mcp_server import server

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

    # Supervisor terminal metadata says claude_code — the pre-F613 re-derivation
    # would pick THIS up as the fallback. The supplied provider must win.
    meta = MagicMock()
    meta.json.return_value = {
        "provider": "claude_code",
        "session_name": "sess",
        "allowed_tools": "",
    }
    meta.raise_for_status.return_value = None
    created = MagicMock()
    created.json.return_value = {"id": "worker_y"}
    created.raise_for_status.return_value = None

    with (
        patch.object(server, "cao_http") as http,
        patch.object(server, "_diagnose_own_404", return_value=""),
        patch.object(server, "resolve_provider", return_value="claude_code") as rp,
        patch.object(server, "_resolve_child_allowed_tools", return_value=""),
    ):
        http.get.return_value = meta
        http.post.return_value = created
        tid, prov = server._create_terminal(
            "cline_general",
            "/repo",
            provider="cline_cli",
        )

    assert tid == "worker_y"
    assert prov == "cline_cli"
    # resolve_provider must NOT have driven the decision (supplied provider wins).
    rp.assert_not_called()
    post_params = http.post.call_args.kwargs["params"]
    assert post_params["provider"] == "cline_cli"


def test_create_terminal_without_provider_is_byte_identical(monkeypatch):
    """Absent provider → re-derive via resolve_provider exactly as before."""
    from cli_agent_orchestrator.mcp_server import server

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    meta = MagicMock()
    meta.json.return_value = {
        "provider": "claude_code",
        "session_name": "sess",
        "allowed_tools": "",
    }
    meta.raise_for_status.return_value = None
    created = MagicMock()
    created.json.return_value = {"id": "worker_z"}
    created.raise_for_status.return_value = None

    with (
        patch.object(server, "cao_http") as http,
        patch.object(server, "_diagnose_own_404", return_value=""),
        patch.object(server, "resolve_provider", return_value="codex") as rp,
        patch.object(server, "_resolve_child_allowed_tools", return_value=""),
    ):
        http.get.return_value = meta
        http.post.return_value = created
        tid, prov = server._create_terminal("codex_dev", "/repo")

    assert prov == "codex"
    rp.assert_called_once()  # re-derivation ran (byte-identical path)
    assert http.post.call_args.kwargs["params"]["provider"] == "codex"
