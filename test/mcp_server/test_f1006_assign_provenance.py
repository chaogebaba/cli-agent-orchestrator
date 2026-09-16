"""F1006 #854 — the MCP ``assign`` shim declares WHERE the composed name came from.

``assign`` resolves a bare position into ``<position>-<provider>`` client-side
and then POSTs that composed literal, so by the time the create request reaches
the server the routing-driven shape is gone and a shape-only derivation reads
EXPLICIT. These tests pin the shim half of the fix: the routing-driven arm
declares its origin position, the explicit-override arm declares nothing, and
``_create_terminal`` puts the declaration on the wire.

Pure over a fixture store; ``_create_terminal`` / the HTTP client are patched.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.utils.profile_composition import overlay_sha, position_sha

_CLAUSES_TOML = """\
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"

[required]
general = ["callback-contract", "containment"]
dev = ["callback-contract", "containment"]

[budget]
general = 2500
dev = 6000
overlay = 1200
composed_slack = 500
"""

_GENERAL_BODY = "# GENERAL\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
_DEV_BODY = (
    '---\nproviders: ["kiro_cli"]\n---\n# DEV - coding worker\n'
    "<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _build_store(home: Path) -> Path:
    positions = home / "agent-store" / "positions"
    overlays = home / "agent-store" / "overlays"
    _write(positions / "_clauses.toml", _CLAUSES_TOML)
    _write(positions / "general.md", _GENERAL_BODY)
    _write(positions / "dev.md", _DEV_BODY)
    _write(overlays / "kiro_cli.md", "## notes (kiro_cli)\n")
    return positions


def _certify(positions: Path, position: str, provider: str, outcome: str) -> None:
    import frontmatter

    parsed = frontmatter.loads((positions / f"{position}.md").read_text(encoding="utf-8"))
    p_sha = position_sha(parsed.content, dict(parsed.metadata))
    overlays = positions.parent / "overlays"
    frags = [
        (overlays / n).read_text(encoding="utf-8")
        for n in (f"{provider}.md", f"{provider}.{position}.md")
        if (overlays / n).exists()
    ]
    rows = list(parsed.metadata.get("certification") or [])
    rows.append(
        {
            "provider": provider,
            "position_sha": p_sha,
            "overlay_sha": overlay_sha(frags),
            "outcome": outcome,
            "date": "2026-09-16",
        }
    )
    parsed.metadata["certification"] = rows
    (positions / f"{position}.md").write_text(frontmatter.dumps(parsed) + "\n", encoding="utf-8")


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    home = tmp_path / "cao-home"
    positions = _build_store(home)
    _certify(positions, "general", "kiro_cli", "PASS")
    rt = tmp_path / "routing.toml"
    _write(
        rt,
        """\
        [[binding]]
        position = "dev"
        provider = "kiro_cli"
        kind = "cao"
        """,
    )
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    return positions


def _run_assign(*args, **kwargs):
    from cli_agent_orchestrator.mcp_server.server import _assign_impl

    captured = {}

    def fake_create(*a, **k):
        captured.update(k)
        return ("worker_x", "kiro_cli")

    with patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ):
        result = _assign_impl(*args, **kwargs)
    return result, captured


def test_routing_driven_assign_declares_its_origin_position(wired):
    """MUTANT KILLER: a bare position with no provider= is routing-driven; the
    shim must name the position it composed ``dev-kiro_cli`` FROM. Dropping the
    declaration reinstates the 403 (the server reads the composed shape)."""
    result, captured = _run_assign("dev", "task", working_directory="/repo")
    assert result["success"] is True
    assert captured["cell_request_class"] == "routing"
    assert captured["cell_request_origin"] == "dev"


def test_explicit_provider_override_declares_no_provenance(wired):
    """An operator-supplied provider= is the operator's own cell choice: EXPLICIT,
    no provenance. Declaring one here would hand the routing concession to an
    explicit request."""
    _certify(wired, "dev", "kiro_cli", "PASS")  # explicit requires a certified cell
    result, captured = _run_assign("dev", "task", provider="kiro_cli", working_directory="/repo")
    assert result["success"] is True
    assert captured["cell_request_class"] == "explicit"
    assert captured["cell_request_origin"] is None


def test_legacy_name_declares_no_provenance(wired):
    result, captured = _run_assign("my_legacy_worker", "task", working_directory="/repo")
    assert result["success"] is True
    assert captured["cell_request_class"] == "legacy"
    assert captured["cell_request_origin"] is None


def test_create_terminal_puts_the_declaration_on_the_wire(monkeypatch):
    """MUTANT KILLER: ``_create_terminal`` must send ``cell_request_origin`` as a
    query param on the existing-session create, next to the class. Keeping it
    local to the shim leaves the server deriving from the composed shape."""
    from cli_agent_orchestrator.mcp_server import server as srv

    meta = MagicMock()
    meta.json.return_value = {
        "provider": "kiro_cli",
        "session_name": "cao-f1006",
        "allowed_tools": None,
    }
    meta.raise_for_status.return_value = None
    created = MagicMock()
    created.json.return_value = {"id": "worker-1", "provider": "kiro_cli"}
    created.raise_for_status.return_value = None

    http = MagicMock()
    http.get.return_value = meta
    http.post.return_value = created
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
    with (
        patch.object(srv, "cao_http", http),
        patch.object(srv, "_resolve_child_allowed_tools", return_value=None),
        patch.object(srv, "_diagnose_own_404", return_value=None),
    ):
        srv._create_terminal(
            "dev-kiro_cli",
            "/repo",
            provider="kiro_cli",
            cell_request_class="routing",
            cell_request_origin="dev",
        )
    params = http.post.call_args.kwargs["params"]
    assert params["cell_request_class"] == "routing"
    assert params["cell_request_origin"] == "dev"


def test_create_terminal_omits_the_param_when_there_is_no_provenance(monkeypatch):
    """No declaration → no query param at all, so an explicit/legacy create is
    byte-identical to its pre-F1006 shape."""
    from cli_agent_orchestrator.mcp_server import server as srv

    meta = MagicMock()
    meta.json.return_value = {
        "provider": "kiro_cli",
        "session_name": "cao-f1006",
        "allowed_tools": None,
    }
    meta.raise_for_status.return_value = None
    created = MagicMock()
    created.json.return_value = {"id": "worker-1", "provider": "kiro_cli"}
    created.raise_for_status.return_value = None

    http = MagicMock()
    http.get.return_value = meta
    http.post.return_value = created
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
    with (
        patch.object(srv, "cao_http", http),
        patch.object(srv, "_resolve_child_allowed_tools", return_value=None),
        patch.object(srv, "_diagnose_own_404", return_value=None),
    ):
        srv._create_terminal("reviewer", "/repo", provider="kiro_cli")
    assert "cell_request_origin" not in http.post.call_args.kwargs["params"]
