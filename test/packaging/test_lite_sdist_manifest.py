"""AC-LITE-5 — a built artifact carries no skill knowledge.

``wp-arch-modular-core.md`` A.5: "A built sdist and wheel contain no file under
``blueprints/``, ``orchestrator/``, or ``doctrine/``, and no ``*-build-report.md``."

Slice 3 adds the ``[tool.hatch.build.targets.sdist]`` manifest that makes that true. Before
it, hatchling's default swept in everything git tracks, which in this repository includes the
orchestrator skill's own knowledge sitting next to the source: 43 files, measured at
``ad4339e9``. No repository file is deleted — only archive membership changes.

The artifacts are BUILT here rather than inspected as a member list, because the manifest is
a build-backend contract and only the backend can say what it actually does with it. The pair
of builds costs about three seconds, so the test stays in the default selection instead of
hiding behind a marker that CI deselects.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path
from test.helpers.knowledge_io_scan import Finding, scan_artifact, scan_members

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# What the manifest must keep out. Anchored entries are project-root relative.
EXPECTED_EXCLUDES = {"/blueprints", "/orchestrator", "/doctrine", "*-build-report.md"}


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Build the real sdist and wheel once for the module."""
    if shutil.which("uv") is None:  # pragma: no cover - uv is the project's toolchain
        pytest.skip("uv is required to build the artifacts")
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"uv build failed:\n{result.stdout}\n{result.stderr}"
    sdists = list(out.glob("*.tar.gz"))
    wheels = list(out.glob("*.whl"))
    assert len(sdists) == 1 and len(wheels) == 1, sorted(p.name for p in out.iterdir())
    return sdists[0], wheels[0]


def test_sdist_manifest_is_declared_and_anchored() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sdist = data["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert set(sdist["exclude"]) == EXPECTED_EXCLUDES
    for entry in sdist["exclude"]:
        # Either anchored at the project root, or a bare filename glob — never an
        # unanchored directory name, which would also strip `src/.../doctrine/`.
        assert entry.startswith("/") or entry.startswith("*"), entry


def test_sdist_carries_no_skill_knowledge(built_artifacts: tuple[Path, Path]) -> None:
    sdist, _ = built_artifacts
    findings = scan_artifact(sdist)
    assert findings == [], _render(findings)


def test_wheel_carries_no_skill_knowledge(built_artifacts: tuple[Path, Path]) -> None:
    (wheel,) = (built_artifacts[1],)
    findings = scan_artifact(wheel)
    assert findings == [], _render(findings)


def test_sdist_still_ships_what_the_project_needs(built_artifacts: tuple[Path, Path]) -> None:
    """The manifest must not have taken source, tests or packaging metadata with it.

    An exclude list that quietly stripped `src/` would also pass every assertion above,
    so the archive's usefulness is asserted directly.
    """
    sdist, _ = built_artifacts
    with tarfile.open(sdist) as archive:
        names = {name.partition("/")[2] for name in archive.getnames()}
    for required in (
        "pyproject.toml",
        "README.md",
        "src/cli_agent_orchestrator/cli/main.py",
        "src/cli_agent_orchestrator/cli/orchestrator_main.py",
        "src/cli_agent_orchestrator/cli/orchestrator_commands/ledger.py",
        "src/cao_workflow/__init__.py",
        "test/architecture/test_no_knowledge_path_reads.py",
    ):
        assert required in names, f"sdist lost a file it needs: {required}"


def test_wheel_ships_only_the_two_source_packages(built_artifacts: tuple[Path, Path]) -> None:
    """The wheel needs no manifest entry, and this is why: `packages` already bounds it."""
    _, wheel = built_artifacts
    with zipfile.ZipFile(wheel) as archive:
        tops = {name.split("/")[0] for name in archive.namelist()}
    unexpected = {
        top
        for top in tops
        if top not in {"cli_agent_orchestrator", "cao_workflow"} and not top.endswith(".dist-info")
    }
    assert not unexpected, sorted(unexpected)


@pytest.mark.parametrize(
    "member",
    [
        "cli_agent_orchestrator-2.5.0/blueprints/wpm3-delivery-hardening.md",
        "cli_agent_orchestrator-2.5.0/orchestrator/build-reports/f497-build-report.md",
        "cli_agent_orchestrator-2.5.0/doctrine/orchestrator.md",
        "cli_agent_orchestrator-2.5.0/f640-build-report.md",
        "cli_agent_orchestrator-2.5.0/orchestrator/GOLDEN-TIPS.md",
    ],
)
def test_mutant_adding_a_knowledge_file_back_turns_red(member: str) -> None:
    """The AC-LITE-5 mutation arm: put one back and the scan must fire.

    Exercised against the member list rather than by editing pyproject and rebuilding,
    because what is under test is the scan's ability to SEE a re-added file — and it must
    see it whatever put it there, a loosened exclude or a stray package-data glob.
    """
    findings = scan_members([(member, b"# knowledge\n")], "sdist")
    assert findings, f"a re-added knowledge file went unnoticed: {member}"


def test_generic_product_docs_are_not_mistaken_for_knowledge() -> None:
    """The negative control. A.2: `docusaurus/docs/patterns/handoff.md` is a product doc.

    This fired before the matcher was made case-sensitive on the named files, which is why
    it is pinned: the skill's files are SHOUTED, the product's are not.
    """
    members = [
        ("cli_agent_orchestrator-2.5.0/docusaurus/docs/patterns/handoff.md", b"# Handoff\n"),
        ("cli_agent_orchestrator-2.5.0/docs/assets/handoff-workflow.mmd", b"graph TD\n"),
        (
            "cli_agent_orchestrator-2.5.0/src/cli_agent_orchestrator/services/agui/handoff_approval.py",
            b"",
        ),
        (
            "cli_agent_orchestrator-2.5.0/examples/ag-ui/ag-ui-handoff-approval/README.md",
            b"# Demo\n",
        ),
    ]
    assert scan_members(members, "sdist") == []


def test_packaged_prose_is_scanned_but_test_fixtures_are_not() -> None:
    """The prose rule is package-data only — in `test/` it is noise (slice 1's finding)."""
    packaged = [
        (
            "cli_agent_orchestrator-2.5.0/src/cli_agent_orchestrator/skills/x/SKILL.md",
            b"Consult the orchestrator doctrine before acting.\n",
        ),
    ]
    assert [row.rule for row in scan_members(packaged, "sdist")] == ["knowledge-content"]

    fixture = [
        (
            "cli_agent_orchestrator-2.5.0/test/providers/fixtures/f568/spinner-ebbing-bare.txt",
            b"reading GOLDEN-TIPS.md ...\n",
        ),
    ]
    assert scan_members(fixture, "sdist") == []


def _render(findings: list[Finding]) -> str:
    return "artifact carries skill knowledge:\n" + "\n".join(
        f"  {row.location} [{row.rule}] {row.evidence}" for row in findings
    )
