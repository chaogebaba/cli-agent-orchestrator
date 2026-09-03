"""Decoded-span raw-token rewrite engine for JSONL transcripts.

This module deliberately uses only the Python standard library.  It knows
nothing about providers, artifact locations, or which content is eligible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, TypeAlias

JsonPathPart: TypeAlias = str | int
JsonPath: TypeAlias = tuple[JsonPathPart, ...]

NEUTRAL_REPLACEMENT_POLICY = "neutral-equal-codepoints-v1"
_NEUTRAL_CODEPOINT = "_"
_DECODER = json.JSONDecoder()


class ScrubRejected(ValueError):
    """The requested rewrite could not be proven structurally safe."""


@dataclass(frozen=True)
class Span:
    """One expected decoded-string interval, addressed inside a JSONL record."""

    line_number: int
    key_path: JsonPath
    start: int
    end: int
    replacement_policy: str
    expected_preimage_sha256: str
    rule_id: str = "supervisor.ad-hoc.v1"


@dataclass(frozen=True)
class _Token:
    start: int
    end: int
    decoded: str


@dataclass(frozen=True)
class _VerifiedInterval:
    start: int
    end: int
    policy: str
    rule_ids: tuple[str, ...]


@dataclass(frozen=True)
class ScrubResult:
    content: bytes
    span_count: int
    target_count: int
    rule_ids: tuple[str, ...]
    artifact_sha256_before: str
    artifact_sha256_after: str
    spans: tuple[Span, ...]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _scan_value(
    text: str,
    pos: int,
    path: JsonPath,
    tokens: dict[JsonPath, _Token],
) -> int:
    """Parse one JSON value while recording exact raw string-token bounds."""
    pos = _skip_ws(text, pos)
    if pos >= len(text):
        raise ScrubRejected("unexpected_end_of_json")
    marker = text[pos]
    if marker == '"':
        try:
            decoded, end = _DECODER.raw_decode(text, pos)
        except json.JSONDecodeError as exc:
            raise ScrubRejected("invalid_json_string") from exc
        if not isinstance(decoded, str):
            raise ScrubRejected("string_decoder_type_mismatch")
        if path in tokens:
            raise ScrubRejected("duplicate_json_path")
        tokens[path] = _Token(pos, end, decoded)
        return end
    if marker == "{":
        pos = _skip_ws(text, pos + 1)
        if pos < len(text) and text[pos] == "}":
            return pos + 1
        seen_keys: set[str] = set()
        while True:
            if pos >= len(text) or text[pos] != '"':
                raise ScrubRejected("invalid_object_key")
            try:
                key, key_end = _DECODER.raw_decode(text, pos)
            except json.JSONDecodeError as exc:
                raise ScrubRejected("invalid_object_key") from exc
            if not isinstance(key, str) or key in seen_keys:
                raise ScrubRejected("duplicate_object_key")
            seen_keys.add(key)
            pos = _skip_ws(text, key_end)
            if pos >= len(text) or text[pos] != ":":
                raise ScrubRejected("missing_object_colon")
            pos = _scan_value(text, pos + 1, path + (key,), tokens)
            pos = _skip_ws(text, pos)
            if pos < len(text) and text[pos] == "}":
                return pos + 1
            if pos >= len(text) or text[pos] != ",":
                raise ScrubRejected("invalid_object_separator")
            pos = _skip_ws(text, pos + 1)
    if marker == "[":
        pos = _skip_ws(text, pos + 1)
        if pos < len(text) and text[pos] == "]":
            return pos + 1
        index = 0
        while True:
            pos = _scan_value(text, pos, path + (index,), tokens)
            index += 1
            pos = _skip_ws(text, pos)
            if pos < len(text) and text[pos] == "]":
                return pos + 1
            if pos >= len(text) or text[pos] != ",":
                raise ScrubRejected("invalid_array_separator")
            pos = _skip_ws(text, pos + 1)
    try:
        _value, end = _DECODER.raw_decode(text, pos)
    except json.JSONDecodeError as exc:
        raise ScrubRejected("invalid_json_value") from exc
    return end


def _string_tokens(line: str) -> dict[JsonPath, _Token]:
    tokens: dict[JsonPath, _Token] = {}
    end = _scan_value(line, 0, (), tokens)
    if _skip_ws(line, end) != len(line):
        raise ScrubRejected("trailing_json_bytes")
    return tokens


def _line_body_and_ending(raw_line: bytes) -> tuple[bytes, bytes]:
    if raw_line.endswith(b"\r\n"):
        return raw_line[:-2], b"\r\n"
    if raw_line.endswith(b"\n") or raw_line.endswith(b"\r"):
        return raw_line[:-1], raw_line[-1:]
    return raw_line, b""


def _parse_records(content: bytes) -> tuple[list[Any], list[bytes]]:
    lines = content.splitlines(keepends=True)
    if not lines and content:
        lines = [content]
    records: list[Any] = []
    for raw_line in lines:
        body, _ending = _line_body_and_ending(raw_line)
        if not body:
            raise ScrubRejected("blank_jsonl_record")
        try:
            records.append(json.loads(body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScrubRejected("invalid_jsonl_record") from exc
    return records, lines


def identity_sequence(value: Any) -> tuple[tuple[JsonPath, Any], ...]:
    """Return recursive identity-bearing fields in deterministic traversal order."""
    found: list[tuple[JsonPath, Any]] = []

    def visit(node: Any, path: JsonPath) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = path + (key,)
                if key in {"type", "id", "timestamp"} or key.endswith("_id"):
                    found.append((child_path, child))
                visit(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, path + (index,))

    visit(value, ())
    return tuple(found)


def _path_sort_key(path: JsonPath) -> tuple[str, ...]:
    return tuple(f"i:{part}" if isinstance(part, int) else f"s:{part}" for part in path)


def _verify_and_merge(spans: list[Span], decoded: str) -> list[_VerifiedInterval]:
    identities: set[tuple[int, int]] = set()
    verified: list[_VerifiedInterval] = []
    for span in spans:
        if span.replacement_policy != NEUTRAL_REPLACEMENT_POLICY:
            raise ScrubRejected("unknown_replacement_policy")
        if span.start < 0 or span.end <= span.start or span.end > len(decoded):
            raise ScrubRejected("span_offsets_invalid")
        identity = (span.start, span.end)
        if identity in identities:
            raise ScrubRejected("duplicate_span")
        identities.add(identity)
        if _sha256_text(decoded[span.start : span.end]) != span.expected_preimage_sha256:
            raise ScrubRejected("span_preimage_mismatch")
        verified.append(
            _VerifiedInterval(span.start, span.end, span.replacement_policy, (span.rule_id,))
        )

    verified.sort(key=lambda item: (item.start, item.end))
    merged: list[_VerifiedInterval] = []
    for item in verified:
        if not merged or item.start > merged[-1].end:
            merged.append(item)
            continue
        previous = merged[-1]
        if previous.policy != item.policy:
            raise ScrubRejected("conflicting_overlap")
        merged[-1] = _VerifiedInterval(
            previous.start,
            max(previous.end, item.end),
            previous.policy,
            tuple(sorted(set(previous.rule_ids + item.rule_ids))),
        )
    return merged


def rewrite_jsonl(content: bytes, spans: Iterable[Span]) -> ScrubResult:
    """Apply verified decoded spans while preserving every non-target token byte."""
    original_records, raw_lines = _parse_records(content)
    requested = tuple(spans)
    grouped: dict[tuple[int, JsonPath], list[Span]] = {}
    for span in requested:
        if span.line_number <= 0:
            raise ScrubRejected("line_number_invalid")
        grouped.setdefault((span.line_number, span.key_path), []).append(span)

    rewritten = list(raw_lines)
    ordered_targets = sorted(
        grouped,
        key=lambda item: (item[0], _path_sort_key(item[1])),
    )
    for line_number, key_path in ordered_targets:
        if line_number > len(rewritten):
            raise ScrubRejected("line_number_out_of_range")
        body, ending = _line_body_and_ending(rewritten[line_number - 1])
        try:
            line = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ScrubRejected("record_not_utf8") from exc
        token = _string_tokens(line).get(key_path)
        if token is None:
            raise ScrubRejected("target_is_not_exact_string")
        intervals = _verify_and_merge(grouped[(line_number, key_path)], token.decoded)
        replacement_value = token.decoded
        for interval in sorted(intervals, key=lambda item: item.start, reverse=True):
            filler = _NEUTRAL_CODEPOINT * (interval.end - interval.start)
            replacement_value = (
                replacement_value[: interval.start]
                + filler
                + replacement_value[interval.end :]
            )
        encoded_token = json.dumps(replacement_value, ensure_ascii=False, separators=(",", ":"))
        new_line = line[: token.start] + encoded_token + line[token.end :]
        rewritten[line_number - 1] = new_line.encode("utf-8") + ending

    result_content = b"".join(rewritten)
    rewritten_records, rewritten_lines = _parse_records(result_content)
    if len(raw_lines) != len(rewritten_lines):
        raise ScrubRejected("line_count_changed")
    before_identity = tuple(identity_sequence(record) for record in original_records)
    after_identity = tuple(identity_sequence(record) for record in rewritten_records)
    if before_identity != after_identity:
        raise ScrubRejected("record_identity_changed")
    return ScrubResult(
        content=result_content,
        span_count=len(requested),
        target_count=len(grouped),
        rule_ids=tuple(sorted({span.rule_id for span in requested})),
        artifact_sha256_before=hashlib.sha256(content).hexdigest(),
        artifact_sha256_after=hashlib.sha256(result_content).hexdigest(),
        spans=requested,
    )
