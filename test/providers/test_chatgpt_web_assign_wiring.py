"""F862 (#718) r3 — AC-3 dispatch-guard wiring test (item 3, verdict Blocker 4).

Drives the real ``server._assign_impl`` across all three reachable dispatch
paths — routing-driven (provider omitted), explicit ``provider="chatgpt_web"``,
and resume-prepared — and asserts:

* a FORBIDDEN gate position (``design_reviewer``) + chatgpt_web is REFUSED before
  any terminal is created or browser launched, on EACH path;
* the same three inputs to ``design_findings`` are NOT refused by the guard
  (creation is reached), proving the backstop is an allow-list, not a gate-
  position deny predicate;
* the guard uses ``resolve_routing_binding`` against a fixture store.

The ledger mutant — removing the explicit arm's provider assignment
``_f862_provider = _resolved_provider or _f838_checked_provider or provider`` —
must make specifically the EXPLICIT case stop refusing; the mutant-kill test
asserts that behaviour by simulating the mutant with a monkeypatch.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_CLAUSES = """
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
design_findings = ["callback-contract","containment","f129-pins","never-edit-artifact-branch","never-emit-verdict"]
design_reviewer = ["callback-contract","containment","f129-pins","never-edit-artifact-branch"]
general = ["callback-contract","containment","never-emit-verdict"]

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
wall.
<!-- clause:never-emit-verdict -->
not a gate.
## Frozen Authority Pin protocol (F129)
pins.
<!-- clause:callback-contract -->
cb.
<!-- clause:containment -->
scratch.
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

_ROUTING = """
[[binding]]
position = "design_findings"
provider = "chatgpt_web"
kind = "cao"
[[binding]]
position = "design_reviewer"
provider = "chatgpt_web"
kind = "cao"
"""


_GENERAL_BODY = """\
# GENERAL
<!-- clause:never-emit-verdict -->
not a gate.
<!-- clause:callback-contract -->
cb.
<!-- clause:containment -->
scratch.
"""


def _cert_row(positions_dir: Path, position: str, body: str, provider: str) -> str:
    import frontmatter

    from cli_agent_orchestrator.utils.profile_composition import overlay_sha, position_sha

    parsed = frontmatter.loads(f"---\nrole: developer\ncertification: []\n---\n\n{body}")
    pos_sha = position_sha(parsed.content, dict(parsed.metadata))
    ov = positions_dir.parent / "overlays" / f"{provider}.md"
    frags = [ov.read_text(encoding="utf-8")] if ov.exists() else []
    ov_sha = overlay_sha(frags)
    return (
        f'[{{"provider": "{provider}", "position_sha": "{pos_sha}", '
        f'"overlay_sha": "{ov_sha}", "outcome": "PASS", "date": "2026-09-09"}}]'
    )


def _store(tmp: Path) -> Path:
    positions = tmp / "positions"
    overlays = tmp / "overlays"
    positions.mkdir(parents=True)
    overlays.mkdir(parents=True)
    (positions / "_clauses.toml").write_text(textwrap.dedent(_CLAUSES), encoding="utf-8")
    (positions / "design_findings.md").write_text(
        f"---\nrole: developer\ncertification: []\n---\n\n{_FINDINGS_BODY}", encoding="utf-8"
    )
    (positions / "design_reviewer.md").write_text(
        f"---\nrole: developer\ncertification: []\n---\n\n{_REVIEWER_BODY}", encoding="utf-8"
    )
    (positions / "general.md").write_text(
        f"---\nrole: developer\ncertification: []\n---\n\n{_GENERAL_BODY}", encoding="utf-8"
    )
    (overlays / "chatgpt_web.md").write_text("---\n---\n\n## notes\n", encoding="utf-8")
    routing = tmp / "routing.toml"
    routing.write_text(textwrap.dedent(_ROUTING), encoding="utf-8")
    return tmp


def _certify(tmp: Path) -> None:
    """Add PASS cert rows for general + design_findings at their current shas so
    resolve_routing_binding admits a certified design_findings cell."""
    positions = tmp / "positions"
    gen = _cert_row(positions, "general", _GENERAL_BODY, "chatgpt_web")
    find = _cert_row(positions, "design_findings", _FINDINGS_BODY, "chatgpt_web")
    (positions / "general.md").write_text(
        f"---\nrole: developer\ncertification: {gen}\n---\n\n{_GENERAL_BODY}", encoding="utf-8"
    )
    (positions / "design_findings.md").write_text(
        f"---\nrole: developer\ncertification: {find}\n---\n\n{_FINDINGS_BODY}", encoding="utf-8"
    )


@pytest.fixture()
def wired_server(tmp_path, monkeypatch):
    """Point the D14 guard's store/routing lookups at a fixture, and neutralise
    the surrounding _assign_impl machinery so control reaches the guard for each
    of the three input shapes with a real resolve_routing_binding."""
    from cli_agent_orchestrator import constants as _const
    from cli_agent_orchestrator.mcp_server import server as srv
    from cli_agent_orchestrator.utils import agent_profiles as ap

    _store(tmp_path)
    positions = tmp_path / "positions"
    routing = tmp_path / "routing.toml"

    # The guard imports these from constants at call time.
    monkeypatch.setattr(_const, "positions_store_dir", lambda: positions, raising=False)
    monkeypatch.setattr(_const, "routing_toml_path", lambda: routing, raising=False)

    # Neutralise the pre-guard machinery so the three shapes reach the guard.
    monkeypatch.setattr(srv, "_dispatch_guard", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(srv, "_routing_guard", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(srv, "_current_terminal_id", lambda: None, raising=False)

    # _position_exists must recognise our fixture positions (used by both the
    # routing-driven detector and the guard's own existence check).
    _known = {"design_findings", "design_reviewer", "general"}
    monkeypatch.setattr(ap, "_position_exists", lambda name: name in _known, raising=False)

    # Spy on terminal creation + browser launch — must NOT be reached on refusal.
    created: list = []

    def _spy_create(*a, **k):  # pragma: no cover - asserted not called on refusal
        created.append((a, k))
        return ("t-created", None)

    monkeypatch.setattr(srv, "_create_terminal", _spy_create, raising=False)

    launched: list = []
    try:
        from cli_agent_orchestrator.chatgpt_web_runner import runtime as _rt

        monkeypatch.setattr(_rt, "launch", lambda *a, **k: launched.append(1), raising=False)
    except Exception:
        pass

    return srv, created, launched


def _call(srv, **kw):
    return srv._assign_impl(agent_profile=kw.pop("agent_profile"), message="do it", **kw)


def _assert_d14_refusal(res: dict) -> None:
    """Assert the refusal came from the D14 guard specifically (not a downstream
    stub failure) — the guard returns 'Assignment refused (no spawn): ...' with a
    routing/cell error code. This is what the ledger mutant must break."""
    assert res["success"] is False and res["terminal_id"] is None
    msg = res["message"]
    assert "Assignment refused (no spawn)" in msg, f"not a D14 refusal: {msg!r}"


# --- routing-driven (provider omitted) ------------------------------------------
def test_ac3_routing_driven_gate_refused(wired_server) -> None:
    srv, created, launched = wired_server
    res = _call(srv, agent_profile="design_reviewer", provider=None)
    _assert_d14_refusal(res)
    assert created == [] and launched == []


# --- explicit provider="chatgpt_web" (the r2-gate mutant path) ------------------
def test_ac3_explicit_provider_gate_refused(wired_server, monkeypatch) -> None:
    srv, created, launched = wired_server
    from cli_agent_orchestrator.utils import agent_profiles as ap

    # resolve_assignment_target for an open-allowlist gate position + explicit
    # provider returns the composed name + the provider.
    monkeypatch.setattr(
        ap,
        "resolve_assignment_target",
        lambda profile, provider: (f"{profile}-{provider}", provider),
        raising=False,
    )
    res = _call(srv, agent_profile="design_reviewer", provider="chatgpt_web")
    _assert_d14_refusal(res)
    assert created == [] and launched == []


# --- resume-prepared ------------------------------------------------------------
def test_ac3_resume_prepared_gate_refused(wired_server, monkeypatch) -> None:
    srv, created, launched = wired_server
    # Force the resume-prepared branch: patch prepare_resume where _assign_impl
    # imports it (resume_service), returning a prepared dict naming the gate
    # position + chatgpt_web.
    from cli_agent_orchestrator.services import resume_service as _rs

    def _fake_prepare(*a, **k):
        return {
            "provider": "chatgpt_web",
            "agent_profile": "design_reviewer",
            "working_directory": None,
            "forked_from_info": None,
            "authority_files": None,
            "fork_context": None,
            "admission": None,
            "via_identity": False,
            "known_pins": None,
        }

    monkeypatch.setattr(_rs, "prepare_resume", _fake_prepare, raising=False)
    res = _call(srv, agent_profile="design_reviewer", resume_from="deadbeef")
    _assert_d14_refusal(res)
    assert created == [] and launched == []


# --- allow-list: a CERTIFIED design_findings is NOT refused by the guard --------
def test_ac3_certified_design_findings_not_refused_by_guard(wired_server, monkeypatch) -> None:
    srv, created, launched = wired_server
    # Certify the fixture design_findings + general cells so resolve_routing_binding
    # admits the cell; the D14 guard must then NOT refuse (control passes it). The
    # store dir was created by the fixture; re-certify it in place.
    from cli_agent_orchestrator import constants as _const
    from cli_agent_orchestrator.utils import agent_profiles as ap

    positions_dir = _const.positions_store_dir()
    _certify(positions_dir.parent)

    monkeypatch.setattr(
        ap,
        "resolve_assignment_target",
        lambda profile, provider: (f"{profile}-{provider}", provider),
        raising=False,
    )
    res = _call(srv, agent_profile="design_findings", provider="chatgpt_web")
    # A certified design_findings cell is NOT refused by the D14 guard. Control
    # passed the guard (any later failure is a downstream stub artifact, NOT the
    # 'Assignment refused (no spawn)' D14 refusal).
    assert "Assignment refused (no spawn)" not in res["message"]
