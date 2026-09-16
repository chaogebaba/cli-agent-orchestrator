"""F809 #666 ASK A06 (``#668``) — ``cao-orchestrator evidence verify``.

Every check has an EXECUTED arm in both directions: the allowed action and the
true violation, over a real git repository on disk and real files, driven through
the real Click command.  Nothing is asserted by inspecting source.

The fixture builds a two-commit repository and an attested report whose digest is
computed the way ``scripts/report-attest.sh`` computes it — and one arm proves
that equivalence against ``grep``/``sha256sum`` themselves rather than trusting
the reimplementation.

The mutation arms replay the defects this command exists not to have: an exit
code that ignores its own blocking findings, a digest that covers the
attestation line it is supposed to elide, a staleness check that accepts a
commit it cannot resolve, an impact matrix that finds no providers, and the
warn-mode ``die()`` that let an internal error read as a pass.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import subprocess
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.orchestrator_commands.evidence import (
    DEFAULT_MAX_AGE_HOURS,
    EXIT_INTERNAL,
    EXIT_INVALID,
    EXIT_OK,
    EvidenceError,
    Finding,
    VerifyResult,
    canonical_report_digest,
    check_ledger,
    evidence,
    exit_code_for,
    providers_touched,
    render_matrix,
    run_verify,
)

NOW = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc)
FRESH = (NOW - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
STALE = (NOW - dt.timedelta(hours=DEFAULT_MAX_AGE_HOURS + 5)).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _attest(report: Path, body: str) -> str:
    """Write ``body`` and insert the CRD line, as ``report-attest.sh`` would."""
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(body, encoding="utf-8")
    digest = canonical_report_digest(report)
    report.write_text(f"**Report-SHA256:** {digest}\n{body}", encoding="utf-8")
    return digest


class World:
    """A repo, a report, a ledger and a manifest — all consistent to start with."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        (self.root / "orchestrator").mkdir(parents=True)
        (self.root / "providers").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        _git(self.root, "config", "user.email", "lane@example.invalid")
        _git(self.root, "config", "user.name", "lane")
        (self.root / "providers/claude_code.py").write_text("SPAWN = 1\n", encoding="utf-8")
        (self.root / "orchestrator/HANDOFF.md").write_text("# handoff\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "base")
        self.base = _git(self.root, "rev-parse", "HEAD")

        (self.root / "providers/claude_code.py").write_text("SPAWN = 2\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "head")
        self.head = _git(self.root, "rev-parse", "HEAD")

        self.out = tmp_path / "out"
        self.out.mkdir()
        self.report = self.out / "report.md"
        _attest(self.report, "**Ruling:** GATE-YES\n\nbody\n")
        self.suite_log = self.out / "suite.log"
        self.suite_log.write_text("812 passed\n", encoding="utf-8")
        self.live_log = self.out / "live.log"
        self.live_log.write_text("Prepared 3 MCP servers\n", encoding="utf-8")
        self.dependency = self.out / "ledger-empirical-r1.md"
        self.dependency.write_text("# stage A ledger\n", encoding="utf-8")

        self.ledger_row = f"| F811 | live | owning commit {self.head} | pending |\n"
        self._write_ledger()
        self.manifest = tmp_path / "evidence.toml"
        self.write()

    def _write_ledger(self) -> None:
        (self.root / "orchestrator/HANDOFF.md").write_text(
            "# handoff\n\n## Live ledger\n\n" + self.ledger_row, encoding="utf-8"
        )

    def sha(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def body(self, **over: str) -> str:
        parts = {
            "evidence": textwrap.dedent(f"""
                [evidence]
                feature = "F811"
                repo = "fork"
                base = "{self.base}"
                head = "{self.head}"
                report = "{self.report}"
                """),
            "suite": textwrap.dedent(f"""
                [[suite]]
                name = "paired suite (head)"
                executor = "box"
                command = "uv run pytest -m 'not e2e and not slow'"
                tree = "{self.head}"
                exit_code = 0
                counts = "812 passed, 3 skipped"
                log = "{self.suite_log}"
                load_bearing = true
                cache = "bypassed"
                lease = "0f3ad21c"
                """),
            "static": textwrap.dedent("""
                [static.mypy]
                base = ["src/a.py:1: error: x"]
                head = ["src/a.py:1: error: x"]
                [static.lint_imports]
                exit_code = 0
                contract = "skill-cli-only-via-public-api"
                """),
            "live_round": textwrap.dedent(f"""
                [[live_round]]
                provider = "claude_code"
                box = "grok-box-002"
                lease = "0f3ad21c"
                callback = "own MCP send_message"
                log = "{self.live_log}"
                """),
            "depends_on": textwrap.dedent(f"""
                [[depends_on]]
                kind = "ledger-pin"
                path = "{self.dependency}"
                sha256 = "{self.sha(self.dependency)}"
                """),
        }
        parts.update(over)
        return "\n".join(value for value in parts.values() if value)

    def write(self, **over: str) -> Path:
        self.manifest.write_text(self.body(**over), encoding="utf-8")
        return self.manifest

    def verify(self, *, now: dt.datetime = NOW) -> VerifyResult:
        return run_verify(self.manifest, root=self.root, repo=self.root, now=now)

    def blocking(self) -> list[Finding]:
        return [f for f in self.verify().findings if f.blocking]

    def details(self) -> str:
        return " ".join(f"{f.subject} {f.detail}" for f in self.verify().findings)


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# --------------------------------------------------------------------------
# the allowed action
# --------------------------------------------------------------------------


def test_a_consistent_manifest_verifies_clean(world: World) -> None:
    result = world.verify()
    assert [f for f in result.findings if f.blocking] == [], result.findings
    assert exit_code_for(result, "block") == EXIT_OK


def test_clean_run_exits_zero_through_the_real_command(world: World) -> None:
    out = CliRunner().invoke(
        evidence,
        ["verify", str(world.manifest), "--workspace", str(world.root)],
    )
    assert out.exit_code == EXIT_OK, out.output
    assert "clean" in out.output


# --------------------------------------------------------------------------
# E1/E2 — report present/absent, hash match/mismatch
# --------------------------------------------------------------------------


def test_missing_report_blocks(world: World) -> None:
    world.report.unlink()
    assert "E-REPORT-MISSING" in world.details()


def test_relative_report_path_blocks(world: World) -> None:
    world.write(
        evidence=world.body().split("\n[[suite]]")[0].replace(str(world.report), "report.md")
    )
    assert "path is not absolute" in world.details()


def test_unattested_report_blocks(world: World) -> None:
    world.report.write_text("**Ruling:** GATE-YES\n", encoding="utf-8")
    assert "E-REPORT-UNATTESTED" in world.details()


def test_edited_report_blocks_on_drift(world: World) -> None:
    world.report.write_text(
        world.report.read_text(encoding="utf-8") + "a later edit\n", encoding="utf-8"
    )
    assert "E-REPORT-DRIFT" in world.details()


def test_digest_matches_report_attest_sh_byte_for_byte(world: World) -> None:
    """Parity with ``grep -Ev … | sha256sum``, the algorithm in the shell script.

    Asserted against the real utilities rather than the reimplementation, and for
    a file with no trailing newline — the case where a naive reimplementation and
    ``grep`` disagree.
    """
    for body in (
        "**Report-SHA256:** deadbeef\nline\n",
        "Report-SHA256: deadbeef\nno trailing newline",
        "**Ruling:** GATE-YES\n",
    ):
        target = world.out / "parity.md"
        target.write_text(body, encoding="utf-8")
        shell = subprocess.run(
            "grep -Ev '^(\\*\\*)?Report-SHA256(:\\*\\*|:)[[:space:]]' "
            f"{target} | sha256sum | cut -d' ' -f1",
            shell=True,
            capture_output=True,
            text=True,
        )
        assert canonical_report_digest(target) == shell.stdout.strip(), body


def test_drifted_pinned_artifact_blocks(world: World) -> None:
    blueprint = world.root / "orchestrator/blueprint.md"
    blueprint.write_text("r1\n", encoding="utf-8")
    pinned = world.sha(blueprint)
    world.write(artifact=f'[[artifact]]\npath = "orchestrator/blueprint.md"\nsha256 = "{pinned}"\n')
    assert world.blocking() == []
    blueprint.write_text("r2\n", encoding="utf-8")
    assert "E-ARTIFACT-DRIFT" in world.details()


def test_absent_pinned_artifact_blocks(world: World) -> None:
    world.write(
        artifact='[[artifact]]\npath = "orchestrator/gone.md"\nsha256 = "' + "0" * 64 + '"\n'
    )
    assert "E-ARTIFACT-ABSENT" in world.details()


# --------------------------------------------------------------------------
# E3 — ledger row present/stale
# --------------------------------------------------------------------------


def test_ledger_row_absent_blocks(world: World) -> None:
    (world.root / "orchestrator/HANDOFF.md").write_text("# handoff\n", encoding="utf-8")
    assert "E-LEDGER-ROW-ABSENT" in world.details()


def test_ledger_row_without_a_commit_is_stale(world: World) -> None:
    world.ledger_row = "| F811 | live | owning commit: TBD | pending |\n"
    world._write_ledger()
    assert "E-LEDGER-ROW-STALE" in world.details()
    assert "cites no owning commit" in world.details()


def test_ledger_row_citing_an_unreachable_commit_is_stale(world: World) -> None:
    """A row that names a commit from some other line of history is not evidence."""
    world.ledger_row = f"| F811 | live | owning commit {'b' * 40} | pending |\n"
    world._write_ledger()
    assert "E-LEDGER-ROW-STALE" in world.details()


def test_ledger_row_citing_an_ancestor_is_accepted(world: World) -> None:
    """The obligation may have been recorded at the base of the range."""
    world.ledger_row = f"| F811 | live | owning commit {world.base} | pending |\n"
    world._write_ledger()
    assert world.blocking() == [], world.blocking()


# --------------------------------------------------------------------------
# E4 — declared dependency satisfied/missing
# --------------------------------------------------------------------------


def test_missing_dependency_blocks(world: World) -> None:
    world.dependency.unlink()
    assert "E-DEPENDENCY-MISSING" in world.details()


def test_unpinned_dependency_blocks(world: World) -> None:
    world.write(depends_on=f'[[depends_on]]\nkind = "ledger-pin"\npath = "{world.dependency}"\n')
    assert "E-DEPENDENCY-UNPINNED" in world.details()


def test_drifted_dependency_blocks(world: World) -> None:
    world.dependency.write_text("# stage A ledger, edited\n", encoding="utf-8")
    assert "E-DEPENDENCY-DRIFT" in world.details()


# --------------------------------------------------------------------------
# E5 — executor predicates, tree state, cache, CI attestation
# --------------------------------------------------------------------------


def test_box_run_without_a_lease_blocks(world: World) -> None:
    world.manifest.write_text(world.body().replace('lease = "0f3ad21c"\n', "", 1), encoding="utf-8")
    assert "declares no grokfleet lease" in world.details()


def test_ci_run_without_an_exhausted_fleet_blocks(world: World) -> None:
    world.write(suite=textwrap.dedent(f"""
            [[suite]]
            name = "ci"
            executor = "ci"
            command = "pytest"
            tree = "{world.head}"
            counts = "812 passed"
            signature = "sha256:abc"
            workflow = "ci.yml"
            derived_from = "{world.head}"
            conclusion = "success"
            attested_at = "{FRESH}"
            """))
    assert "CI is the FALLBACK executor" in world.details()


def test_local_run_without_a_predicate_blocks(world: World) -> None:
    world.write(suite=textwrap.dedent(f"""
            [[suite]]
            name = "local"
            executor = "local"
            command = "make test-full"
            tree = "{world.head}"
            exit_code = 0
            counts = "812 passed"
            log = "{world.suite_log}"
            """))
    assert "local run needs fleet_exhausted" in world.details()


def test_local_run_with_a_live_env_reason_is_accepted(world: World) -> None:
    world.write(suite=textwrap.dedent(f"""
            [[suite]]
            name = "local"
            executor = "local"
            command = "make test-full"
            tree = "{world.head}"
            exit_code = 0
            counts = "812 passed"
            log = "{world.suite_log}"
            local_reason = "needs the live local fleet"
            """))
    assert world.blocking() == [], world.blocking()


def test_run_covering_a_third_tree_blocks(world: World) -> None:
    body = world.body().replace(f'tree = "{world.head}"', f'tree = "{"c" * 40}"')
    world.manifest.write_text(body, encoding="utf-8")
    assert "E-TREE-MISMATCH" in world.details()


def test_load_bearing_cache_hit_blocks(world: World) -> None:
    body = world.body().replace('cache = "bypassed"', 'cache = "hit"')
    world.manifest.write_text(body, encoding="utf-8")
    assert "E-CACHE-UNSIGNED" in world.details()


def test_missing_suite_log_blocks(world: World) -> None:
    world.suite_log.unlink()
    assert "E-LOG-MISSING" in world.details()


def test_manifest_with_no_suite_blocks(world: World) -> None:
    world.write(suite="")
    assert "declares no [[suite]] run" in world.details()


def _ci_suite(world: World, attested_at: str = FRESH) -> str:
    return textwrap.dedent(f"""
        [[suite]]
        name = "ci"
        executor = "ci"
        command = "pytest"
        fleet_exhausted = true
        tree = "{world.head}"
        counts = "812 passed"
        signature = "sha256:abc"
        workflow = "ci.yml"
        derived_from = "{world.head}"
        conclusion = "success"
        attested_at = "{attested_at}"
        """)


def test_a_fully_structured_ci_attestation_is_accepted(world: World) -> None:
    world.write(suite=_ci_suite(world))
    assert world.blocking() == [], world.blocking()


@pytest.mark.parametrize(
    "code,old,new",
    [
        ("E-ATTEST-TREE", 'tree = "{head}"', 'tree = "{base}"'),
        ("E-ATTEST-SIGNATURE", 'signature = "sha256:abc"\n', ""),
        ("E-ATTEST-PREDICATE", 'workflow = "ci.yml"\n', ""),
        ("E-ATTEST-CHAIN", 'derived_from = "{head}"', 'derived_from = "' + "d" * 40 + '"'),
        ("E-ATTEST-RESULT", 'conclusion = "success"', 'conclusion = "failure"'),
        ("E-ATTEST-RESULT", 'counts = "812 passed"\n', ""),
    ],
)
def test_each_attestation_leg_blocks_on_its_own(
    world: World, code: str, old: str, new: str
) -> None:
    suite = _ci_suite(world).replace(
        old.format(head=world.head, base=world.base), new.format(head=world.head, base=world.base)
    )
    world.write(suite=suite)
    assert code in world.details(), world.details()


def test_an_expired_attestation_blocks(world: World) -> None:
    world.write(suite=_ci_suite(world, attested_at=STALE))
    assert "E-ATTEST-AGE" in world.details()


def test_a_run_url_alone_is_never_sufficient(world: World) -> None:
    """The whole point of the six legs: a plausible URL proves nothing."""
    world.write(suite=textwrap.dedent(f"""
            [[suite]]
            name = "ci"
            executor = "ci"
            command = "pytest"
            fleet_exhausted = true
            tree = "{world.head}"
            run_url = "https://github.com/o/r/actions/runs/1"
            """))
    details = world.details()
    for code in ("E-ATTEST-SIGNATURE", "E-ATTEST-PREDICATE", "E-ATTEST-CHAIN", "E-ATTEST-RESULT"):
        assert code in details, details


# --------------------------------------------------------------------------
# E6/E7 — static diagnostics and FAILED sets: compared here, judged in prose
# --------------------------------------------------------------------------


def test_unrecorded_lint_imports_blocks(world: World) -> None:
    world.write(static="[static.mypy]\nbase = []\nhead = []\n")
    assert "records no [static.lint_imports] result" in world.details()


def test_failing_lint_imports_blocks(world: World) -> None:
    body = world.body().replace(
        "[static.lint_imports]\nexit_code = 0", "[static.lint_imports]\nexit_code = 1"
    )
    world.manifest.write_text(body, encoding="utf-8")
    assert "lint-imports exited 1" in world.details()


def test_unrecorded_mypy_compare_blocks(world: World) -> None:
    world.write(static="[static.lint_imports]\nexit_code = 0\n")
    assert "declares no [static.mypy]" in world.details()


def test_a_new_mypy_diagnostic_warns_and_never_blocks(world: World) -> None:
    """Regression-or-pre-existing is the reviewer's call; the compare is not."""
    body = world.body().replace(
        'head = ["src/a.py:1: error: x"]',
        'head = ["src/a.py:1: error: x", "src/b.py:2: error: y"]',
    )
    world.manifest.write_text(body, encoding="utf-8")
    result = world.verify()
    warned = [f for f in result.findings if not f.blocking and f.check == "E6"]
    assert warned and "src/b.py:2" in warned[0].detail
    assert [f for f in result.findings if f.blocking] == []
    assert exit_code_for(result, "block") == EXIT_OK


def test_an_unexplained_head_only_failure_warns_and_never_blocks(world: World) -> None:
    world.write(failed_set=textwrap.dedent("""
            [[failed_set]]
            test = "test/services/test_x.py::test_y"
            base = "pass"
            head = "fail"
            """))
    result = world.verify()
    assert [f for f in result.findings if f.blocking] == []
    assert any("adjudicate" in f.detail for f in result.findings)
    assert exit_code_for(result, "block") == EXIT_OK


def test_an_explained_head_only_failure_is_silent(world: World) -> None:
    world.write(failed_set=textwrap.dedent("""
            [[failed_set]]
            test = "test/services/test_x.py::test_y"
            base = "pass"
            head = "fail"
            explanation = "F785 flake family"
            """))
    assert world.verify().findings == []


def test_a_malformed_outcome_blocks(world: World) -> None:
    world.write(failed_set='[[failed_set]]\ntest = "t"\nbase = "green"\nhead = "fail"\n')
    assert "outcomes must be" in world.details()


# --------------------------------------------------------------------------
# E8 — the GENERATED change-impact matrix
# --------------------------------------------------------------------------


def test_touched_provider_without_a_live_round_blocks(world: World) -> None:
    world.write(live_round="")
    assert "E-MATRIX-MISSING" in world.details()
    assert "claude_code" in world.details()


def test_matrix_is_generated_from_the_diff_not_the_manifest(world: World) -> None:
    result = world.verify()
    assert result.matrix["claude_code"].startswith("required")
    assert "| claude_code |" in render_matrix(result)


def test_provider_path_classes_are_recognised() -> None:
    touched, shared = providers_touched(
        [
            "src/cli_agent_orchestrator/providers/codex.py",
            "src/cli_agent_orchestrator/providers/kiro_capabilities.py",
            "src/cli_agent_orchestrator/agent_store/templates/pi_cli.toml",
            "docs/readme.md",
        ]
    )
    assert touched == {"codex", "kiro", "pi_cli"}
    assert not shared

    _, shared = providers_touched(["src/cli_agent_orchestrator/services/terminal_service.py"])
    assert shared


def test_live_round_without_its_own_callback_blocks(world: World) -> None:
    body = world.body().replace('callback = "own MCP send_message"\n', "")
    world.manifest.write_text(body, encoding="utf-8")
    assert "records no OWN callback" in world.details()


def test_fleet_down_waiver_needs_a_substitute_round(world: World) -> None:
    world.write(live_round="", live_round_waiver="[live_round_waiver]\nfleet_down = true\n")
    assert "needs a substitute round" in world.details()


def test_fleet_down_waiver_with_a_g7_round_is_accepted(world: World) -> None:
    g7 = world.out / "g7.log"
    g7.write_text("sandbox round\n", encoding="utf-8")
    world.write(
        live_round="",
        live_round_waiver=(
            f'[live_round_waiver]\nfleet_down = true\nsubstitute = "g7-sandbox"\nlog = "{g7}"\n'
        ),
    )
    assert world.blocking() == [], world.blocking()
    assert world.verify().matrix["claude_code"] == "waived (fleet down)"


# --------------------------------------------------------------------------
# exit codes and the absence of a bypass
# --------------------------------------------------------------------------


def test_block_mode_exits_one_on_invalid_evidence(world: World) -> None:
    world.report.unlink()
    out = CliRunner().invoke(
        evidence, ["verify", str(world.manifest), "--workspace", str(world.root)]
    )
    assert out.exit_code == EXIT_INVALID, out.output
    assert "BLOCK" in out.output


def test_warn_mode_reports_the_same_findings_but_exits_zero(world: World) -> None:
    world.report.unlink()
    out = CliRunner().invoke(
        evidence,
        ["verify", str(world.manifest), "--workspace", str(world.root), "--mode", "warn"],
    )
    assert out.exit_code == EXIT_OK, out.output
    assert "E-REPORT-MISSING" in out.output
    assert "BLOCK" not in out.output


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda w: w.manifest.unlink(), "not a regular file"),
        (lambda w: w.manifest.write_text("[evidence\n", encoding="utf-8"), "not valid TOML"),
        (
            lambda w: w.manifest.write_text("[other]\nx = 1\n", encoding="utf-8"),
            "no [evidence] table",
        ),
        (
            lambda w: w.manifest.write_text(
                w.body().replace(f'head = "{w.head}"', 'head = "abc"'), encoding="utf-8"
            ),
            "not a full 40-hex sha",
        ),
        (
            lambda w: w.manifest.write_text(
                w.body().replace(f'head = "{w.head}"', f'head = "{"e" * 40}"'), encoding="utf-8"
            ),
            "is not a commit",
        ),
    ],
)
def test_structural_failures_always_exit_two(world: World, mutate, fragment: str) -> None:
    """Never 0 on an internal error — the defect that motivated the exit-code table."""
    mutate(world)
    out = CliRunner().invoke(
        evidence, ["verify", str(world.manifest), "--workspace", str(world.root)]
    )
    assert out.exit_code == EXIT_INTERNAL, out.output
    assert fragment in out.output


def test_warn_mode_does_not_downgrade_an_internal_error(world: World) -> None:
    """The one bypass that must not exist: --mode warn is not --ignore-errors."""
    world.manifest.unlink()
    out = CliRunner().invoke(
        evidence,
        ["verify", str(world.manifest), "--workspace", str(world.root), "--mode", "warn"],
    )
    assert out.exit_code == EXIT_INTERNAL, out.output


def test_a_non_repo_workspace_exits_two(world: World, tmp_path: Path) -> None:
    out = CliRunner().invoke(
        evidence,
        [
            "verify",
            str(world.manifest),
            "--workspace",
            str(world.root),
            "--repo-path",
            str(tmp_path / "out"),
        ],
    )
    assert out.exit_code == EXIT_INTERNAL, out.output
    assert "not a git work tree" in out.output


def test_emit_matrix_writes_the_generated_table(world: World, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "matrix.md"
    out = CliRunner().invoke(
        evidence,
        [
            "verify",
            str(world.manifest),
            "--workspace",
            str(world.root),
            "--emit-matrix",
            str(target),
        ],
    )
    assert out.exit_code == EXIT_OK, out.output
    assert "| claude_code |" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# mutation arms — each must come back RED
# --------------------------------------------------------------------------


def test_mutant_exit_code_ignoring_blocking_findings_turns_red(world: World) -> None:
    """A check nothing acts on is documentation, not a gate."""
    world.report.unlink()
    result = world.verify()
    assert exit_code_for(result, "block") == EXIT_INVALID, "fixture did not reproduce the defect"

    mutant = VerifyResult(findings=[f for f in result.findings if not f.blocking])
    with pytest.raises(AssertionError):
        assert exit_code_for(mutant, "block") == EXIT_INVALID


def test_mutant_digest_covering_the_attestation_line_turns_red(world: World) -> None:
    """Hash the whole file and every correctly attested report reads as drifted."""
    naive = hashlib.sha256(world.report.read_bytes()).hexdigest()
    declared = world.report.read_text(encoding="utf-8").splitlines()[0].split()[-1]
    assert canonical_report_digest(world.report) == declared, "fixture is not attested"

    with pytest.raises(AssertionError):
        assert naive == declared


def test_mutant_staleness_accepting_an_unresolvable_commit_turns_red(world: World) -> None:
    """Skipping a commit git cannot resolve is how a stale row passes."""
    world.ledger_row = f"| F811 | live | owning commit {'b' * 40} | pending |\n"
    world._write_ledger()
    real = VerifyResult()
    check_ledger(
        {"evidence": {"feature": "F811", "head": world.head}}, world.root, world.root, real
    )
    assert any("E-LEDGER-ROW-STALE" in f.detail for f in real.findings)

    mutant = VerifyResult(
        findings=[f for f in real.findings if "E-LEDGER-ROW-STALE" not in f.detail]
    )
    with pytest.raises(AssertionError):
        assert any("E-LEDGER-ROW-STALE" in f.detail for f in mutant.findings)


def test_mutant_matrix_that_finds_no_providers_turns_red(world: World) -> None:
    """A matrix generated from an empty path-class list waives every provider."""
    world.write(live_round="")
    assert "E-MATRIX-MISSING" in world.details(), "fixture did not reproduce the defect"

    def mutant_providers(paths):
        return set(), False

    touched, _ = mutant_providers(["src/cli_agent_orchestrator/providers/claude_code.py"])
    with pytest.raises(AssertionError):
        assert touched == {"claude_code"}


def test_mutant_internal_error_exiting_zero_turns_red(world: World) -> None:
    """``doctrine-budget.sh:69`` shipped exactly this: a FATAL that exits 0."""

    def mutant_exit(exc: EvidenceError, mode: str) -> int:
        return EXIT_INTERNAL if mode == "block" else EXIT_OK

    world.manifest.unlink()
    with pytest.raises(EvidenceError) as raised:
        world.verify()
    assert mutant_exit(raised.value, "block") == EXIT_INTERNAL, "fixture did not reproduce"
    with pytest.raises(AssertionError):
        assert mutant_exit(raised.value, "warn") == EXIT_INTERNAL
