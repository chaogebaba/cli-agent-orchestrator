from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services import fold_service
from cli_agent_orchestrator.services.fold_service import (
    P9Report,
    RepoMapping,
    check_corpus,
    check_file,
)


def _report(path: Path, *repos: RepoMapping) -> P9Report:
    report = check_file(path, repos).p9
    assert report is not None
    return report


def _write(path: Path, text: str = "# fixture\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_p9_doctrine_merge_witness_fires_exactly_one_path_missing(tmp_path: Path) -> None:
    repo = tmp_path / "outer"
    repo.mkdir()
    document = _write(
        tmp_path / "blueprints" / "doctrine-merge.md",
        "\n" * 52 + "`memory/kiro-lanes-and-w2w-messaging.md:17-18`\n",
    )

    report = _report(document, RepoMapping("outer", repo))

    assert [finding.kind for finding in report.findings] == ["path-missing"]
    assert report.defect_count == 1
    assert report.render_lines() == (
        f"P9 DEFECT {document}:53 path-missing "
        "citation=memory/kiro-lanes-and-w2w-messaging.md:17-18 reason=missing",
    )


def test_p9_resolves_direct_package_and_bare_citations(tmp_path: Path) -> None:
    repo = tmp_path / "fork"
    _write(repo / "docs" / "contract.md")
    _write(repo / "src" / "cli_agent_orchestrator" / "services" / "terminal_service.py")
    document = _write(
        tmp_path / "probe.md",
        "`docs/contract.md:1` `services/terminal_service.py:1` `terminal_service.py:1`\n",
    )

    report = _report(document, RepoMapping("fork", repo))

    assert report.findings == ()
    assert report.statuses == ()
    assert report.basename_resolved == 1


def test_p9_aggregate_worktree_exclusions_apply_to_every_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = tmp_path / "outer"
    fork = outer / "fork"
    worktree = outer / "cao-f87p2-copy"
    _write(outer / "live" / "copied.py")
    _write(worktree / "src" / "copied.py")
    fork.mkdir(parents=True)
    document = _write(tmp_path / "probe.md", "`copied.py:1`\n")

    def worktrees(root: Path) -> tuple[Path, ...]:
        return (worktree,) if root.resolve() == fork.resolve() else ()

    monkeypatch.setattr(fold_service, "_p9_git_worktree_roots", worktrees)
    report = _report(document, RepoMapping("outer", outer), RepoMapping("fork", fork))

    assert report.findings == ()
    assert report.basename_resolved == 1


def test_p9_reports_ambiguous_adjacency_without_counting_it_as_basename_hygiene(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _write(repo / "left.py")
    _write(repo / "right.py")
    document = _write(tmp_path / "probe.md", "`left.py:1` `name` `right.py:1`\n")

    report = _report(document, RepoMapping("repo", repo))

    assert [finding.kind for finding in report.findings] == ["AMBIGUOUS-ADJACENCY"]
    assert report.ambiguous_adjacency == 1
    assert report.ambiguous_basename == 0
    assert report.basename_resolved == 2


def test_p9_corpus_census_reproduces_85_of_482_hygiene_rate(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    repo = tmp_path / "repo"
    citations: list[str] = []
    for index in range(397):
        name = f"unique_{index:03d}.py"
        _write(repo / "unique" / name)
        citations.append(f"`{name}:1`")
    for index in range(85):
        name = f"ambiguous_{index:03d}.py"
        _write(repo / "left" / name)
        _write(repo / "right" / name)
        citations.append(f"`{name}:1`")
    _write(corpus / "blueprints" / "census.md", "\n".join(citations) + "\n")

    result = check_corpus(corpus, (RepoMapping("repo", repo),))
    report = result.p9_reports[0]

    assert report.basename_resolved == 397
    assert report.ambiguous_basename == 85
    assert report.defect_count == 0
    assert result.p9_summary_lines[-1] == (
        "P9 HYGIENE: ambiguous-basename=85/482 (17.6349%) ambiguous-adjacency=0"
    )


def test_p9_cli_wires_repo_mapping_and_preserves_block_order(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    document = _write(tmp_path / "probe.md", "`missing.py:1`\n")

    result = CliRunner().invoke(cli, ["fold", str(document), "--check", "--repo", f"repo={repo}"])

    assert result.exit_code == 0, result.output
    assert result.output.index("P5/P6: no violations") < result.output.index("P9 DEFECT")
    assert result.output.index("P9 DEFECT") < result.output.index("P10")
    assert f"P9 DEFECT {document}:1 path-missing" in result.output


def test_p9_corpus_reports_unused_mapping_once(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    unused = tmp_path / "unused"
    _write(outer / "found.md")
    unused.mkdir()
    _write(tmp_path / "blueprints" / "probe.md", "`outer/found.md:1`\n")

    result = check_corpus(
        tmp_path,
        (RepoMapping("outer", outer), RepoMapping("unused", unused)),
    )

    assert result.p9_unused_mapping_lines == (f"P9 STATUS MAPPING-UNUSED - unused={unused}",)


def test_p9_duplicate_repo_name_with_different_paths_is_usage_error(tmp_path: Path) -> None:
    document = _write(tmp_path / "probe.md", "# probe\n")
    first = tmp_path / "first"
    second = tmp_path / "second"

    result = CliRunner().invoke(
        cli,
        [
            "fold",
            str(document),
            "--check",
            "--repo",
            f"repo={first}",
            "--repo",
            f"repo={second}",
        ],
    )

    assert result.exit_code == 2
    assert "duplicate --repo mapping" in result.output
