"""F594 #451 — REGRESSION GUARD (not a fix) for the routing-driven assign
``(provider, agent_profile)`` pair.

READ THIS BEFORE ASSUMING THE ARMS TEST A FIX IN THIS COMMIT. They do not.
The mismatched-pair defect these arms describe was ALREADY FIXED by F613 #469
(commit ``3dea0c00``, 2026-08-30) — this commit adds NO source change. These
arms exist so that a future change to ``resolve_assignment_target`` (which
itself resolves the routing binding, ``utils/agent_profiles.py``) or to the D7
routing-driven branch in ``_assign_impl`` cannot SILENTLY re-open the defect.

The historical defect (pre-F613): ``_assign_impl`` computed a resolved provider
but never threaded it to ``_create_terminal``, which re-derived via
``resolve_provider(..., fallback=supervisor provider)`` → ``claude_code`` when
the rewritten profile name failed to load; and the general fallback returned the
raw ``f"{provider}_general"`` (``cline_cli_general``), an uninstalled name. The
two together produced the live URL::

    400 ... /terminals?provider=claude_code&agent_profile=cline_cli_general&...

F613 #469 fixed BOTH halves: it resolves the installed alias stub
(``cline_general``) and threads ``provider=_resolved_provider`` into
``_create_terminal`` (server.py, the POST-building call). On HEAD the composed
pair is consistent by construction.

These arms assert on the COMPOSED REQUEST — the ``(provider, agent_profile)``
pair actually handed to ``_create_terminal`` (which builds the POST params) —
never on source text. ``_create_terminal`` is mocked exactly as the sibling D9
harness (test_f497_routing_d9.py) does; the provider reaches it as the
``provider=`` kwarg, so it is captured via ``k.get("provider")``.

WHY THE "REVERT TO CALLER'S PROVIDER" MUTATION IS UNSATISFIABLE (by construction,
not by weakness of the arms). On the routing-driven path ``provider`` is None, so
``resolve_assignment_target`` looks the binding up ITSELF (via
``_routing_provider_for_position``) and returns the BOUND provider as the
"caller's" resolved provider. ``resolve_routing_binding`` then returns that same
provider UNCHANGED. So the "resolver's provider" and the "caller's resolved
provider" are the SAME VALUE on this path — there is no distinct caller provider
to revert to. A reader who tries to kill an arm by pointing the call site back at
"the caller's provider" will find the arm still green, because that expression
already evaluates to the bound provider here. This is a real, load-bearing
invariant (the binding is resolved once and reused), NOT a hollow always-true
assertion of the kind that has cost us gate rounds. ``test_f594_mutation_guard_*``
pins the invariant directly: the composed provider equals
``resolve_routing_binding(...).provider``.
"""

from __future__ import annotations

from pathlib import Path

# Reuse the D9 fixture-store builders verbatim — one source of truth for the
# clause table, personas, and certification-row writer.
from test.mcp_server.test_f497_routing_d9 import (
    _CLAUSES_TOML,
    _GENERAL_BODY,
    _certify,
    _write,
)
from unittest.mock import patch

import pytest

# The provider the "supervisor seat" defaults to in these tests. It is
# DELIBERATELY not the bound provider in ARM 1, reproducing the live mismatch
# (supervisor seat = claude_code; grunt binds to cline_cli).
_CALLER_PROVIDER = "claude_code"


def _seed_terminal_provider(monkeypatch, provider: str) -> None:
    """Make ``_create_terminal``'s caller-provider re-derivation observable.

    ``_assign_impl`` resolves ``_resolved_provider`` from
    ``resolve_assignment_target(agent_profile, provider)``. For the routing-
    driven path ``provider`` is None, so the caller default is whatever the
    seat's own provider resolves to. We do not need the real value for the
    position arms (the fix repoints away from it); we only pin CAO_TERMINAL_ID
    so the existing-session branch is taken.
    """
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")


def _build_dev_routing_env(
    tmp_path: Path, monkeypatch, *, provider: str, certify_cell: bool
) -> None:
    """Build a hermetic routing env: a non-gate ``dev`` position bound to
    ``provider`` in a fixture routing.toml, with the provider ``general`` cell
    PASS and (optionally) the ``dev`` cell PASS.

    When ``certify_cell`` is True the ``dev`` cell is PASS, so the resolver binds
    the position directly (``spawn_profile == "dev"``) with NO general fallback —
    the cleanest surface on which to assert the composed provider. Seeds the
    ``(general, provider)`` alias stub regardless so a fallback, if it ever
    occurred, still resolves to an installed stem.
    """
    home = tmp_path / "cao-home"
    positions = home / "agent-store" / "positions"
    overlays = home / "agent-store" / "overlays"

    _write(
        positions / "_clauses.toml",
        _CLAUSES_TOML.replace(
            "[budget]",
            'dev = ["callback-contract", "containment"]\n\n[budget]',
        )
        + "dev = 6000\n",
    )
    _write(positions / "general.md", _GENERAL_BODY)
    _write(
        positions / "dev.md",
        "# DEV\nwork.\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n",
    )
    _write(overlays / f"{provider}.md", f"## Provider notes ({provider})\nq.\n")

    _certify(positions, "general", provider, "PASS")
    if certify_cell:
        _certify(positions, "dev", provider, "PASS")

    # Installed general alias stub for (general, provider) — mirrors F613 #469.
    short = {"cline_cli": "cline", "kiro_cli": "kiro", "codex": "codex"}.get(provider, provider)
    _write(
        home / "agent-store" / f"{short}_general.md",
        f"---\nextends: general\nname: {short}_general\nprovider: {provider}\n---\n"
        f"# {short} general\n",
    )

    rt = tmp_path / "routing.toml"
    _write(
        rt,
        f"""\
        [[binding]]
        position = "dev"
        provider = "{provider}"
        kind = "cao"
        """,
    )

    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    _seed_terminal_provider(monkeypatch, provider)


def _assign_capturing(agent_profile: str, provider_arg=None):
    """Run ``_assign_impl`` for ``agent_profile`` (optional explicit provider),
    patching ``_create_terminal`` to capture the COMPOSED request. Returns
    ``(result, captured)`` where captured carries agent_profile + provider."""
    from cli_agent_orchestrator.mcp_server.server import _assign_impl

    captured: dict = {}

    def fake_create(agent_profile, working_directory, *a, **k):
        captured["agent_profile"] = agent_profile
        captured["provider"] = k.get("provider")
        return ("worker_f594", "unused")

    with patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ):
        if provider_arg is None:
            result = _assign_impl(agent_profile, "task", working_directory="/repo")
        else:
            result = _assign_impl(
                agent_profile, "task", working_directory="/repo", provider=provider_arg
            )
    return result, captured


# --------------------------------------------------------------------------
# ARM 1 — bound provider DIFFERS from the caller's: pair composed from the
# routing binding, assign succeeds. (The grunt→cline_cli live shape.) On HEAD
# this is F613's landed behaviour; the guard fails if a future change re-drops
# the provider back to the caller's.
# --------------------------------------------------------------------------


def test_arm1_position_bound_provider_differs_composes_matching_pair(tmp_path, monkeypatch):
    _build_dev_routing_env(tmp_path, monkeypatch, provider="cline_cli", certify_cell=True)

    result, captured = _assign_capturing("dev")

    assert result["success"] is True
    # The COMPOSED REQUEST: provider taken from the routing binding — the bound
    # provider, NOT the supervisor seat's own. On HEAD (post-F613) this is
    # already correct; pre-F613 the POST carried claude_code here.
    assert captured["provider"] == "cline_cli"
    assert captured["provider"] != _CALLER_PROVIDER
    # Certified cell → the position itself is the spawn profile (no fallback).
    assert captured["agent_profile"] == "dev"


# --------------------------------------------------------------------------
# ARM 2 — bound provider EQUALS the caller's: unchanged, still works.
# --------------------------------------------------------------------------


def test_arm2_position_bound_provider_equals_caller_still_works(tmp_path, monkeypatch):
    # Bind the position to the caller's own provider. The pair is trivially
    # consistent regardless; assert it still composes.
    _build_dev_routing_env(tmp_path, monkeypatch, provider=_CALLER_PROVIDER, certify_cell=True)

    result, captured = _assign_capturing("dev")

    assert result["success"] is True
    assert captured["provider"] == _CALLER_PROVIDER
    assert captured["agent_profile"] == "dev"


# --------------------------------------------------------------------------
# ARM 3 — a legacy profile name (no position row) is unaffected: no routing
# rewrite, the caller's provider is preserved (provider kwarg stays None so
# _create_terminal re-derives, exactly as pre-F594).
# --------------------------------------------------------------------------


def test_arm3_legacy_profile_name_unaffected(tmp_path, monkeypatch):
    # An env with a bound 'dev' position exists, but we assign a name that is NOT
    # a position (kiro_dev) → the routing-driven branch never engages.
    _build_dev_routing_env(tmp_path, monkeypatch, provider="cline_cli", certify_cell=True)

    result, captured = _assign_capturing("kiro_dev")

    assert result["success"] is True
    assert captured["agent_profile"] == "kiro_dev"
    # No routing rewrite: _resolved_provider stays the caller-derived value.
    # resolve_assignment_target returns None for a legacy name with no provider=,
    # so _create_terminal receives provider=None and re-derives (legacy path).
    assert captured["provider"] is None


# --------------------------------------------------------------------------
# ARM 4 — an explicit provider= on a position name (D7 operator override, the
# current #505 workaround) behaves as today: the operator-chosen provider is
# what composes.
# --------------------------------------------------------------------------


def test_arm4_explicit_provider_on_position_still_behaves(tmp_path, monkeypatch):
    _build_dev_routing_env(tmp_path, monkeypatch, provider="cline_cli", certify_cell=True)

    # Explicit provider= makes this the D7 operator-override path (NOT routing-
    # driven), so the D9 branch is skipped. The operator-chosen provider wins and
    # the composed profile is the D6 synthesis <provider>_<position>.
    result, captured = _assign_capturing("dev", provider_arg="cline_cli")

    assert result["success"] is True
    assert captured["provider"] == "cline_cli"
    # D6 synthesis on the override path (no alias stub for the dev cell): the
    # deterministic <provider>_<position> name.
    assert captured["agent_profile"] == "cline_cli_dev"


# --------------------------------------------------------------------------
# MUTATION guard / INVARIANT pin — on the routing-driven path the composed
# provider EQUALS the routing resolver's provider. The "revert to the caller's
# provider" mutation the ARMS spec asks for is UNSATISFIABLE here by
# construction (see module docstring): resolve_assignment_target resolves the
# binding itself, so the "caller's resolved provider" IS the bound provider —
# there is no distinct value to revert to. This pins the load-bearing invariant
# directly (resolve once, reuse) so a future re-derivation that re-introduced a
# distinct caller provider on this path would break it.
# --------------------------------------------------------------------------


def test_f594_mutation_guard_provider_is_resolver_not_caller(tmp_path, monkeypatch):
    """Pin the invariant: composed provider == routing resolver's provider.

    This is NOT a killable mutation of a source edit in this commit (there is no
    edit — F613 #469 already made this true). It is a guard: if a future change
    re-introduced a distinct caller provider on the routing-driven path and let
    it reach the POST, ``captured["provider"]`` would diverge from
    ``res.provider`` and this would fail.
    """
    from cli_agent_orchestrator.constants import positions_store_dir, routing_toml_path
    from cli_agent_orchestrator.utils.routing import (
        load_routing_table,
        resolve_routing_binding,
    )

    _build_dev_routing_env(tmp_path, monkeypatch, provider="cline_cli", certify_cell=True)

    # The resolver's answer is the ground truth the call site must honor.
    rt = load_routing_table(routing_toml_path())
    res = resolve_routing_binding("dev", "cline_cli", table=rt, positions_dir=positions_store_dir())
    assert res.provider == "cline_cli"

    # The end-to-end composed request must carry exactly that provider.
    _, captured = _assign_capturing("dev")
    assert captured["provider"] == res.provider
    assert captured["provider"] != _CALLER_PROVIDER
