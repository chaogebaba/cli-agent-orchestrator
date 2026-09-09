"""F862 (#718) — dispatch refusal, certification, and provider tests.

AC-3 (gate positions refused), AC-16/AC-17 (certification + clause rows +
_is_gate_position + certified-cell succeeds despite the gate predicate), and the
provider-side D14 backstop + status parsing.

The routing tests build a self-contained temp position store (positions +
overlays + clause table) so they never depend on the installed store's mutable
certification state, then drive ``resolve_routing_binding`` and
``_is_gate_position`` directly — the SAME refusal machinery the server guard
reaches (D14).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.chatgpt_web import (
    CHATGPT_WEB_ALLOWED_POSITIONS,
    ChatGptWebProvider,
    _resolve_position_name,
)
from cli_agent_orchestrator.utils.routing import (
    RoutingError,
    RoutingTable,
    _is_gate_position,
    cell_certified,
    resolve_routing_binding,
)

pytestmark = pytest.mark.unit

_CLAUSES_TOML = """
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"
[clauses.f129-pins]
heading = "## Frozen Authority Pin protocol (F129)"
[clauses.never-edit-artifact-branch]
marker = "<!-- clause:never-edit-artifact-branch -->"
[clauses.never-emit-verdict]
marker = "<!-- clause:never-emit-verdict -->"

[required]
design_findings = ["callback-contract", "containment", "f129-pins", "never-edit-artifact-branch", "never-emit-verdict"]
design_reviewer = ["callback-contract", "containment", "f129-pins", "never-edit-artifact-branch"]
general = ["callback-contract", "containment", "never-emit-verdict"]

[budget]
design_findings = 4000
design_reviewer = 8000
general = 2500
overlay = 1200
composed_slack = 500
"""

_FINDINGS_BODY = """\
# DESIGN FINDINGS

<!-- clause:never-edit-artifact-branch -->
Hard wall.

<!-- clause:never-emit-verdict -->
Not a gate.

## Frozen Authority Pin protocol (F129)
verify_pin cadence.

<!-- clause:callback-contract -->
send_message when done.

<!-- clause:containment -->
No /tmp.
"""

_GENERAL_BODY = """\
# GENERAL
<!-- clause:never-emit-verdict -->
Not a gate.
<!-- clause:callback-contract -->
send_message.
<!-- clause:containment -->
No /tmp.
"""

_REVIEWER_BODY = """\
# DESIGN REVIEWER
<!-- clause:never-edit-artifact-branch -->
wall.
## Frozen Authority Pin protocol (F129)
pins.
<!-- clause:callback-contract -->
cb.
<!-- clause:containment -->
scratch.
"""


def _write_store(tmp: Path, *, findings_cert: str = "[]", general_cert: str = "[]") -> Path:
    positions = tmp / "positions"
    overlays = tmp / "overlays"
    positions.mkdir(parents=True)
    overlays.mkdir(parents=True)
    (positions / "_clauses.toml").write_text(textwrap.dedent(_CLAUSES_TOML), encoding="utf-8")

    def _pos(name: str, body: str, cert: str) -> None:
        (positions / f"{name}.md").write_text(
            f"---\nrole: developer\ncertification: {cert}\n---\n\n{body}",
            encoding="utf-8",
        )

    _pos("design_findings", _FINDINGS_BODY, findings_cert)
    _pos("design_reviewer", _REVIEWER_BODY, "[]")
    _pos("general", _GENERAL_BODY, general_cert)
    # A minimal provider overlay so the composed body is well-formed.
    (overlays / "chatgpt_web.md").write_text("---\n---\n\n## notes\n", encoding="utf-8")
    return positions


def _empty_table() -> RoutingTable:
    return RoutingTable(bindings=[])


# ==========================================================================
# AC-17 — _is_gate_position is TRUE for design_findings (refusal predicate)
# ==========================================================================
def test_ac17_design_findings_is_gate_position(tmp_path: Path) -> None:
    positions = _write_store(tmp_path)
    assert _is_gate_position("design_findings", positions, None) is True


# ==========================================================================
# AC-17 — an uncertified design_findings cell is REFUSED, never substituted
# ==========================================================================
def test_ac17_uncertified_findings_cell_refused_not_substituted(tmp_path: Path) -> None:
    # general cell IS certified (so the provider passes step 1); the findings
    # cell is NOT — a gate cell refuses instead of falling back to general.
    positions = _write_store(
        tmp_path,
        general_cert=_cert_rows(
            positions_dir_hint=tmp_path, position="general", provider="chatgpt_web"
        ),
    )
    with pytest.raises(RoutingError) as ei:
        resolve_routing_binding(
            "design_findings", "chatgpt_web", table=_empty_table(), positions_dir=positions
        )
    # Refusal, not a general substitution.
    assert "not" in str(ei.value).lower() or ei.value.code


def test_ac17_missing_clause_row_refused(tmp_path: Path) -> None:
    positions = _write_store(tmp_path)
    # Remove the design_findings [required] row -> missing-row refusal.
    clause_path = positions / "_clauses.toml"
    text = clause_path.read_text(encoding="utf-8").replace(
        'design_findings = ["callback-contract", "containment", "f129-pins", "never-edit-artifact-branch", "never-emit-verdict"]\n',
        "",
    )
    clause_path.write_text(text, encoding="utf-8")
    with pytest.raises(RoutingError):
        resolve_routing_binding(
            "design_findings", "chatgpt_web", table=_empty_table(), positions_dir=positions
        )


# ==========================================================================
# AC-3 — a gate position + chatgpt_web is refused; design_findings is NOT
# (both via the same resolve_routing_binding the server guard calls)
# ==========================================================================
@pytest.mark.parametrize("gate_pos", ["design_reviewer"])
def test_ac3_gate_position_refused_for_chatgpt_web(tmp_path: Path, gate_pos: str) -> None:
    positions = _write_store(
        tmp_path,
        general_cert=_cert_rows(tmp_path, "general", "chatgpt_web"),
    )
    with pytest.raises(RoutingError):
        resolve_routing_binding(
            gate_pos, "chatgpt_web", table=_empty_table(), positions_dir=positions
        )


# --------------------------------------------------------------------------
# Helper: compute a PASS certification row for a cell at its current shas.
# --------------------------------------------------------------------------
def _cert_rows(positions_dir_hint: Path, position: str, provider: str) -> str:
    """Return a YAML-ish inline list with a PASS row at the cell's current shas.

    We can't know the shas before writing the file, so this is a two-phase
    trick: write the store once with empty cert, compute the shas, then the
    caller re-points. To keep the tests simple we instead compute against a
    freshly written store here.
    """
    import frontmatter

    from cli_agent_orchestrator.utils.profile_composition import overlay_sha, position_sha

    positions = positions_dir_hint / "positions"
    body_map = {"general": _GENERAL_BODY, "design_findings": _FINDINGS_BODY}
    # Write a throwaway store to compute shas deterministically.
    parsed = frontmatter.loads(
        f"---\nrole: developer\ncertification: []\n---\n\n{body_map[position]}"
    )
    pos_sha = position_sha(parsed.content, dict(parsed.metadata))
    ov = positions.parent / "overlays" / f"{provider}.md"
    frags = [ov.read_text(encoding="utf-8")] if ov.exists() else []
    ov_sha = overlay_sha(frags)
    return (
        f'[{{"provider": "{provider}", "position_sha": "{pos_sha}", '
        f'"overlay_sha": "{ov_sha}", "outcome": "PASS", "date": "2026-09-09"}}]'
    )


def test_ac16_certified_general_and_findings_cells_resolve(tmp_path: Path) -> None:
    # Write store, compute both cells' shas, re-write with PASS rows, then a
    # certified design_findings dispatch SUCCEEDS despite _is_gate_position True.
    positions = _write_store(tmp_path)
    gen_cert = _cert_rows(tmp_path, "general", "chatgpt_web")
    find_cert = _cert_rows(tmp_path, "design_findings", "chatgpt_web")
    # Re-write the two position files with their PASS rows.
    (positions / "general.md").write_text(
        f"---\nrole: developer\ncertification: {gen_cert}\n---\n\n{_GENERAL_BODY}", encoding="utf-8"
    )
    (positions / "design_findings.md").write_text(
        f"---\nrole: developer\ncertification: {find_cert}\n---\n\n{_FINDINGS_BODY}",
        encoding="utf-8",
    )
    gen_pass, _ = cell_certified("general", "chatgpt_web", positions)
    find_pass, _ = cell_certified("design_findings", "chatgpt_web", positions)
    assert gen_pass is True
    assert find_pass is True
    # A certified design_findings dispatch resolves despite the gate predicate.
    res = resolve_routing_binding(
        "design_findings", "chatgpt_web", table=_empty_table(), positions_dir=positions
    )
    assert res.provider == "chatgpt_web"
    assert res.fallback_profile is None  # not a general substitution
    # AC-17: _is_gate_position stays True for the position even when certified.
    assert _is_gate_position("design_findings", positions, None) is True


# ==========================================================================
# Provider-side D14 backstop + status parsing
# ==========================================================================
def _provider(agent_profile: str) -> ChatGptWebProvider:
    return ChatGptWebProvider("t1", "sess", "win", agent_profile=agent_profile)


def test_backstop_refuses_gate_position() -> None:
    p = _provider("design_reviewer")
    with pytest.raises(PermissionError):
        p._assert_position_allowed()


def test_backstop_refuses_gate_position_composed_name() -> None:
    p = _provider("design_reviewer-chatgpt_web")
    with pytest.raises(PermissionError):
        p._assert_position_allowed()


def test_backstop_allows_design_findings_and_general() -> None:
    for name in (
        "design_findings",
        "general",
        "design_findings-chatgpt_web",
        "general-chatgpt_web",
    ):
        _provider(name)._assert_position_allowed()  # no raise


def test_resolve_position_name_strips_suffix() -> None:
    assert _resolve_position_name("design_findings-chatgpt_web") == "design_findings"
    assert _resolve_position_name("general") == "general"
    assert _resolve_position_name(None) == ""


def test_allowed_positions_set() -> None:
    assert CHATGPT_WEB_ALLOWED_POSITIONS == frozenset({"design_findings", "general"})


def test_status_markers_parse() -> None:
    p = _provider("design_findings")
    p._initialized = True
    p._task_dispatched = True
    assert p.get_status("[chatgpt_web] RUNNING bundle") is TerminalStatus.PROCESSING
    assert p.get_status("[chatgpt_web] FINDINGS-READY /path") is TerminalStatus.COMPLETED
    assert p.get_status("[chatgpt_web] ERROR ui_changed") is TerminalStatus.ERROR
    assert p.get_status("[chatgpt_web] WAIT_USER bot_flagged") is TerminalStatus.WAITING_USER_ANSWER
    assert p.get_status("nothing recognizable") is TerminalStatus.UNKNOWN


def test_provider_no_fork_no_resume() -> None:
    p = _provider("design_findings")
    assert p.declared_capabilities["fork"] is False
    assert p.declared_capabilities["resume"] is False
    assert p.supports_fork_context is False
    assert p.supports_resume is False


def test_extract_last_message_raises() -> None:
    p = _provider("design_findings")
    with pytest.raises(ValueError):
        p.extract_last_message_from_script("anything")


def test_newest_user_msg_id_picks_latest_user_node() -> None:
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import _newest_user_msg_id

    conv = {
        "mapping": {
            "u1": {"message": {"id": "u1", "author": {"role": "user"}, "create_time": 10.0}},
            "a1": {"message": {"id": "a1", "author": {"role": "assistant"}, "create_time": 11.0}},
            "u2": {"message": {"id": "u2", "author": {"role": "user"}, "create_time": 20.0}},
        }
    }
    assert _newest_user_msg_id(conv) == "u2"
    assert _newest_user_msg_id({"mapping": {}}) == ""
    assert _newest_user_msg_id({}) == ""
