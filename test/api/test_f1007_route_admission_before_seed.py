"""F1007 #855 — the cold create ROUTE admits the cell BEFORE the seed exec.

``POST /sessions/{s}/terminals`` owns the reported defect: on the cold branch
(no ``resume_from``, no client ``fork_context``) it called
``terminal_service.seed_resume_bootstrap`` — a real provider execution — and only
then derived the cell class and reached the D4 choke point inside
``create_terminal``. An uncertified cell therefore spawned a provider, and when
that provider's credential was dead its ``seed_exec_failed rc=1`` surfaced as a
**500** in place of the certification refusal.

The route now derives the class ahead of the seed and hands it to the seed, which
runs the guard itself. Asserted here: the typed 400, zero provider execs, zero
create — and the admitted arm still seeding exactly once. The service-level arms,
the order pin and the two mutants live in
``test/services/test_f1007_seed_admission_order.py``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import Terminal
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

_BODY = "# POSITION\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _certify(positions: Path, position: str, provider: str, outcome: str) -> None:
    import frontmatter

    parsed = frontmatter.loads((positions / f"{position}.md").read_text(encoding="utf-8"))
    p_sha = position_sha(parsed.content, dict(parsed.metadata))
    overlays = positions.parent / "overlays"
    frags = [
        f.read_text(encoding="utf-8")
        for f in (overlays / f"{provider}.md", overlays / f"{provider}.{position}.md")
        if f.exists()
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
def store(tmp_path, monkeypatch):
    """``general`` certified for the seed-capable provider, ``dev`` not. Only the
    position store and routing table are redirected (CAO_HOME_DIR stays put, as
    in the F1006 route tests, so the route's own DB/session machinery is
    untouched)."""
    home = tmp_path / "cao-home"
    positions = home / "agent-store" / "positions"
    overlays = home / "agent-store" / "overlays"
    _write(positions / "_clauses.toml", _CLAUSES_TOML)
    _write(positions / "general.md", _BODY)
    _write(positions / "dev.md", _BODY)
    _write(overlays / "codex.md", "## notes (codex)\n")
    _certify(positions, "general", "codex", "PASS")
    rt = tmp_path / "routing.toml"
    _write(
        rt,
        """\
        [[binding]]
        position = "dev"
        provider = "codex"
        kind = "cao"
        [[binding]]
        position = "general"
        provider = "codex"
        kind = "cao"
        """,
    )
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    monkeypatch.setattr("cli_agent_orchestrator.constants.positions_store_dir", lambda: positions)
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles._position_exists",
        lambda name: name in {"dev", "general"},
    )
    return positions


class _SpawnLedger:
    def __init__(self) -> None:
        self.execs: list[tuple[str, str]] = []


def _fake_provider(led: _SpawnLedger, *, fail: bool):
    class _FakeSeedProvider:
        supports_seed_resume_identity = True

        @staticmethod
        def seed_resume_identity(cwd: str, agent_profile: str) -> str:
            led.execs.append((agent_profile, cwd))
            if fail:
                raise RuntimeError("seed_exec_failed rc=1: Failed to refresh token")
            return "11111111-2222-3333-4444-555555555555"

    return _FakeSeedProvider


@pytest.fixture()
def broken_credential(monkeypatch):
    """A seed-capable provider whose exec fails the way a dead credential does —
    the incident's own shape. Nothing real runs, so a lost guard is a RECORDED
    spawn rather than a subprocess."""
    led = _SpawnLedger()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_provider_class",
        lambda _name: _fake_provider(led, fail=True),
    )
    return led


@pytest.fixture()
def working_credential(monkeypatch):
    led = _SpawnLedger()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_provider_class",
        lambda _name: _fake_provider(led, fail=False),
    )
    return led


def _terminal() -> Terminal:
    return Terminal(
        id="abcd1234",
        name="general-codex-abcd1234",
        session_name="cao-f1007",
        provider="codex",
        agent_profile="general-codex",
    )


def test_route_refuses_uncertified_cell_with_zero_provider_execs(client, store, broken_credential):
    """THE REPORTED 500, now a certification refusal: an uncertified cell whose
    provider credential is broken renders the typed 400 with ZERO execs and ZERO
    create. Before F1007 the seed ran first and its failure masked the refusal."""
    create = AsyncMock(return_value=_terminal())
    with patch("cli_agent_orchestrator.api.main.terminal_service.create_terminal", create):
        resp = client.post(
            "/sessions/cao-f1007/terminals",
            params={
                "provider": "codex",
                "agent_profile": "dev-codex",
                "cell_request_class": "explicit",
            },
        )
    assert resp.status_code == 400, resp.text
    assert "E-CELL-UNCERTIFIED" in str(resp.json()["detail"])
    assert "seed_exec_failed" not in resp.text
    assert broken_credential.execs == []
    assert create.await_count == 0


def test_route_admits_certified_cell_and_seeds_exactly_once(client, store, working_credential):
    """The admitted arm is untouched: one exec, and the minted resume context
    reaches create_terminal as ``fork_context`` with the derived class."""
    create = AsyncMock(return_value=_terminal())
    with patch("cli_agent_orchestrator.api.main.terminal_service.create_terminal", create):
        resp = client.post(
            "/sessions/cao-f1007/terminals",
            params={
                "provider": "codex",
                "agent_profile": "general-codex",
                "cell_request_class": "explicit",
            },
        )
    assert resp.status_code == 201, resp.text
    assert len(working_credential.execs) == 1
    kwargs = create.call_args.kwargs
    assert kwargs["fork_context"] is not None and kwargs["fork_context"].base_name == "seed"
    assert kwargs["cell_request_class"] == "explicit"


def test_route_forged_class_refused_before_the_seed(client, store, broken_credential):
    """F868 r4 kept: a forged class is still a typed 403 — and now it is raised
    BEFORE the seed, so a forged request execs nothing either."""
    create = AsyncMock(return_value=_terminal())
    with patch("cli_agent_orchestrator.api.main.terminal_service.create_terminal", create):
        resp = client.post(
            "/sessions/cao-f1007/terminals",
            params={
                "provider": "codex",
                "agent_profile": "general-codex",
                "cell_request_class": "legacy",
            },
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "E-CELL-CLASS-FORGED"
    assert broken_credential.execs == []
    assert create.await_count == 0


def test_route_resume_branch_does_not_seed(client, store, broken_credential):
    """RESUME BRANCH UNCHANGED: a resume create never reaches the cold seed at
    all (its fork_context comes from the root admission), so this arm must show
    zero execs regardless of the cell — the reorder touched only the cold path.
    An unknown handle is refused by the resume admission, which is the point:
    the refusal arrives with no provider execution behind it."""
    create = AsyncMock(return_value=_terminal())
    with patch("cli_agent_orchestrator.api.main.terminal_service.create_terminal", create):
        resp = client.post(
            "/sessions/cao-f1007/terminals",
            params={"provider": "codex", "agent_profile": "general-codex"},
            json={"resume_from": "no-such-terminal"},
        )
    assert resp.status_code >= 400
    assert broken_credential.execs == []
    assert create.await_count == 0
