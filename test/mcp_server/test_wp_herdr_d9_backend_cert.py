"""WP-HERDR D9 — the BACKEND certification axis (H1 slice 2).

herdr is a backend, not a provider, so a cell being certified for its provider
says nothing about whether it has been proven under the herdr runtime. D9 gives
the backend its own axis: a ``herdr_certification:`` block beside
``certification:``, a ``backend =`` key on the routing row, and a FOURTH ordered
check in ``resolve_routing_binding`` that refuses ``E-BACKEND-UNCERTIFIED``.

Two properties carry the design and each has a test that would fail without it:

* **Certification, not the backend setting, switches a terminal over.** A row can
  name ``backend = "herdr"`` and still be refused; nothing about setting the
  backend grants the cohort.
* **The pin is about THIS machine.** The recorded ``herdr_sha256`` must equal the
  sha of the herdr binary installed here, so a row certifying a binary we cannot
  see, or a different one, is a refusal rather than a pass.

The fixture store and its helpers are the F497 D9 ones, reused rather than
rebuilt — the two axes must hash the same way or a sha discipline that holds on
one block and not the other is worse than none.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cli_agent_orchestrator.utils import routing
from cli_agent_orchestrator.utils.profile_composition import position_sha

from .test_f497_routing_d9 import _build_store, _certify, _shas, _write

#: A stand-in for the sha256 of an installed herdr binary.
FAKE_BINARY_SHA = "a" * 64


def _herdr_certify(
    positions: Path,
    position: str,
    provider: str,
    outcome: str,
    *,
    binary_sha: str = FAKE_BINARY_SHA,
    position_sha_override: str | None = None,
) -> None:
    """Append a ``herdr_certification`` row at the CURRENT sha pair."""
    import frontmatter

    p_sha, o_sha = _shas(positions, position, provider)
    path = positions / f"{position}.md"
    parsed = frontmatter.loads(path.read_text(encoding="utf-8"))
    rows = list(parsed.metadata.get("herdr_certification") or [])
    rows.append(
        {
            "provider": provider,
            "herdr_version": "0.9.0",
            "herdr_sha256": binary_sha,
            "protocol": 22,
            "position_sha": position_sha_override or p_sha,
            "overlay_sha": o_sha,
            "outcome": outcome,
            "date": "2026-09-11",
            "evidence": "/data/cao-scratch/briefs/h1-live.md",
        }
    )
    parsed.metadata["herdr_certification"] = rows
    path.write_text(frontmatter.dumps(parsed) + "\n", encoding="utf-8")


def _routing(tmp_path: Path, *, backend: str | None) -> Path:
    backend_line = f'backend = "{backend}"\n' if backend else ""
    rt = tmp_path / "routing.toml"
    _write(
        rt,
        textwrap.dedent(f"""\
            [[binding]]
            position = "general"
            provider = "kiro_cli"
            kind = "cao"
            {backend_line}"""),
    )
    return rt


@pytest.fixture
def installed_binary(monkeypatch: pytest.MonkeyPatch):
    """Pretend a herdr binary with :data:`FAKE_BINARY_SHA` is installed."""

    def _set(value: str | None) -> None:
        monkeypatch.setattr(routing, "installed_herdr_sha256", lambda: value)

    _set(FAKE_BINARY_SHA)
    return _set


# --------------------------------------------------------------------------
# the routing row's backend key
# --------------------------------------------------------------------------


def test_a_row_without_a_backend_key_is_a_tmux_row(tmp_path: Path) -> None:
    """Every routing.toml that exists today keeps its exact meaning."""
    table = routing.load_routing_table(_routing(tmp_path, backend=None))
    binding = table.binding_for("general", "kiro_cli")
    assert binding is not None
    assert binding.backend == "tmux"


def test_a_herdr_row_round_trips(tmp_path: Path) -> None:
    table = routing.load_routing_table(_routing(tmp_path, backend="herdr"))
    binding = table.binding_for("general", "kiro_cli")
    assert binding is not None
    assert binding.backend == "herdr"


def test_an_unknown_backend_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(routing.RoutingError) as ei:
        routing.load_routing_table(_routing(tmp_path, backend="screen"))
    assert ei.value.code is None  # a structural fault, not a named refusal


def test_an_in_harness_row_may_not_name_a_backend(tmp_path: Path) -> None:
    """A non-CAO lane runs no terminal, so it has no backend to name."""
    rt = tmp_path / "routing.toml"
    _write(
        rt,
        """\
        [[binding]]
        position = "design_reviewer"
        kind = "in_harness"
        backend = "herdr"
        """,
    )
    with pytest.raises(routing.RoutingError):
        routing.load_routing_table(rt)


# --------------------------------------------------------------------------
# herdr_cell_certified — the sha pair AND the binary pin
# --------------------------------------------------------------------------


def test_a_cell_with_no_herdr_rows_is_uncertified(tmp_path: Path, installed_binary) -> None:
    positions = _build_store(tmp_path)
    assert routing.herdr_cell_certified("general", "kiro_cli", positions) == (
        False,
        "UNCERTIFIED",
    )


def test_a_pass_row_at_the_current_shas_with_a_matching_binary_certifies(
    tmp_path: Path, installed_binary
) -> None:
    positions = _build_store(tmp_path)
    _herdr_certify(positions, "general", "kiro_cli", "PASS")
    assert routing.herdr_cell_certified("general", "kiro_cli", positions) == (True, "PASS")


def test_the_herdr_block_does_not_invalidate_its_own_sha(tmp_path: Path, installed_binary) -> None:
    """A recorded row must not change the sha it recorded.

    ``position_sha`` excludes BOTH certification blocks for this reason; without
    the herdr one in the exclusion set no herdr row could ever match, and the
    whole axis would read as permanently uncertified.
    """
    import frontmatter

    positions = _build_store(tmp_path)
    before = position_sha(
        *(lambda p: (p.content, dict(p.metadata)))(
            frontmatter.loads((positions / "general.md").read_text(encoding="utf-8"))
        )
    )
    _herdr_certify(positions, "general", "kiro_cli", "PASS")
    after_parsed = frontmatter.loads((positions / "general.md").read_text(encoding="utf-8"))
    assert position_sha(after_parsed.content, dict(after_parsed.metadata)) == before


def test_a_stale_row_is_not_a_certification(tmp_path: Path, installed_binary) -> None:
    positions = _build_store(tmp_path)
    _herdr_certify(
        positions, "general", "kiro_cli", "PASS", position_sha_override="deadbeefdeadbeef"
    )
    assert routing.herdr_cell_certified("general", "kiro_cli", positions) == (
        False,
        "UNCERTIFIED",
    )


def test_a_non_pass_outcome_is_reported_verbatim(tmp_path: Path, installed_binary) -> None:
    positions = _build_store(tmp_path)
    _herdr_certify(positions, "general", "kiro_cli", "FAIL")
    assert routing.herdr_cell_certified("general", "kiro_cli", positions) == (False, "FAIL")


def test_a_different_installed_binary_refuses(tmp_path: Path, installed_binary) -> None:
    """D7's pin: the certified runtime must be the running one."""
    positions = _build_store(tmp_path)
    _herdr_certify(positions, "general", "kiro_cli", "PASS")
    installed_binary("b" * 64)
    assert routing.herdr_cell_certified("general", "kiro_cli", positions) == (
        False,
        "BINARY-MISMATCH",
    )


def test_an_absent_binary_refuses_rather_than_passes(tmp_path: Path, installed_binary) -> None:
    """A row certifying a binary we cannot see is not evidence about this machine."""
    positions = _build_store(tmp_path)
    _herdr_certify(positions, "general", "kiro_cli", "PASS")
    installed_binary(None)
    assert routing.herdr_cell_certified("general", "kiro_cli", positions) == (
        False,
        "BINARY-UNKNOWN",
    )


def test_the_pin_can_be_skipped_for_a_sha_pair_only_question(
    tmp_path: Path, installed_binary
) -> None:
    positions = _build_store(tmp_path)
    _herdr_certify(positions, "general", "kiro_cli", "PASS")
    installed_binary(None)
    assert routing.herdr_cell_certified(
        "general", "kiro_cli", positions, resolve_installed=False
    ) == (True, "PASS")


# --------------------------------------------------------------------------
# the fourth ordered check in resolve_routing_binding
# --------------------------------------------------------------------------


def test_a_herdr_row_with_no_backend_certification_is_refused(
    tmp_path: Path, installed_binary
) -> None:
    positions = _build_store(tmp_path)
    _certify(positions, "general", "kiro_cli", "PASS")  # provider + cell both PASS
    table = routing.load_routing_table(_routing(tmp_path, backend="herdr"))
    with pytest.raises(routing.RoutingError) as ei:
        routing.resolve_routing_binding("general", "kiro_cli", table=table, positions_dir=positions)
    assert ei.value.code == routing.E_BACKEND_UNCERTIFIED


def test_setting_the_backend_does_not_grant_the_cohort(tmp_path: Path, installed_binary) -> None:
    """The load-bearing claim: certification switches a terminal over, not the
    backend setting. The SAME cell, the SAME provider certification, differing
    only in whether a herdr row exists."""
    positions = _build_store(tmp_path)
    _certify(positions, "general", "kiro_cli", "PASS")
    table = routing.load_routing_table(_routing(tmp_path, backend="herdr"))
    with pytest.raises(routing.RoutingError):
        routing.resolve_routing_binding("general", "kiro_cli", table=table, positions_dir=positions)
    _herdr_certify(positions, "general", "kiro_cli", "PASS")
    resolution = routing.resolve_routing_binding(
        "general", "kiro_cli", table=table, positions_dir=positions
    )
    assert resolution.backend == "herdr"
    assert resolution.herdr_certified is True


def test_a_tmux_row_never_reaches_the_backend_check(tmp_path: Path, installed_binary) -> None:
    """No herdr rows anywhere, and the cell still binds — the check is opt-in."""
    positions = _build_store(tmp_path)
    _certify(positions, "general", "kiro_cli", "PASS")
    table = routing.load_routing_table(_routing(tmp_path, backend=None))
    resolution = routing.resolve_routing_binding(
        "general", "kiro_cli", table=table, positions_dir=positions
    )
    assert resolution.backend == "tmux"
    assert resolution.herdr_certified is False


def test_the_backend_check_runs_after_provider_certification(
    tmp_path: Path, installed_binary
) -> None:
    """Ordering: a herdr row on an UNCERTIFIED provider reports the provider
    refusal, not the backend one — the fourth check is genuinely fourth."""
    positions = _build_store(tmp_path)  # no general PASS at all
    table = routing.load_routing_table(_routing(tmp_path, backend="herdr"))
    with pytest.raises(routing.RoutingError) as ei:
        routing.resolve_routing_binding("general", "kiro_cli", table=table, positions_dir=positions)
    assert ei.value.code == routing.E_PROVIDER_UNCERTIFIED
