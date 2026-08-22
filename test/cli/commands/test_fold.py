from __future__ import annotations

import hashlib
import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services.fold_service import _parse_structure


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _invoke(path: Path, document: bytes, *args: str) -> Result:
    return CliRunner().invoke(cli, ["fold", str(path), *args], input=document)


def _raw_command(path: Path, *args: str) -> list[str]:
    return [
        sys.executable,
        "-c",
        "from cli_agent_orchestrator.cli.main import cli; cli()",
        "fold",
        str(path),
        *args,
    ]


def _invoke_raw(path: Path, document: bytes, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        _raw_command(path, *args),
        input=document,
        capture_output=True,
        check=False,
        timeout=5,
    )


def _assert_no_postcondition_output(output: str) -> None:
    for marker in ("P1 failed", "P2 failed", "P3 failed", "P5 failed", "P6 failed", "WROTE"):
        assert marker not in output


def test_ac1_broken_folds_refuse_and_preserve_bytes(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"
    missing.write_text("present\n", encoding="utf-8")
    before = _sha(missing)
    result = _invoke(
        missing,
        b'{"hunks":[{"anchor":"absent","replace":"new"}]}',
    )
    assert result.exit_code == 1
    assert "P1 failed" in result.output
    assert _sha(missing) == before

    duplicated = tmp_path / "duplicated.md"
    duplicated.write_text("§7 old\n", encoding="utf-8")
    before = _sha(duplicated)
    result = _invoke(
        duplicated,
        '{"hunks":[{"anchor":"old","replace":"§7\'s"}]}'.encode(),
    )
    assert result.exit_code == 1
    assert "P3 failed" in result.output
    assert "duplicated token sequence '§7'" in result.output
    assert _sha(duplicated) == before


@pytest.mark.parametrize(
    ("pre", "document", "post"),
    [
        (b"OLD", b'{"hunks":[{"anchor":"OLD","replace":"NEW OLD form"}]}', b"NEW OLD form"),
        (b"anchor", b'{"hunks":[{"anchor":"anchor","after":" added"}]}', b"anchor added"),
        (b"legacy", b'{"hunks":[{"anchor":"legacy","strike":true}]}', b"~~legacy~~"),
    ],
)
def test_ac2_correct_edits_pass(tmp_path: Path, pre: bytes, document: bytes, post: bytes) -> None:
    path = tmp_path / "target.md"
    path.write_bytes(pre)
    result = _invoke(path, document)
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == post
    assert result.output.count("-> WROTE (") == 1


def test_ac4_and_ac5_command_is_registered_without_force() -> None:
    command_help = CliRunner().invoke(cli, ["fold", "--help"])
    root_help = CliRunner().invoke(cli, ["--help"])
    assert command_help.exit_code == 0
    assert "--force" not in command_help.output
    assert root_help.exit_code == 0
    assert "fold" in root_help.output
    assert cli.commands["fold"] is not None


def test_ac6_check_reports_absolute_table_and_list_violations(tmp_path: Path) -> None:
    path = tmp_path / "broken.md"
    path.write_text(
        "| A | B |\n"
        "|---|---|\n"
        "| `x | y` | ok |\n"
        "| escaped \\| pipe | ok |\n"
        "| broken |\n\n"
        "1. one\n"
        "3. three\n",
        encoding="utf-8",
    )
    before = _sha(path)
    result = CliRunner().invoke(cli, ["fold", str(path), "--check"])
    assert result.exit_code == 0, result.output
    assert "skipped: P1, P2, P3" in result.output
    assert result.output.count("table body row") == 1
    assert result.output.count("ordered-list item") == 1
    assert _sha(path) == before


def test_ac6_nested_and_restarted_lists_are_valid(tmp_path: Path) -> None:
    path = tmp_path / "lists.md"
    path.write_text(
        "1. outer\n"
        "   1. inner\n"
        "   2. inner\n"
        "2. outer\n\n"
        "## Boundary\n\n"
        "1. restarted\n"
        "2. continued\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli, ["fold", str(path), "--check"])
    assert result.exit_code == 0
    assert "P5/P6: no violations" in result.output


def test_ac6_p5_ignores_lazy_continuation_without_pipes_but_retains_item(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lazy-table.md"
    document = (
        "| a | b | c |\n"
        "|---|---|---|\n"
        "| 1 | 2 | 3 |\n"
        "**prose right after the table, no blank line**\n"
    )
    path.write_text(document, encoding="utf-8")

    structure = _parse_structure(document.encode())
    table = next(container for container in structure.containers if container.kind == "P5")
    assert len(table.items) == 2
    assert not structure.violations

    result = CliRunner().invoke(cli, ["fold", str(path), "--check"])
    assert result.exit_code == 0, result.output
    assert "P5/P6: no violations" in result.output


@pytest.mark.parametrize("row", ["1 | 2", "1 | 2 | 3 | 4 |"])
def test_ac6_p5_no_leading_pipe_wrong_arity_still_fires(
    row: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "no-leading-pipe.md"
    path.write_text(
        "| a | b | c |\n" "|---|---|---|\n" f"{row}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["fold", str(path), "--check"])
    assert result.exit_code == 0, result.output
    assert result.output.count("table body row") == 1
    assert "header has 3" in result.output


def test_ac6_p5_pipe_started_wrong_arity_still_fires(tmp_path: Path) -> None:
    path = tmp_path / "pipe-started.md"
    path.write_text(
        "| a | b | c |\n" "|---|---|---|\n" "| 1 | 2 |\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["fold", str(path), "--check"])
    assert result.exit_code == 0, result.output
    assert result.output.count("table body row") == 1
    assert "header has 3" in result.output


def test_ac6_p5_lazy_prose_with_literal_pipe_still_fires(tmp_path: Path) -> None:
    path = tmp_path / "lazy-prose-with-pipe.md"
    path.write_text(
        "| a | b | c |\n"
        "|---|---|---|\n"
        "| 1 | 2 | 3 |\n"
        "prose with a literal | right after the table\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["fold", str(path), "--check"])
    assert result.exit_code == 0, result.output
    assert result.output.count("table body row") == 1
    assert "header has 3" in result.output


def test_ac6_p6_has_no_lazy_continuation_bleed(tmp_path: Path) -> None:
    prose_path = tmp_path / "list-prose.md"
    prose_path.write_text(
        "1. one\n" "2. two\n" "prose right after the list, no blank line\n",
        encoding="utf-8",
    )
    prose_result = CliRunner().invoke(cli, ["fold", str(prose_path), "--check"])
    assert prose_result.exit_code == 0, prose_result.output
    assert "P5/P6: no violations" in prose_result.output

    skip_path = tmp_path / "list-skip.md"
    skip_path.write_text("1. one\n3. three\n", encoding="utf-8")
    skip_result = CliRunner().invoke(cli, ["fold", str(skip_path), "--check"])
    assert skip_result.exit_code == 0, skip_result.output
    assert skip_result.output.count("ordered-list item") == 1


def test_ac6_new_table_violation_blocks_but_baseline_survives(tmp_path: Path) -> None:
    path = tmp_path / "table.md"
    clean = b"| A | B |\n|---|---|\n| x | y |\n"
    path.write_bytes(clean)
    result = _invoke(
        path,
        b'{"hunks":[{"anchor":"| x | y |","replace":"| x |"}]}',
    )
    assert result.exit_code == 1
    assert "P5 failed" in result.output
    assert path.read_bytes() == clean

    baseline = b"| A | B |\n|---|---|\n| old |\n\nparagraph\n"
    path.write_bytes(baseline)
    result = _invoke(
        path,
        b'{"hunks":[{"anchor":"paragraph","replace":"updated"}]}',
    )
    assert result.exit_code == 0, result.output
    assert path.read_bytes().endswith(b"updated\n")


def test_ac6_baseline_row_cannot_excuse_a_new_violation_in_another_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "row-origins.md"
    pre = b"| A | B |\n|---|---|\n| baseline |\n| target | ok |\n"
    path.write_bytes(pre)
    result = _invoke(
        path,
        b'{"hunks":[{"anchor":"target","after":" | extra"}]}',
    )
    assert result.exit_code == 1
    assert "P5 failed" in result.output
    assert path.read_bytes() == pre


def test_ac6_changed_header_identity_cannot_launder_a_baseline_violation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "header-identity.md"
    pre = b"| A | B |\n|---|---|\n| baseline |\n"
    path.write_bytes(pre)
    result = _invoke(
        path,
        b'{"hunks":[{"anchor":"| A | B |","replace":"| X | Y |"}]}',
    )
    assert result.exit_code == 1
    assert "P5 failed" in result.output
    assert path.read_bytes() == pre


def test_ac6_new_list_violation_blocks(tmp_path: Path) -> None:
    path = tmp_path / "list.md"
    clean = b"1. one\n2. two\n"
    path.write_bytes(clean)
    result = _invoke(
        path,
        b'{"hunks":[{"anchor":"2.","replace":"3."}]}',
    )
    assert result.exit_code == 1
    assert "P6 failed" in result.output
    assert path.read_bytes() == clean


def test_ac13_multi_hunk_transaction_writes_once(tmp_path: Path) -> None:
    path = tmp_path / "multi.md"
    path.write_bytes(b"alpha middle omega")
    result = _invoke(
        path,
        b'{"hunks":[{"anchor":"alpha","replace":"A"},' b'{"anchor":"omega","replace":"Z"}]}',
    )
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == b"A middle Z"
    assert result.output.count("-> WROTE (") == 1


def test_ac13_partial_failure_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "partial.md"
    path.write_bytes(b"alpha omega")
    before = _sha(path)
    result = _invoke(
        path,
        b'{"hunks":[{"anchor":"alpha","replace":"A"},' b'{"anchor":"missing","replace":"Z"}]}',
    )
    assert result.exit_code == 1
    assert "P1 failed" in result.output
    assert _sha(path) == before


def test_ac13_stdin_preserves_quotes_backticks_and_newlines(tmp_path: Path) -> None:
    path = tmp_path / "prose.md"
    path.write_bytes(b"old")
    replacement = b'first "quoted" line\nsecond `code` line'
    document = (
        b'{"hunks":[{"anchor":"old","replace":"first \\"quoted\\" line' b'\\nsecond `code` line"}]}'
    )
    result = _invoke(path, document)
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == replacement


@pytest.mark.pty
def test_ac13_flag_form_completes_on_a_real_pty(tmp_path: Path) -> None:
    path = tmp_path / "pty.md"
    path.write_bytes(b"old")
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"new")
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            _raw_command(path, "--anchor", "old", "--replace-with", f"@{replacement}"),
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(slave)
        slave = -1
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)
    assert process.returncode == 0, (stdout + stderr).decode()
    assert path.read_bytes() == b"new"


def test_ac13_flag_form_ignores_supplied_stdin(tmp_path: Path) -> None:
    path = tmp_path / "flag.md"
    path.write_bytes(b"old untouched")
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"new")
    stdin = b'{"hunks":[{"anchor":"untouched","replace":"changed"}]}'
    result = _invoke(
        path,
        stdin,
        "--anchor",
        "old",
        "--replace-with",
        f"@{replacement}",
    )
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == b"new untouched"


@pytest.mark.pty
def test_ac13_bare_tty_exits_two_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "pty.md"
    path.write_bytes(b"old")
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            _raw_command(path),
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(slave)
        slave = -1
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)
    assert process.returncode == 2, (stdout + stderr).decode()
    assert path.read_bytes() == b"old"


def test_ac13_check_ignores_stdin_and_rejects_an_edit_spec(tmp_path: Path) -> None:
    path = tmp_path / "check.md"
    path.write_bytes(b"old")
    result = _invoke(path, b"{", "--check")
    assert result.exit_code == 0, result.output
    assert "skipped: P1, P2, P3" in result.output
    assert path.read_bytes() == b"old"

    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"new")
    result = _invoke(
        path,
        b"",
        "--check",
        "--anchor",
        "old",
        "--replace-with",
        f"@{replacement}",
    )
    assert result.exit_code == 2
    assert path.read_bytes() == b"old"


@pytest.mark.parametrize(
    "args",
    [
        ("--anchor", "old"),
        ("--replace-with", "@replacement"),
        ("--expect-count", "3"),
    ],
)
def test_ac13_incomplete_flags_do_not_fall_through_to_stdin(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    path = tmp_path / "incomplete.md"
    path.write_bytes(b"old")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new")
    resolved = tuple(f"@{replacement}" if value == "@replacement" else value for value in args)
    document = b'{"hunks":[{"anchor":"old","replace":"stdin won"}]}'
    result = _invoke(path, document, *resolved)
    assert result.exit_code == 2
    assert path.read_bytes() == b"old"


def test_empty_flag_anchor_is_a_usage_error(tmp_path: Path) -> None:
    path = tmp_path / "empty-anchor.md"
    path.write_bytes(b"old")
    result = _invoke(path, b"", "--anchor", "", "--strike")
    assert result.exit_code == 2
    assert path.read_bytes() == b"old"


@pytest.mark.parametrize(
    "args",
    [
        ("--anchor", "a", "--anchor", "b", "--strike"),
        ("--anchor", "a", "--replace-with", "@first", "--replace-with", "@second"),
        ("--anchor", "a", "--strike", "--strike"),
        ("--anchor", "a", "--strike", "--expect-count", "1", "--expect-count", "2"),
    ],
)
def test_ac13_repeated_flags_are_usage_errors(tmp_path: Path, args: tuple[str, ...]) -> None:
    path = tmp_path / "repeated.md"
    path.write_bytes(b"a")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"x")
    second.write_bytes(b"y")
    resolved = tuple(
        f"@{first}" if value == "@first" else f"@{second}" if value == "@second" else value
        for value in args
    )
    result = _invoke(path, b"", *resolved)
    assert result.exit_code == 2
    assert path.read_bytes() == b"a"


SCHEMA_ROWS = [
    ("malformed JSON", b"{"),
    ("non-object root", b'[{"anchor":"a","replace":"b"}]'),
    ("missing hunks", b"{}"),
    ("empty hunks", b'{"hunks":[]}'),
    ("root unknown key", b'{"hunks":[{"anchor":"a","replace":"b"}],"bogus":1}'),
    ("hunk unknown key", b'{"hunks":[{"anchor":"a","replace":"b","bogus":1}]}'),
    (
        "root duplicate key",
        b'{"hunks":[{"anchor":"a","replace":"b"}],' b'"hunks":[{"anchor":"c","replace":"d"}]}',
    ),
    (
        "hunk duplicate key",
        b'{"hunks":[{"anchor":"a","replace":"x","replace":"y"}]}',
    ),
    ("empty anchor", b'{"hunks":[{"anchor":"","replace":"b"}]}'),
    ("expect count below minimum", b'{"hunks":[{"anchor":"a","replace":"b","expect_count":0}]}'),
    ("expect count non-integer", b'{"hunks":[{"anchor":"a","replace":"b","expect_count":"1"}]}'),
    ("zero operations", b'{"hunks":[{"anchor":"a"}]}'),
    ("two operations", b'{"hunks":[{"anchor":"a","replace":"b","after":"c"}]}'),
    ("false strike", b'{"hunks":[{"anchor":"a","strike":false}]}'),
]


@pytest.mark.parametrize(("name", "document"), SCHEMA_ROWS, ids=[row[0] for row in SCHEMA_ROWS])
def test_ac13_schema_rejection_table(name: str, document: bytes, tmp_path: Path) -> None:
    path = tmp_path / "schema.md"
    path.write_bytes(b"a b c\n")
    before = _sha(path)
    result = _invoke(path, document)
    assert result.exit_code == 2, (name, result.output)
    assert _sha(path) == before
    _assert_no_postcondition_output(result.output)


def test_ac13_schema_accepts_one_hunk_and_defaults_expect_count(tmp_path: Path) -> None:
    path = tmp_path / "accepted.md"
    path.write_bytes(b"a b c\n")
    result = _invoke(path, b'{"hunks":[{"anchor":"a","replace":"x"}]}')
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == b"x b c\n"


@pytest.mark.parametrize(
    "document",
    [
        b'{"hunks":\xff',
        b'{"hunks":[{"anchor":"a","replace":"\\uD800"}]}',
    ],
    ids=["malformed UTF-8", "lone surrogate"],
)
@pytest.mark.slow  # F254 D19: exceeds unit budget
def test_ac13_raw_byte_decode_rejections(document: bytes, tmp_path: Path) -> None:
    path = tmp_path / "raw.md"
    path.write_bytes(b"a b c\n")
    before = _sha(path)
    result = _invoke_raw(path, document)
    output = (result.stdout + result.stderr).decode("utf-8")
    assert result.returncode == 2, output
    assert _sha(path) == before
    _assert_no_postcondition_output(output)
    assert b"Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("operation", ["replace", "after", "strike"])
@pytest.mark.parametrize("fixture", ["single", "repeated", "failure"])
def test_ac13_json_and_flag_transport_equivalence(
    operation: str, fixture: str, tmp_path: Path
) -> None:
    path = tmp_path / "transport.md"
    operand = tmp_path / "operand"
    operand.write_bytes(b"z")
    if fixture == "single":
        pre, anchor, count = b"a", "a", 1
    elif fixture == "repeated":
        pre, anchor, count = b"a a", "a", 2
    else:
        pre, anchor, count = b"a", "missing", 1

    path.write_bytes(pre)
    if operation == "strike":
        body = f'{{"anchor":"{anchor}","strike":true,"expect_count":{count}}}'
        flags = ["--anchor", anchor, "--strike", "--expect-count", str(count)]
    else:
        body = f'{{"anchor":"{anchor}","{operation}":"z","expect_count":{count}}}'
        option = "--replace-with" if operation == "replace" else "--after"
        flags = ["--anchor", anchor, option, f"@{operand}", "--expect-count", str(count)]
    document = f'{{"hunks":[{body}]}}'.encode()

    json_result = _invoke(path, document)
    json_bytes = path.read_bytes()
    path.write_bytes(pre)
    flag_result = _invoke(path, b"ignored", *flags)
    flag_bytes = path.read_bytes()

    assert flag_result.exit_code == json_result.exit_code
    assert flag_bytes == json_bytes
    assert flag_result.output == json_result.output


AC14_ROWS = [
    (
        1,
        b"ONE...TWO",
        b'{"hunks":[{"anchor":"ONE","replace":"A"},{"anchor":"TWO","replace":"B"}]}',
        0,
        b"A...B",
    ),
    (
        2,
        b"ONETWO",
        b'{"hunks":[{"anchor":"TWO","replace":"B"},{"anchor":"ONE","replace":"A"}]}',
        0,
        b"AB",
    ),
    (
        3,
        b"xx",
        b'{"hunks":[{"anchor":"x","expect_count":2,"replace":"Y"}]}',
        0,
        b"YY",
    ),
    (
        4,
        b"xx",
        b'{"hunks":[{"anchor":"x","expect_count":2,"after":"Y"}]}',
        0,
        b"xYxY",
    ),
    (
        5,
        b"ONETWO",
        b'{"hunks":[{"anchor":"ONE","after":"X"},{"anchor":"TWO","replace":"Z"}]}',
        2,
        None,
    ),
    (
        6,
        b"ABCDE",
        b'{"hunks":[{"anchor":"ABC","replace":"P"},{"anchor":"CDE","replace":"Q"}]}',
        2,
        None,
    ),
    (
        7,
        b"ONE",
        b'{"hunks":[{"anchor":"ONE","after":"X"},{"anchor":"ONE","after":"Y"}]}',
        2,
        None,
    ),
    (
        8,
        b"ABCDE",
        b'{"hunks":[{"anchor":"AB","after":"X"},{"anchor":"BCD","replace":"Q"}]}',
        2,
        None,
    ),
    (
        9,
        b"ONETWO",
        b'{"hunks":[{"anchor":"ONETWO","after":"X"},{"anchor":"TWO","replace":"Z"}]}',
        2,
        None,
    ),
    (
        10,
        b"ABCDE",
        b'{"hunks":[{"anchor":"ABC","after":"X"},{"anchor":"BCD","after":"Y"}]}',
        0,
        b"ABCXDYE",
    ),
]


@pytest.mark.parametrize(
    ("row", "pre", "document", "exit_code", "post"),
    AC14_ROWS,
    ids=[f"row {row[0]}" for row in AC14_ROWS],
)
def test_ac14_edit_footprint_matrix(
    row: int,
    pre: bytes,
    document: bytes,
    exit_code: int,
    post: bytes | None,
    tmp_path: Path,
) -> None:
    path = tmp_path / "footprints.md"
    path.write_bytes(pre)
    before = _sha(path)
    result = _invoke(path, document)
    assert result.exit_code == exit_code, (row, result.output)
    if post is None:
        assert _sha(path) == before
        _assert_no_postcondition_output(result.output)
    else:
        assert path.read_bytes() == post
