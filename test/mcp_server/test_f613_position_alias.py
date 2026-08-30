"""F613 #469 — fork hotfix: general-fallback alias resolution + provider threading.

Two bugs closed:

  1. ``utils/routing.py`` non-gate general fallback returned the RAW
     ``f"{provider}_general"`` (``cline_cli_general``) instead of the INSTALLED
     alias stub stem (``cline_general``). The raw name is not an installed
     profile; the server fails to load it and silently re-derives to claude_code.
     Fix: resolve the stub via ``_find_alias_for_cell(general, provider)`` and
     raise ``RoutingError(E-ALIAS-MISSING)`` when no stub exists.

  2. ``mcp_server/server.py`` ``_assign_impl`` computed ``_resolved_provider``
     but did not pass it to ``_create_terminal``, which re-derived the provider
     via ``resolve_provider(..., fallback=supervisor provider)`` → claude_code
     when the (position/alias) name failed to load. Fix: thread
     ``provider=_resolved_provider`` through; when given it wins, when absent the
     behaviour is byte-identical.
"""

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# --------------------------------------------------------------------------
# Bug 1 — general alias resolution per provider
# --------------------------------------------------------------------------

# provider id -> installed alias stub stem (the F613 mapping the bug got wrong)
_PROVIDER_TO_ALIAS = {
    "cline_cli": "cline_general",
    "kiro_cli": "kiro_general",
    "grok_cli": "grok_general",
    "codex": "codex_general",
    "claude_code": "claude_general",
}


def _seed_general_stub(store: Path, stem: str, provider: str) -> None:
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{stem}.md").write_text(
        textwrap.dedent(f"""\
            ---
            extends: general
            name: {stem}
            provider: {provider}
            ---
            # {stem}
            """),
        encoding="utf-8",
    )


@pytest.mark.parametrize(("provider", "expected_alias"), sorted(_PROVIDER_TO_ALIAS.items()))
def test_general_fallback_resolves_installed_alias_stub(
    tmp_path, monkeypatch, provider, expected_alias
):
    """The non-gate general fallback binds the INSTALLED alias stub stem
    (``<short>_general``), never the raw ``<provider>_general`` f-string."""
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.utils import agent_profiles

    store = tmp_path / "agent-store"
    _seed_general_stub(store, expected_alias, provider)
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    # _find_alias_for_cell reads local_agent_store_dir() (CAO_HOME_DIR/agent-store).
    assert constants.local_agent_store_dir() == store
    alias = agent_profiles._find_alias_for_cell("general", provider)
    assert alias == expected_alias
    # For cline_cli / kiro_cli / grok_cli the raw f-string DIVERGES from the stub
    # stem — the exact bug. Assert the resolver did NOT return the raw name.
    if provider not in ("codex",):  # codex raw == stem coincidentally
        assert alias != f"{provider}_general"


def test_general_fallback_unknown_provider_is_none(tmp_path, monkeypatch):
    """A provider with no installed general stub resolves to None (the caller
    raises E-ALIAS-MISSING)."""
    from cli_agent_orchestrator.utils import agent_profiles

    (tmp_path / "agent-store").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    assert agent_profiles._find_alias_for_cell("general", "nonesuch_cli") is None


# --------------------------------------------------------------------------
# Bug 1 — resolve_routing_binding end to end (fallback binds the stub; unknown
# provider raises E-ALIAS-MISSING). Reuses the D9 harness helpers.
# --------------------------------------------------------------------------


def _routing_fallback_env(tmp_path, monkeypatch, provider, alias_stem, *, seed_stub):
    """Build a positions store with a non-gate 'dev' cell that is UNCERTIFIED so
    resolve_routing_binding takes the general-fallback path; optionally seed the
    (general, provider) alias stub in the flat store."""
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
    if seed_stub:
        _seed_general_stub(tmp_path / "agent-store", alias_stem, provider)
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    return positions


def test_routing_binding_fallback_binds_alias_stub_for_cline(tmp_path, monkeypatch):
    """cline_cli: the general fallback binds ``cline_general`` (stub), NOT
    ``cline_cli_general`` (the pre-F613 defect)."""
    from cli_agent_orchestrator.utils import routing

    positions = _routing_fallback_env(
        tmp_path, monkeypatch, "cline_cli", "cline_general", seed_stub=True
    )
    table = routing.bindings_to_table(
        [routing.Binding(position="dev", provider="cline_cli", kind="cao")]
    )
    res = routing.resolve_routing_binding("dev", "cline_cli", table=table, positions_dir=positions)
    assert res.spawn_profile == "cline_general"
    assert res.fallback_profile == "cline_general"
    assert res.spawn_profile != "cline_cli_general"


def test_routing_binding_fallback_missing_stub_raises_e_alias_missing(tmp_path, monkeypatch):
    """No installed general stub for the provider → RoutingError E-ALIAS-MISSING
    (never hand the unresolved f-string to the server)."""
    from cli_agent_orchestrator.utils import routing

    positions = _routing_fallback_env(
        tmp_path, monkeypatch, "cline_cli", "cline_general", seed_stub=False
    )
    table = routing.bindings_to_table(
        [routing.Binding(position="dev", provider="cline_cli", kind="cao")]
    )
    with pytest.raises(routing.RoutingError) as ei:
        routing.resolve_routing_binding("dev", "cline_cli", table=table, positions_dir=positions)
    assert ei.value.code == routing.E_ALIAS_MISSING
    assert ei.value.code == "E-ALIAS-MISSING"


# --------------------------------------------------------------------------
# Bug 2 — _assign_impl threads _resolved_provider into _create_terminal, and the
# HTTP terminal-create call carries provider=cline_cli.
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
    _seed_general_stub(home / "agent-store", "cline_general", "cline_cli")

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
