from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services.fold_service import P10Report, check_file


def _document(
    *,
    opener: str = "``` # @branches: 2 lang=python",
    code: str = ("if ready:  # @branch:ready # @branch:ready-fallthrough\n" "    return 1\n"),
    acceptance: str = "| AC1 | branch:ready branch:ready-fallthrough |",
    acceptance_title: str = "## Acceptance criteria",
) -> str:
    return f"# Probe\n\n{opener}\n{code}```\n\n" f"{acceptance_title}\n\n{acceptance}\n"


def _report(tmp_path: Path, text: str, name: str = "probe.md") -> P10Report:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    report = check_file(path).p10
    assert report is not None
    return report


def _defects(report: P10Report) -> set[tuple[str, str]]:
    return {
        (finding.kind, finding.branch_id or "")
        for finding in report.findings
        if finding.classification == "DEFECT"
    }


def _hygiene(report: P10Report) -> list[str]:
    return [finding.kind for finding in report.findings if finding.classification == "HYGIENE"]


def test_p10_clean_python_document(tmp_path: Path) -> None:
    report = _report(tmp_path, _document())
    assert report.population_eligible is True
    assert report.covered is True
    assert report.findings == ()
    assert report.statuses == ()


@pytest.mark.parametrize(
    ("opener", "code", "status", "field"),
    [
        (
            "``` # @branches: 2",
            "if ready: # @branch:ready # @branch:ready-fallthrough\n    pass\n",
            "SKIPPED-UNDECLARED",
            "undeclared",
        ),
        (
            "``` # @branches: 2 lang=bash",
            "if ready; then # @branch:ready # @branch:ready-fallthrough\nfi\n",
            "SKIPPED-NO-PARSER",
            "no_parser",
        ),
        (
            "``` # @branches: 2 lang=python",
            "if: # @branch:ready # @branch:ready-fallthrough\n",
            "SKIPPED-UNPARSEABLE",
            "unparseable",
        ),
    ],
)
def test_p10_language_states_preserve_coverage(
    tmp_path: Path, opener: str, code: str, status: str, field: str
) -> None:
    report = _report(tmp_path, _document(opener=opener, code=code))
    assert report.population_eligible is True
    assert report.covered is True
    assert report.statuses == (status,)
    assert getattr(report.status_counts, field) == 1


def test_p10_skipped_fence_ids_exempt_ac_without_branch(tmp_path: Path) -> None:
    text = _document(
        opener="``` # @branches: 2 lang=bash",
        code="if ready; then # @branch:ready # @branch:ready-fallthrough\nfi\n",
    )
    report = _report(tmp_path, text)
    assert report.statuses == ("SKIPPED-NO-PARSER",)
    assert _defects(report) == set()


def test_p10_one_skipped_fence_does_not_suppress_parsed_fence(tmp_path: Path) -> None:
    text = _document(
        acceptance=(
            "| AC1 | branch:ready branch:ready-fallthrough branch:shell "
            "branch:shell-fallthrough |"
        )
    )
    text = text.replace(
        "\n## Acceptance",
        (
            "\n``` # @branches: 2 lang=bash\n"
            "if shell; then # @branch:shell # @branch:shell-fallthrough\n"
            "fi\n```\n\n## Acceptance"
        ),
    )
    report = _report(tmp_path, text)
    assert report.statuses == ("SKIPPED-NO-PARSER",)
    assert _defects(report) == set()
    assert "branches-mismatch" not in _hygiene(report)


def test_p10_fallthrough_is_derived_from_chain_id(tmp_path: Path) -> None:
    wrong = _document(code="if ready: # @branch:ready # @branch:other-fallthrough\n    pass\n")
    report = _report(tmp_path, wrong)
    assert "branches-mismatch" in _hygiene(report)
    assert ("no-branch", "ready-fallthrough") in _defects(report)


def test_p10_untagged_chain_has_no_fallthrough_seat(tmp_path: Path) -> None:
    text = _document(
        opener="``` # @branches: 2 lang=python",
        code=(
            "if first: # @branch:first # @branch:first-fallthrough\n"
            "    pass\n"
            "if second:\n"
            "    pass\n"
        ),
        acceptance="| AC1 | branch:first branch:first-fallthrough |",
    )
    report = _report(tmp_path, text)
    assert "branches-mismatch" in _hygiene(report)
    assert _defects(report) == set()


def test_p10_explicit_else_has_no_virtual_fallthrough(tmp_path: Path) -> None:
    text = _document(
        opener="``` # @branches: 2 lang=python",
        code=(
            "if ready: # @branch:ready\n" "    pass\n" "else: # @branch:not-ready\n" "    pass\n"
        ),
        acceptance="| AC1 | branch:ready branch:not-ready |",
    )
    report = _report(tmp_path, text)
    assert report.findings == ()


def test_p10_detached_annotation_does_not_attach(tmp_path: Path) -> None:
    text = _document(
        opener="``` # @branches: 2 lang=python",
        code=(
            "# if ready: # @branch:ready # @branch:ready-fallthrough\n" "if ready:\n" "    pass\n"
        ),
    )
    report = _report(tmp_path, text)
    assert "branches-mismatch" in _hygiene(report)
    assert {item for item in _defects(report) if item[0] == "no-branch"} == {
        ("no-branch", "ready"),
        ("no-branch", "ready-fallthrough"),
    }


def test_p10_header_string_literal_is_not_an_annotation(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        _document(
            code='if ready: print("@branch:ready @branch:ready-fallthrough")\n',
        ),
    )
    assert report.covered is False
    assert report.statuses == ("SKIPPED",)
    assert report.findings == ()


def test_p10_multiline_header_accepts_annotation_on_first_line(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        _document(
            code=(
                "if (  # @branch:ready # @branch:ready-fallthrough\n"
                "    ready\n"
                "):\n"
                "    pass\n"
            ),
        ),
    )
    assert report.findings == ()
    assert report.statuses == ()


def test_p10_multiline_header_accepts_annotation_on_colon_line(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        _document(
            code=(
                "if (\n"
                "    ready\n"
                "):  # @branch:ready # @branch:ready-fallthrough\n"
                "    pass\n"
            ),
        ),
    )
    assert report.findings == ()
    assert report.statuses == ()


def test_p10_non_header_continuation_annotation_does_not_attach(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        _document(
            code=(
                "value = (\n"
                "    ready  # @branch:ready # @branch:ready-fallthrough\n"
                ")\n"
                "if ready:\n"
                "    pass\n"
            ),
        ),
    )
    assert "branches-mismatch" in _hygiene(report)
    assert {item for item in _defects(report) if item[0] == "no-branch"} == {
        ("no-branch", "ready"),
        ("no-branch", "ready-fallthrough"),
    }


def test_p10_multiline_if_elif_else_headers_attach_three_of_three(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        _document(
            opener="``` # @branches: 3 lang=python",
            code=(
                "if first: # @branch:first\n"
                "    pass\n"
                "elif (\n"
                "    second\n"
                "): # @branch:second\n"
                "    pass\n"
                "else: # @branch:otherwise\n"
                "    pass\n"
            ),
            acceptance="| AC1 | branch:first branch:second branch:otherwise |",
        ),
    )
    assert report.findings == ()
    assert report.statuses == ()


def test_p10_tab_indented_nested_else_uses_source_indentation(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        _document(
            opener="``` # @branches: 4 lang=python",
            code=(
                "if outer: # @branch:outer # @branch:outer-fallthrough\n"
                "\tif inner: # @branch:inner\n"
                "\t\tpass\n"
                "\telse: # @branch:inner-else\n"
                "\t\tpass\n"
            ),
            acceptance=(
                "| AC1 | branch:outer branch:outer-fallthrough " "branch:inner branch:inner-else |"
            ),
        ),
    )
    assert report.findings == ()
    assert report.statuses == ()


def test_p10_token_layer_is_case_sensitive_and_whole_token(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        _document(acceptance="| AC1 | branch:Ready branch:ready-fallthrough-more |"),
    )
    assert _defects(report) == {
        ("no-AC", "ready"),
        ("no-AC", "ready-fallthrough"),
        ("no-branch", "Ready"),
        ("no-branch", "ready-fallthrough-more"),
    }


def test_p10_malformed_and_duplicate_tokens_are_hygiene(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        _document(
            opener="``` # @branches: 2 lang=python",
            code=(
                "if ready: # @branch:ready # @branch:ready-fallthrough\n"
                "    pass # @branch:$bad\n"
            ),
            acceptance=("| AC1 | branch:ready branch:ready branch:ready-fallthrough branch:$bad |"),
        ),
    )
    assert _hygiene(report).count("malformed-token") == 2
    assert "duplicate-id" in _hygiene(report)


@pytest.mark.parametrize(
    ("title", "acceptance"),
    [
        ("## Acceptance", "### AC1\n\nbranch:ready branch:ready-fallthrough\n"),
        (
            "### Acceptance criteria",
            "**AC1 - behavior.**\n\nDetails.\n\nbranch:ready branch:ready-fallthrough\n",
        ),
        ("#### 4.10 Acceptance", "| AC1 | branch:ready branch:ready-fallthrough |"),
    ],
)
def test_p10_acceptance_formats_and_multiline_extent(
    tmp_path: Path, title: str, acceptance: str
) -> None:
    report = _report(
        tmp_path,
        _document(acceptance_title=title, acceptance=acceptance),
    )
    assert _defects(report) == set()


def test_p10_bold_ac_outside_acceptance_is_prose(tmp_path: Path) -> None:
    text = "**AC1 branch:ready branch:ready-fallthrough**\n\n" + _document(
        acceptance="| AC2 | no mapping |"
    )
    report = _report(tmp_path, text)
    assert _defects(report) == {
        ("no-AC", "ready"),
        ("no-AC", "ready-fallthrough"),
    }


def test_p10_zero_identifier_acceptance_section_is_ineligible(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        _document(acceptance="No numbered acceptance unit."),
    )
    assert report.population_eligible is False
    assert report.statuses == ("INELIGIBLE",)


def test_p10_two_status_kinds_render_once_but_count_per_fence(tmp_path: Path) -> None:
    text = _document(
        opener="``` # @branches: 2 lang=bash",
        code="if a; then # @branch:a # @branch:a-fallthrough\nfi\n",
        acceptance=(
            "| AC1 | branch:a branch:a-fallthrough branch:b branch:b-fallthrough "
            "branch:c branch:c-fallthrough |"
        ),
    )
    text = text.replace(
        "\n## Acceptance",
        (
            "\n``` # @branches: 2 lang=bash\n"
            "if b; then # @branch:b # @branch:b-fallthrough\nfi\n```\n"
            "``` # @branches: 2\n"
            "if c: # @branch:c # @branch:c-fallthrough\n    pass\n```\n\n"
            "## Acceptance"
        ),
    )
    report = _report(tmp_path, text)
    assert report.statuses == ("SKIPPED-UNDECLARED", "SKIPPED-NO-PARSER")
    assert report.status_counts.no_parser == 2
    assert report.status_counts.undeclared == 1
    assert report.render_lines()[-2:] == (
        f"P10 SKIPPED-UNDECLARED {report.path}",
        f"P10 SKIPPED-NO-PARSER {report.path}",
    )


def test_p10_cli_block_order_and_labelled_defects(tmp_path: Path) -> None:
    path = tmp_path / "mixed.md"
    path.write_text(
        _document(acceptance="| AC1 | branch:stale |"),
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli, ["fold", str(path), "--check"])
    assert result.exit_code == 0, result.output
    assert result.output.index("P5/P6: no violations") < result.output.index("P10 DEFECT")
    assert "branch:ready no-AC" in result.output
    assert "branch:ready-fallthrough no-AC" in result.output
    assert "branch:stale no-branch" in result.output


def test_p10_product_replay_has_10_9_set_difference(tmp_path: Path) -> None:
    no_else_ids = [*(f"path-{index}" for index in range(8)), "multiple-matches"]
    code = "".join(
        f"if condition_{index}: # @branch:{branch_id} "
        f"# @branch:{branch_id}-fallthrough\n    pass\n"
        for index, branch_id in enumerate(no_else_ids)
    )
    code += (
        "if terminal: # @branch:terminal\n"
        "    pass\n"
        "else: # @branch:terminal-else\n"
        "    pass\n"
    )
    common_ac = [
        *(branch_id for branch_id in no_else_ids if branch_id != "multiple-matches"),
        "terminal",
        "terminal-else",
    ]
    v4 = _report(
        tmp_path,
        _document(
            opener="``` # @branches: 20 lang=python",
            code=code,
            acceptance="| AC1 | " + " ".join(f"branch:{item}" for item in common_ac) + " |",
        ),
        "v4.md",
    )
    v5 = _report(
        tmp_path,
        _document(
            opener="``` # @branches: 20 lang=python",
            code=code,
            acceptance=(
                "| AC1 | "
                + " ".join(f"branch:{item}" for item in [*common_ac, "multiple-matches"])
                + " |"
            ),
        ),
        "v5.md",
    )
    v4_ids = {branch_id for _, branch_id in _defects(v4)}
    v5_ids = {branch_id for _, branch_id in _defects(v5)}
    assert len(v4_ids) == 10
    assert len(v5_ids) == 9
    assert v4_ids - v5_ids == {"multiple-matches"}
    assert not v5_ids - v4_ids


def test_p10_corpus_invocation_and_exact_four_line_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "blueprints").mkdir()
    (tmp_path / "doctrine" / "nested").mkdir(parents=True)
    (tmp_path / "blueprints" / "eligible.md").write_text(_document(), encoding="utf-8")
    (tmp_path / "blueprints" / "nested").mkdir()
    (tmp_path / "blueprints" / "nested" / "excluded.md").write_text(_document(), encoding="utf-8")
    (tmp_path / "doctrine" / "nested" / "skipped.md").write_text(
        _document(code="if ready:\n    pass\n"), encoding="utf-8"
    )
    (tmp_path / "GOLDEN-TIPS.md").write_text("# Tips\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["fold", "--check", "--corpus"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    summary = [line for line in lines if line.startswith("P10 ") and ":" in line]
    assert summary[-4:] == [
        "P10 POPULATION: 2",
        "P10 COVERAGE: 1/2 annotated",
        "P10 DENOMINATOR: 0 defect firings",
        "P10 STATUS: skipped=1 undeclared=0 no-parser=0 unparseable=0 ineligible=1",
    ]
    assert "excluded.md" not in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["fold", "--corpus"],
        ["fold", "some.md", "--check", "--corpus"],
    ],
)
def test_p10_corpus_usage_contract(args: list[str]) -> None:
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 2



def test_p10_corpus_prefers_orchestrator_blueprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New layout: orchestrator/blueprints/ is preferred over root blueprints/."""
    from cli_agent_orchestrator.services.fold_service import _p10_corpus_paths

    orch_bp = tmp_path / "orchestrator" / "blueprints"
    orch_bp.mkdir(parents=True)
    (orch_bp / "new-layout.md").write_text(_document(), encoding="utf-8")
    (tmp_path / "orchestrator" / "GOLDEN-TIPS.md").write_text("# Tips\n", encoding="utf-8")
    # Also create a legacy blueprints/ to confirm it's NOT used
    legacy_bp = tmp_path / "blueprints"
    legacy_bp.mkdir()
    (legacy_bp / "legacy.md").write_text(_document(), encoding="utf-8")

    paths = _p10_corpus_paths(tmp_path)
    path_strs = [str(p) for p in paths]
    # orchestrator/blueprints wins — new-layout.md present, legacy.md absent
    assert any("new-layout.md" in s for s in path_strs)
    assert not any("legacy.md" in s for s in path_strs)


def test_p10_corpus_falls_back_to_legacy_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy layout still works when no orchestrator/ dir exists."""
    (tmp_path / "blueprints").mkdir()
    (tmp_path / "blueprints" / "eligible.md").write_text(_document(), encoding="utf-8")
    (tmp_path / "GOLDEN-TIPS.md").write_text("# Tips\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["fold", "--check", "--corpus"])
    assert result.exit_code == 0, result.output
    assert "P10 POPULATION: 1" in result.output


def test_p10_corpus_prefers_orchestrator_golden_tips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """orchestrator/GOLDEN-TIPS.md is preferred over root GOLDEN-TIPS.md."""
    from cli_agent_orchestrator.services.fold_service import _p10_corpus_paths

    (tmp_path / "blueprints").mkdir()
    (tmp_path / "blueprints" / "doc.md").write_text(_document(), encoding="utf-8")
    # Both GOLDEN-TIPS exist
    (tmp_path / "GOLDEN-TIPS.md").write_text("# Legacy tips\n", encoding="utf-8")
    orch = tmp_path / "orchestrator"
    orch.mkdir()
    (orch / "GOLDEN-TIPS.md").write_text("# New tips\n", encoding="utf-8")

    paths = _p10_corpus_paths(tmp_path)
    path_strs = [str(p) for p in paths]
    # orchestrator/GOLDEN-TIPS.md chosen, NOT root GOLDEN-TIPS.md
    assert any("orchestrator/GOLDEN-TIPS.md" in s for s in path_strs)
    assert not any(s.endswith("/GOLDEN-TIPS.md") and "orchestrator" not in s for s in path_strs)
