"""F809 #666 A14 (#681) + A10 (#673) — ``cao-orchestrator lint-doctrine``.

Every check has an EXECUTED arm over a real workspace on disk, driven through the
real Click command and the real composer subprocess. The fixture ships a stub
composer rather than importing the root repo's ``doctrine/compose/compose.py``:
the fork must be testable from an unpacked sdist, with no sibling checkout.

The mutation arms are the point of the file. Four of them replay defects this
build exists to fix — the shell script's warn-mode ``die()`` that exited 0 on its
own FATALs, a C3 that is not wired into the exit code, an off-by-one on the
ceiling, and an inventory nobody regenerates — and each must come back RED.
"""

from __future__ import annotations

import datetime as dt
import json
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.orchestrator_commands.lint_doctrine import (
    EXIT_BUDGET,
    EXIT_COVERAGE,
    EXIT_INTERNAL,
    EXIT_OK,
    MECHANISM_SOURCES,
    PHASE_LIMITS,
    Finding,
    LintError,
    LintResult,
    authoritative,
    exit_code_for,
    lint_doctrine,
    mechanism_files,
    parse_ledger,
    render_inventory,
    resolve_mechanisms,
    run_lint,
)

STUB_COMPOSER = """\
import argparse, sys, tomllib
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--manifest", required=True)
p.add_argument("--table", required=True)
p.add_argument("--variant", required=True)
a = p.parse_args()
manifest = Path(a.manifest)
data = tomllib.loads(manifest.read_text(encoding="utf-8"))
out = []
for rel in data[a.table][a.variant]["sections"]:
    out.append((manifest.parent / rel).resolve().read_text(encoding="utf-8"))
sys.stdout.write("".join(out))
"""

LEDGER_HEAD = """\
# MIGRATION-F809

Dispositions: **retained/compressed**, **mechanism EXISTS [id]**, **pending ASK [Axx]**.

## Appendix 1 — playbook section units

| Unit (label) | Disposition |
|---|---|
"""


def _workspace(
    tmp_path: Path,
    *,
    section: str = "Rule one. ^alpha\n\nRule two `[E-DEMO-TOKEN]`. ^beta\n",
    ledger_rows: str = (
        "| ^alpha | retained/compressed |\n" "| ^beta | mechanism EXISTS [E-DEMO-TOKEN] |\n"
    ),
    gate_rules: str = "# GATE-RULES\n\nOne rule.\n",
    mechanism_source: str = 'readonly CODES="E-DEMO-TOKEN E-OTHER"\n',
    exceptions: str = "",
) -> Path:
    """A minimal but REAL workspace: manifest, sections, stub composer, ledger, sources."""
    root = tmp_path / "ws"
    (root / "doctrine/manifests").mkdir(parents=True)
    (root / "doctrine/sections/shared").mkdir(parents=True)
    (root / "doctrine/compose").mkdir(parents=True)
    (root / "doctrine/recipes").mkdir(parents=True)
    (root / "orchestrator/blueprints").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / ".claude").mkdir(parents=True)

    (root / "doctrine/manifests/orchestrator.toml").write_text(
        '[orchestrator.demo]\nsections = [\n  "../sections/shared/one.md",\n]\n', encoding="utf-8"
    )
    (root / "doctrine/sections/shared/one.md").write_text(section, encoding="utf-8")
    (root / "doctrine/compose/compose.py").write_text(STUB_COMPOSER, encoding="utf-8")
    (root / "doctrine/MIGRATION-F809.md").write_text(LEDGER_HEAD + ledger_rows, encoding="utf-8")
    (root / "doctrine/budget-exceptions.toml").write_text(exceptions, encoding="utf-8")
    (root / "orchestrator/GATE-RULES.md").write_text(gate_rules, encoding="utf-8")
    (root / "orchestrator/blueprints/f809-doctrine-mechanism-boundary.md").write_text(
        "#668 F811 A06; #674 F817 A02;\n", encoding="utf-8"
    )
    (root / "scripts/gated-merge.sh").write_text(mechanism_source, encoding="utf-8")
    (root / "scripts/report-attest.sh").write_text("# attest\n", encoding="utf-8")
    (root / ".claude/settings.json").write_text('{"hooks": []}\n', encoding="utf-8")
    return root


def _lint(root: Path, tmp_path: Path, **kwargs):
    defaults: dict[str, object] = dict(
        manifest=root / "doctrine/manifests/orchestrator.toml",
        composer=root / "doctrine/compose/compose.py",
        gate_rules=root / "orchestrator/GATE-RULES.md",
        exceptions=root / "doctrine/budget-exceptions.toml",
        ledger=root / "doctrine/MIGRATION-F809.md",
        ask_map=root / "orchestrator/blueprints/f809-doctrine-mechanism-boundary.md",
        scratch=tmp_path / "scratch",
        today=dt.date(2026, 9, 16),
    )
    defaults.update(kwargs)
    return run_lint(root, **defaults)  # type: ignore[arg-type]


def _run_cli(root: Path, tmp_path: Path, *extra: str):
    return CliRunner().invoke(
        lint_doctrine,
        ["--workspace", str(root), "--scratch", str(tmp_path / "cli-scratch"), *extra],
    )


# --------------------------------------------------------------------------
# C1/C2 — the byte budget
# --------------------------------------------------------------------------
def test_c1_under_budget_is_ok_and_exits_zero(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    result = _lint(root, tmp_path)
    assert [row.verdict for row in result.measurements] == ["OK", "OK"]
    assert result.findings == []
    assert exit_code_for(result, "migration", "block") == EXIT_OK


def test_c1_over_budget_warns_in_warn_mode_and_blocks_in_block_mode(tmp_path: Path) -> None:
    root = _workspace(tmp_path, section="x" * 60_000 + "\n^alpha\n")
    result = _lint(root, tmp_path)
    over = [row for row in result.measurements if row.verdict == "OVER"]
    assert [row.name for row in over] == ["playbook:demo"]
    assert [f.check for f in result.findings if f.kind == "budget"] == ["C1"]
    assert exit_code_for(result, "migration", "warn") == EXIT_OK
    assert exit_code_for(result, "migration", "block") == EXIT_BUDGET


def test_c2_gate_rules_over_budget_is_its_own_check(tmp_path: Path) -> None:
    root = _workspace(tmp_path, gate_rules="# GATE-RULES\n" + "y" * 20_000 + "\n")
    result = _lint(root, tmp_path)
    assert [f.check for f in result.findings if f.kind == "budget"] == ["C2"]


def test_final_phase_is_a_ratchet_and_never_blocks(tmp_path: Path) -> None:
    """AC1 r3: `final` is tracked, never a merge requirement."""
    root = _workspace(tmp_path, section="x" * 30_000 + "\n^alpha\n")
    result = _lint(root, tmp_path, phase="final")
    assert any(f.kind == "budget" for f in result.findings)
    assert exit_code_for(result, "final", "block") == EXIT_OK


def test_the_migration_limits_are_the_ac1_r3_pair() -> None:
    assert PHASE_LIMITS["migration"] == (45_000, 13_000)
    assert PHASE_LIMITS["final"] == (20_000, 8_000)


# --------------------------------------------------------------------------
# C3 — mechanism coverage (A10 #673 folded in)
# --------------------------------------------------------------------------
def test_c3_clean_when_every_claimed_id_resolves(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    result = _lint(root, tmp_path)
    assert [f for f in result.findings if f.check == "C3"] == []
    assert "`E-DEMO-TOKEN` | script | `scripts/gated-merge.sh:1`" in result.inventory


def test_c3_true_positive_names_the_prose_only_claim(tmp_path: Path) -> None:
    """The `queue-native-seat-carrier` shape: a mechanism id in no code at all."""
    root = _workspace(
        tmp_path,
        ledger_rows="| ^alpha | mechanism EXISTS [queue-native-seat-carrier] |\n| ^beta | retained/compressed |\n",
    )
    result = _lint(root, tmp_path)
    findings = [f for f in result.findings if f.check == "C3"]
    assert [f.subject for f in findings] == ["queue-native-seat-carrier"]
    assert "resolves to no code" in findings[0].detail
    assert "doctrine/MIGRATION-F809.md:9" in findings[0].detail
    assert exit_code_for(result, "migration", "warn") == EXIT_COVERAGE, "C3 is armed in warn mode"


def test_c3_finds_a_claim_cited_only_in_prose(tmp_path: Path) -> None:
    """A bracketed citation in a section is a claim, and carries its own site."""
    root = _workspace(
        tmp_path,
        section="Replies ride the queue `[queue-native-seat-carrier]`. ^alpha\n",
        ledger_rows="| ^alpha | mechanism EXISTS [queue-native-seat-carrier] |\n",
    )
    result = _lint(root, tmp_path)
    detail = [f for f in result.findings if f.check == "C3"][0].detail
    assert "doctrine/sections/shared/one.md:1" in detail


def test_prose_labels_are_not_mistaken_for_mechanism_ids(tmp_path: Path) -> None:
    """`[LIVE-ONLY]` is a prose label; only ids the ledger declares are claims."""
    root = _workspace(tmp_path, section="Mark those asserts [LIVE-ONLY] honestly. ^alpha\n^beta\n")
    result = _lint(root, tmp_path)
    assert "LIVE-ONLY" not in result.inventory
    assert [f for f in result.findings if f.check == "C3"] == []


def test_retired_claim_recorded_in_parentheses_is_not_re_asserted(tmp_path: Path) -> None:
    """A row that NARRATES the claim it retired must not re-raise it."""
    root = _workspace(
        tmp_path,
        ledger_rows=(
            "| ^alpha | pending ASK [A02] + interim rule (was mechanism EXISTS "
            "[queue-native-seat-carrier], C3's first true positive) |\n"
            "| ^beta | retained/compressed |\n"
        ),
    )
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([{"number": 674, "state": "OPEN"}]), encoding="utf-8")
    result = _lint(root, tmp_path, issues_json=issues)
    assert [f for f in result.findings if f.check == "C3"] == []


def test_authoritative_drops_only_parenthesised_narration() -> None:
    assert (
        authoritative("pending ASK [A02] (was mechanism EXISTS [x])").strip() == "pending ASK [A02]"
    )
    assert authoritative("mechanism EXISTS [E-X; E-Y]") == "mechanism EXISTS [E-X; E-Y]"


def test_compound_and_glob_claim_forms_expand(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        ledger_rows=(
            "| ^alpha | mechanism EXISTS [E-KEY-ABSENT/-BARE; E-KEY-*] |\n"
            "| ^beta | retained/compressed |\n"
        ),
        mechanism_source='readonly CODES="E-KEY-ABSENT E-KEY-BARE"\n',
    )
    result = _lint(root, tmp_path)
    assert [f for f in result.findings if f.check == "C3"] == []
    for one in ("E-KEY-ABSENT", "E-KEY-BARE", "E-KEY-*"):
        assert f"`{one}`" in result.inventory


def test_c3_pending_ask_must_be_an_open_issue(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        ledger_rows="| ^alpha | pending ASK [A02] + interim rule |\n| ^beta | retained/compressed |\n",
    )
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([{"number": 674, "state": "CLOSED"}]), encoding="utf-8")
    result = _lint(root, tmp_path, issues_json=issues)
    findings = [f for f in result.findings if f.check == "C3" and f.subject == "A02"]
    assert findings and "#674 is CLOSED" in findings[0].detail

    issues.write_text(json.dumps([{"number": 674, "state": "OPEN"}]), encoding="utf-8")
    assert [f for f in _lint(root, tmp_path, issues_json=issues).findings if f.check == "C3"] == []


def test_ask_check_is_skipped_loudly_when_issue_state_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a box with no gh, C3's ASK half must SKIP — not silently pass."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.orchestrator_commands.lint_doctrine.shutil.which",
        lambda _name: None,
    )
    root = _workspace(
        tmp_path,
        ledger_rows="| ^alpha | pending ASK [A02] + interim rule |\n| ^beta | retained/compressed |\n",
    )
    result = _lint(root, tmp_path)
    assert any("pending-ASK" in note for note in result.skipped)
    assert [f for f in result.findings if f.check == "C3"] == []


# --------------------------------------------------------------------------
# inventory generation (AC9)
# --------------------------------------------------------------------------
def test_inventory_generation_is_deterministic(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    out = root / "orchestrator/mechanism-inventory.md"
    first = _lint(root, tmp_path, inventory_out=out)
    generated = out.read_text(encoding="utf-8")
    second = _lint(root, tmp_path, inventory_out=out)
    assert out.read_text(encoding="utf-8") == generated
    assert first.inventory == second.inventory
    assert "GENERATED" in generated and "NEVER hand-edit" in generated


def test_a_stale_committed_inventory_is_a_c3_finding(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    committed = root / "orchestrator/mechanism-inventory.md"
    committed.write_text("# Mechanism inventory\n\nhand-written, and wrong\n", encoding="utf-8")
    result = _lint(root, tmp_path, committed_inventory=committed)
    findings = [f for f in result.findings if "stale" in f.detail]
    assert findings and findings[0].check == "C3"
    assert exit_code_for(result, "migration", "warn") == EXIT_COVERAGE


def test_emitting_the_inventory_clears_the_staleness_finding(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    committed = root / "orchestrator/mechanism-inventory.md"
    committed.write_text("stale\n", encoding="utf-8")
    result = _lint(root, tmp_path, inventory_out=committed, committed_inventory=committed)
    assert [f for f in result.findings if "stale" in f.detail] == []


def test_the_skill_cli_is_never_a_mechanism_source(tmp_path: Path) -> None:
    """C3 must not be self-satisfying: this module's own docstring names FROZEN-PIN.

    The scan reaches the INSTALLED CAO build, so a runtime mechanism resolves from an
    sdist with no sibling checkout. That is also how the linter could resolve an id
    against itself, which the exclusion in ``mechanism_files`` prevents.
    """
    scanned = mechanism_files(_workspace(tmp_path), MECHANISM_SOURCES)
    assert scanned, "the scan found no mechanism source at all"
    offenders = [rel for rel, _kind, path in scanned if "orchestrator_commands" in path.parts]
    assert offenders == [], f"the linter scans the command that cites the ids: {offenders}"
    assert any(
        "authority_pin_service.py" in rel for rel, _k, _p in scanned
    ), "the installed runtime is not reachable — FROZEN-PIN would be a false positive"


def test_a_comment_only_hit_loses_to_a_code_hit(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        mechanism_source='# E-DEMO-TOKEN is documented here\nreadonly CODES="E-DEMO-TOKEN"\n',
    )
    resolutions = resolve_mechanisms(
        ["E-DEMO-TOKEN"], [("scripts/gated-merge.sh", "script", root / "scripts/gated-merge.sh")]
    )
    assert resolutions["E-DEMO-TOKEN"].location == "scripts/gated-merge.sh:2"


# --------------------------------------------------------------------------
# C4 — ledger completeness (AC3)
# --------------------------------------------------------------------------
def test_c4_composed_anchor_without_a_ledger_row_fails(tmp_path: Path) -> None:
    """The `^lanes-no-ack` shape: an obligation composed into doctrine, unrecorded."""
    root = _workspace(tmp_path, ledger_rows="| ^alpha | retained/compressed |\n")
    result = _lint(root, tmp_path)
    findings = [f for f in result.findings if f.check == "C4"]
    assert [f.subject for f in findings] == ["^beta"]
    assert "playbook:demo" in findings[0].detail
    assert exit_code_for(result, "migration", "warn") == EXIT_COVERAGE


def test_c4_duplicate_row_must_name_a_surviving_authority(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        ledger_rows="| ^alpha | duplicate → ^ghost |\n| ^beta | retained/compressed |\n",
    )
    assert [f.subject for f in _lint(root, tmp_path).findings if f.check == "C4"] == ["^alpha"]

    root = _workspace(
        tmp_path / "ok",
        ledger_rows="| ^alpha | duplicate → ^beta |\n| ^beta | retained/compressed |\n",
    )
    assert [f for f in _lint(root, tmp_path).findings if f.check == "C4"] == []


def test_c4_stale_row_may_cite_an_adjudicated_retirement(tmp_path: Path) -> None:
    """AC3's other branch: an explicit retirement is a successor-free row that passes."""
    root = _workspace(
        tmp_path,
        ledger_rows="| ^alpha | stale → superseded box list (deleted; B4-3) |\n| ^beta | retained/compressed |\n",
    )
    assert [f for f in _lint(root, tmp_path).findings if f.check == "C4"] == []


def test_c4_rejects_an_unrecognized_disposition(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path, ledger_rows="| ^alpha | handled somehow |\n| ^beta | retained/compressed |\n"
    )
    findings = [f for f in _lint(root, tmp_path).findings if f.check == "C4"]
    assert any("unrecognized disposition" in f.detail for f in findings)


def test_the_totals_table_is_not_read_as_ledger_rows() -> None:
    text = (
        "## Totals\n\n| Surface | Before | After |\n|---|---:|---:|\n| Composed | 1 | 2 |\n\n"
        "## Appendix 1\n\n| Unit (label) | Disposition |\n|---|---|\n| ^alpha | retained/compressed |\n"
    )
    assert [row.label for row in parse_ledger(text)] == ["^alpha"]


# --------------------------------------------------------------------------
# C5 — exception validity
# --------------------------------------------------------------------------
def test_c5_a_valid_exception_downgrades_over_to_excepted(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        section="x" * 60_000 + "\n^alpha\n",
        ledger_rows="| ^alpha | retained/compressed |\n",
        exceptions='[[exception]]\nname = "playbook:demo"\nuntil = 2026-12-01\nreason = "r"\n',
    )
    result = _lint(root, tmp_path)
    assert [row.verdict for row in result.measurements if row.name == "playbook:demo"] == [
        "EXCEPTED"
    ]
    assert exit_code_for(result, "migration", "block") == EXIT_OK


def test_c5_an_expired_exception_fails_instead_of_excusing(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        section="x" * 60_000 + "\n^alpha\n",
        ledger_rows="| ^alpha | retained/compressed |\n",
        exceptions='[[exception]]\nname = "playbook:demo"\nuntil = 2026-01-01\nreason = "r"\n',
    )
    result = _lint(root, tmp_path)
    assert [f.check for f in result.findings if f.check == "C5"] == ["C5"]
    assert exit_code_for(result, "migration", "warn") == EXIT_COVERAGE


# --------------------------------------------------------------------------
# exit codes — AC10, the defect the shell script shipped with
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["warn", "block"])
def test_a_missing_manifest_exits_two_in_either_mode(tmp_path: Path, mode: str) -> None:
    root = _workspace(tmp_path)
    (root / "doctrine/manifests/orchestrator.toml").unlink()
    result = _run_cli(root, tmp_path, "--mode", mode)
    assert result.exit_code == EXIT_INTERNAL, result.output
    assert "FATAL" in result.output


@pytest.mark.parametrize("mode", ["warn", "block"])
def test_a_composer_crash_exits_two_in_either_mode(tmp_path: Path, mode: str) -> None:
    root = _workspace(tmp_path)
    (root / "doctrine/compose/compose.py").write_text(
        "import sys\nsys.stderr.write('boom\\n')\nsys.exit(9)\n", encoding="utf-8"
    )
    result = _run_cli(root, tmp_path, "--mode", mode)
    assert result.exit_code == EXIT_INTERNAL, result.output
    assert "compose failed" in result.output


def test_an_unmounted_scratch_root_is_an_internal_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CAO_SCRATCH_ROOT", str(tmp_path / "not-mounted"))
    root = _workspace(tmp_path)
    result = CliRunner().invoke(lint_doctrine, ["--workspace", str(root)])
    assert result.exit_code == EXIT_INTERNAL
    assert "not mounted" in result.output


def test_the_cli_exits_zero_and_reports_clean_on_a_good_workspace(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    result = _run_cli(root, tmp_path)
    assert result.exit_code == EXIT_OK, result.output
    assert "clean (phase=migration, mode=warn)" in result.output
    assert "playbook:demo" in result.output


def test_precedence_internal_beats_coverage_beats_budget(tmp_path: Path) -> None:
    both = LintResult(
        findings=[Finding("C1", "p", "over", "budget"), Finding("C4", "^x", "no row")]
    )
    assert exit_code_for(both, "migration", "block") == EXIT_COVERAGE
    budget_only = LintResult(findings=[Finding("C1", "p", "over", "budget")])
    assert exit_code_for(budget_only, "migration", "block") == EXIT_BUDGET
    assert exit_code_for(LintResult(), "migration", "block") == EXIT_OK


# --------------------------------------------------------------------------
# mutation arms — each replays a defect and must come back RED
# --------------------------------------------------------------------------
def test_mutant_warn_mode_swallowing_internal_errors_turns_red(tmp_path: Path) -> None:
    """`doctrine-budget.sh:69`: `die()` exited 0 in warn mode, so a crash read as a pass."""

    def mutant_exit(exc: LintError, mode: str) -> int:
        return EXIT_INTERNAL if mode == "block" else EXIT_OK

    root = _workspace(tmp_path)
    (root / "doctrine/manifests/orchestrator.toml").unlink()
    with pytest.raises(LintError) as raised:
        _lint(root, tmp_path)
    assert (
        mutant_exit(raised.value, "block") == EXIT_INTERNAL
    ), "fixture did not reproduce the defect"
    with pytest.raises(AssertionError):
        assert mutant_exit(raised.value, "warn") == EXIT_INTERNAL


def test_mutant_dropping_c3_from_the_exit_code_turns_red(tmp_path: Path) -> None:
    """A coverage check nothing acts on is documentation, not a gate."""
    root = _workspace(
        tmp_path,
        ledger_rows="| ^alpha | mechanism EXISTS [queue-native-seat-carrier] |\n| ^beta | retained/compressed |\n",
    )
    result = _lint(root, tmp_path)
    assert exit_code_for(result, "migration", "warn") == EXIT_COVERAGE

    mutant = LintResult(
        measurements=result.measurements,
        findings=[f for f in result.findings if f.check != "C3"],
    )
    with pytest.raises(AssertionError):
        assert exit_code_for(mutant, "migration", "warn") == EXIT_COVERAGE


def test_mutant_off_by_one_ceiling_turns_red(tmp_path: Path) -> None:
    """A composed output one byte over the ceiling is OVER, not OK."""
    limit = PHASE_LIMITS["migration"][0]
    body = "^alpha\n"
    root = _workspace(tmp_path, section="x" * (limit + 1 - len(body)) + body)
    composed = [row for row in _lint(root, tmp_path).measurements if row.name == "playbook:demo"][0]
    assert composed.size == limit + 1, composed
    assert composed.verdict == "OVER"

    with pytest.raises(AssertionError):
        assert _mutant_verdict(composed.size, limit + 1) == "OVER"


def _mutant_verdict(size: int, limit: int) -> str:
    return "OK" if size <= limit else "OVER"


def test_mutant_not_regenerating_the_inventory_turns_red(tmp_path: Path) -> None:
    """AC9: the inventory is generated. A committed copy nobody refreshes must fail."""
    root = _workspace(tmp_path)
    committed = root / "orchestrator/mechanism-inventory.md"
    _lint(root, tmp_path, inventory_out=committed)
    (root / "scripts/gated-merge.sh").write_text(
        'readonly CODES="E-DEMO-TOKEN"\n# moved\n', "utf-8"
    )
    (root / "doctrine/MIGRATION-F809.md").write_text(
        LEDGER_HEAD
        + "| ^alpha | retained/compressed |\n| ^beta | mechanism EXISTS [E-DEMO-TOKEN; E-OTHER] |\n",
        encoding="utf-8",
    )
    (root / "scripts/report-attest.sh").write_text('CODES="E-OTHER"\n', encoding="utf-8")
    result = _lint(root, tmp_path, committed_inventory=committed)
    assert [f for f in result.findings if "stale" in f.detail], "fixture did not go stale"

    mutant = LintResult(findings=[f for f in result.findings if "stale" not in f.detail])
    with pytest.raises(AssertionError):
        assert [f for f in mutant.findings if "stale" in f.detail]


def test_mutant_resolving_ids_against_the_skill_cli_turns_red() -> None:
    """Widening the scan to the command that cites the ids makes C3 self-satisfying.

    Fed the linter's OWN source, ``resolve_mechanisms`` happily resolves FROZEN-PIN
    from the docstring. Only the exclusion keeps that file out of the file list.
    """
    from cli_agent_orchestrator.cli.orchestrator_commands import lint_doctrine as module

    own_source = Path(module.__file__)
    assert "FROZEN-PIN" in own_source.read_text(encoding="utf-8"), "fixture lost its bait"
    mutant = resolve_mechanisms(
        ["FROZEN-PIN"], [("cli/orchestrator_commands/lint_doctrine.py", "runtime", own_source)]
    )
    with pytest.raises(AssertionError):
        assert mutant["FROZEN-PIN"].kind == "UNRESOLVED"


def test_render_inventory_is_pure_and_sorted() -> None:
    from cli_agent_orchestrator.cli.orchestrator_commands.lint_doctrine import Resolution

    rows = {
        "b-id": Resolution("b-id", "hook", "path:2"),
        "a-id": Resolution("a-id", "UNRESOLVED", ""),
    }
    text = render_inventory(rows, {"b-id": ["x:1"]})
    assert text.index("`a-id`") < text.index("`b-id`")
    assert "| `a-id` | UNRESOLVED | — | — |" in text
    assert render_inventory(rows, {"b-id": ["x:1"]}) == text
