import ast
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from cli_agent_orchestrator.transcript_scrub import (
    NEUTRAL_REPLACEMENT_POLICY,
    ScrubRejected,
    Span,
    rewrite_jsonl,
)


def _span(text, start, end, *, line=1, path=("payload", "message"), rule="rule.v1"):
    return Span(
        line,
        path,
        start,
        end,
        NEUTRAL_REPLACEMENT_POLICY,
        hashlib.sha256(text[start:end].encode()).hexdigest(),
        rule,
    )


def _line(message="alpha beta gamma"):
    return (
        '{ "timestamp" : "t", "type" : "compacted", "payload" : '
        '{"first_window_id":"a","message":'
        + json.dumps(message)
        + ',"previous_window_id":"b","replacement_history":[],"window_id":"c",'
        '"window_number":1} }\n'
    ).encode()


def test_decoded_span_rewrite_preserves_non_target_bytes_and_identity():
    content = _line()
    span = _span("alpha beta gamma", 6, 10)
    result = rewrite_jsonl(content, [span])
    before_prefix, before_suffix = content.split(b'"alpha beta gamma"')
    after_token_start = result.content.index(b'"', len(before_prefix))
    after_token_end = result.content.index(b'"', after_token_start + 1) + 1
    assert result.content[:after_token_start] == before_prefix
    assert result.content[after_token_end:] == before_suffix
    assert json.loads(result.content)["payload"]["message"] == "alpha ____ gamma"
    assert result.span_count == 1


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda span: replace(span, start=-1), "span_offsets_invalid"),
        (lambda span: replace(span, end=100), "span_offsets_invalid"),
        (lambda span: replace(span, expected_preimage_sha256="0" * 64), "preimage"),
        (lambda span: replace(span, key_path=("payload", "window_number")), "string"),
    ],
)
def test_invalid_span_rejects_without_mutating_input(mutate, match):
    content = _line()
    with pytest.raises(ScrubRejected, match=match):
        rewrite_jsonl(content, [mutate(_span("alpha beta gamma", 0, 5))])
    assert content == _line()


def test_disjoint_spans_apply_in_descending_order():
    text = "ab middle yz"
    result = rewrite_jsonl(_line(text), [_span(text, 0, 2), _span(text, 10, 12)])
    assert json.loads(result.content)["payload"]["message"] == "__ middle __"


def test_exact_duplicate_span_is_rejected():
    span = _span("alpha beta gamma", 0, 5)
    with pytest.raises(ScrubRejected, match="duplicate_span"):
        rewrite_jsonl(_line(), [span, span])


def test_compatible_overlap_and_adjacency_merge_once():
    text = "abcdefgh"
    spans = [_span(text, 1, 4, rule="a.v1"), _span(text, 3, 6, rule="b.v1")]
    result = rewrite_jsonl(_line(text), spans)
    assert json.loads(result.content)["payload"]["message"] == "a_____gh"


def test_conflicting_overlap_is_rejected():
    text = "abcdefgh"
    first = _span(text, 1, 4)
    second = replace(_span(text, 3, 6), replacement_policy="other")
    with pytest.raises(ScrubRejected, match="unknown_replacement_policy|conflicting_overlap"):
        rewrite_jsonl(_line(text), [first, second])


def test_multibyte_and_escaped_decoded_offsets_are_codepoint_based():
    text = "aé\\quoted\"z"
    result = rewrite_jsonl(_line(text), [_span(text, 1, 4)])
    assert json.loads(result.content)["payload"]["message"] == "a___uoted\"z"


def test_identity_field_target_is_rejected_after_rewrite():
    content = _line()
    with pytest.raises(ScrubRejected, match="record_identity_changed"):
        rewrite_jsonl(content, [_span("alpha beta gamma", 0, 5, path=("payload", "message")),
                                _span("t", 0, 1, path=("timestamp",))])


def test_package_is_stdlib_only_and_has_no_dynamic_import_calls(tmp_path):
    package = Path(__file__).parents[2] / "src" / "cli_agent_orchestrator" / "transcript_scrub"
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] in allowed for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] in allowed or node.level > 0
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "__import__"
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert not (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "importlib"
                )

    isolated = tmp_path / "isolated"
    isolated.mkdir()
    target = isolated / "transcript_scrub"
    target.symlink_to(package, target_is_directory=True)
    env = {"PATH": os.environ.get("PATH", "")}
    completed = subprocess.run(
        [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, '.'); import transcript_scrub"],
        cwd=isolated,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
