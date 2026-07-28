"""Transactional, post-condition-checked Markdown edits for ``cao fold``."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from markdown_it import MarkdownIt
from markdown_it.token import Token

Operation = Literal["replace", "after", "strike"]
FootprintKind = Literal["span", "point"]
ValidatorKind = Literal["P5", "P6"]


class FoldUsageError(ValueError):
    """The requested edit is malformed or has conflicting footprints."""


class FoldPostconditionError(RuntimeError):
    """The candidate buffer failed one or more post-conditions."""

    def __init__(self, messages: Sequence[str]) -> None:
        self.messages = tuple(messages)
        super().__init__("\n".join(self.messages))


@dataclass(frozen=True)
class FoldHunk:
    anchor: str
    operation: Operation
    value: str | None = None
    expect_count: int = 1


@dataclass(frozen=True)
class FoldResult:
    old_sha: str
    new_sha: str
    violations: tuple[str, ...] = ()
    check_only: bool = False


@dataclass
class _Edit:
    edit_id: int
    hunk_index: int
    operation: Operation
    start: int
    end: int
    emitted: bytes
    footprint_kind: FootprintKind
    footprint_start: int
    footprint_end: int
    post_start: int = 0
    post_end: int = 0


@dataclass(frozen=True)
class _Segment:
    post_start: int
    post_end: int
    pre_start: int | None
    pre_end: int | None
    edit_id: int | None


@dataclass(frozen=True)
class _Item:
    start: int
    end: int
    ordinal: int
    identity: tuple[str, ...]
    marker: int | None = None


@dataclass(frozen=True)
class _Container:
    container_id: int
    kind: ValidatorKind
    depth: int | None
    start: int
    end: int
    items: tuple[_Item, ...]

    @property
    def partition(self) -> tuple[ValidatorKind, int | None]:
        return (self.kind, self.depth if self.kind == "P6" else None)


@dataclass(frozen=True)
class _StructuralViolation:
    condition: ValidatorKind
    container_id: int
    item_ordinal: int
    item_start: int
    item_end: int
    identity: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class _Structure:
    containers: tuple[_Container, ...]
    violations: tuple[_StructuralViolation, ...]


_TOKEN_RE = re.compile(r"§\d+|[\w]+|[^\w\s]", re.UNICODE)
_LIST_MARKER_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<number>\d+)[.)][ \t]+")


def _schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "schemas" / "fold_hunks.schema.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FoldUsageError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def parse_hunks_document(data: bytes) -> list[FoldHunk]:
    """Decode and validate the closed stdin document contract."""
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise FoldUsageError(f"stdin is not valid UTF-8: {exc}") from exc
    try:
        document = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except FoldUsageError:
        raise
    except json.JSONDecodeError as exc:
        raise FoldUsageError(f"malformed JSON: {exc.msg} at byte {exc.pos}") from exc

    errors = sorted(Draft202012Validator(_schema()).iter_errors(document), key=_schema_error_key)
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "(root)"
        raise FoldUsageError(f"stdin schema error at {location}: {error.message}")

    assert isinstance(document, dict)
    raw_hunks = document["hunks"]
    assert isinstance(raw_hunks, list)
    hunks: list[FoldHunk] = []
    for raw in raw_hunks:
        assert isinstance(raw, dict)
        operation = cast(
            Operation,
            next(key for key in ("replace", "after", "strike") if key in raw),
        )
        value = raw.get(operation)
        if isinstance(value, str):
            try:
                value.encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                raise FoldUsageError(f"hunk operand is not UTF-8 encodable: {exc}") from exc
        hunks.append(
            FoldHunk(
                anchor=raw["anchor"],
                operation=operation,
                value=value if isinstance(value, str) else None,
                expect_count=raw.get("expect_count", 1),
            )
        )
    return hunks


def _schema_error_key(error: Any) -> tuple[list[str], str]:
    return ([str(part) for part in error.absolute_path], error.message)


def fold_file(path: Path, hunks: Sequence[FoldHunk]) -> FoldResult:
    """Apply all hunks to one immutable pre-state and atomically replace the file."""
    _validate_target(path)
    for hunk_index, hunk in enumerate(hunks, start=1):
        if not hunk.anchor:
            raise FoldUsageError(f"hunk {hunk_index}: anchor must not be empty")
        if hunk.expect_count < 1:
            raise FoldUsageError(f"hunk {hunk_index}: expect_count must be at least 1")
    pre = _read_markdown(path)
    old_sha = _sha(pre)
    edits, failures = _resolve_edits(path, pre, hunks)
    if failures:
        raise FoldPostconditionError(failures)
    conflict = _footprint_conflict(edits)
    if conflict is not None:
        raise FoldUsageError(conflict)

    post, segments = _assemble(pre, edits)
    failures.extend(_edit_postconditions(path, pre, edits))
    failures.extend(_structural_delta_failures(path, pre, post, segments, edits))
    if failures:
        raise FoldPostconditionError(failures)

    _atomic_write(path, post)
    return FoldResult(old_sha=old_sha, new_sha=_sha(post))


def check_file(path: Path) -> FoldResult:
    """Report absolute P5/P6 violations without editing or failing the command."""
    _validate_target(path)
    data = _read_markdown(path)
    structure = _parse_structure(data)
    return FoldResult(
        old_sha=_sha(data),
        new_sha=_sha(data),
        violations=tuple(violation.message for violation in structure.violations),
        check_only=True,
    )


def _validate_target(path: Path) -> None:
    if path.suffix.lower() != ".md":
        raise FoldUsageError(f"{path}: cao fold edits UTF-8 Markdown files only")
    if not path.is_file():
        raise FoldUsageError(f"{path}: file does not exist")


def _read_markdown(path: Path) -> bytes:
    try:
        data = path.read_bytes()
        data.decode("utf-8", "strict")
        return data
    except UnicodeDecodeError as exc:
        raise FoldUsageError(f"{path}: file is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise FoldUsageError(f"{path}: cannot read file: {exc}") from exc


def _resolve_edits(
    path: Path, pre: bytes, hunks: Sequence[FoldHunk]
) -> tuple[list[_Edit], list[str]]:
    edits: list[_Edit] = []
    failures: list[str] = []
    edit_id = 0
    for hunk_index, hunk in enumerate(hunks, start=1):
        anchor = hunk.anchor.encode("utf-8")
        matches = _non_overlapping_matches(pre, anchor)
        if len(matches) != hunk.expect_count:
            failures.append(
                f"{path}: P1 failed for hunk {hunk_index}: expected anchor count "
                f"{hunk.expect_count}, observed {len(matches)}; hint: use an exact, "
                "unique anchor or declare --expect-count"
            )
            continue
        for start in matches:
            end = start + len(anchor)
            if hunk.operation == "replace":
                emitted = (hunk.value or "").encode("utf-8")
                footprint_kind: FootprintKind = "span"
                footprint_start, footprint_end = start, end
            elif hunk.operation == "strike":
                emitted = b"~~" + anchor + b"~~"
                footprint_kind = "span"
                footprint_start, footprint_end = start, end
            else:
                emitted = (hunk.value or "").encode("utf-8")
                footprint_kind = "point"
                footprint_start = footprint_end = end
            edits.append(
                _Edit(
                    edit_id=edit_id,
                    hunk_index=hunk_index,
                    operation=hunk.operation,
                    start=start,
                    end=end,
                    emitted=emitted,
                    footprint_kind=footprint_kind,
                    footprint_start=footprint_start,
                    footprint_end=footprint_end,
                )
            )
            edit_id += 1
    return edits, failures


def _non_overlapping_matches(data: bytes, anchor: bytes) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        position = data.find(anchor, cursor)
        if position < 0:
            return positions
        positions.append(position)
        cursor = position + len(anchor)


def _footprint_conflict(edits: Sequence[_Edit]) -> str | None:
    for index, left in enumerate(edits):
        for right in edits[index + 1 :]:
            if left.footprint_kind == right.footprint_kind == "span":
                if max(left.footprint_start, right.footprint_start) < min(
                    left.footprint_end, right.footprint_end
                ):
                    return _conflict_message(left, right, "span interior-overlap")
                continue
            if left.footprint_kind == right.footprint_kind == "point":
                if left.footprint_start == right.footprint_start:
                    return _conflict_message(left, right, "two points at one offset")
                continue
            point = left if left.footprint_kind == "point" else right
            span = right if point is left else left
            if span.footprint_start <= point.footprint_start <= span.footprint_end:
                relation = (
                    "point on span endpoint"
                    if point.footprint_start in {span.footprint_start, span.footprint_end}
                    else "point strictly inside span"
                )
                return _conflict_message(left, right, relation)
    return None


def _conflict_message(left: _Edit, right: _Edit, reason: str) -> str:
    return (
        f"edit-footprint conflict ({reason}) between hunks "
        f"{left.hunk_index} and {right.hunk_index}"
    )


def _assemble(pre: bytes, edits: Sequence[_Edit]) -> tuple[bytes, list[_Segment]]:
    ordered = sorted(
        edits,
        key=lambda edit: (
            edit.footprint_start,
            0 if edit.footprint_kind == "span" else 1,
            edit.edit_id,
        ),
    )
    output = bytearray()
    segments: list[_Segment] = []
    cursor = 0
    for edit in ordered:
        insertion = edit.operation == "after"
        position = edit.end if insertion else edit.start
        if cursor < position:
            start = len(output)
            output.extend(pre[cursor:position])
            segments.append(_Segment(start, len(output), cursor, position, None))
        edit.post_start = len(output)
        output.extend(edit.emitted)
        edit.post_end = len(output)
        if edit.emitted:
            pre_start = edit.start if edit.footprint_kind == "span" else None
            pre_end = edit.end if edit.footprint_kind == "span" else None
            segments.append(
                _Segment(edit.post_start, edit.post_end, pre_start, pre_end, edit.edit_id)
            )
        cursor = position if insertion else edit.end
    if cursor < len(pre):
        start = len(output)
        output.extend(pre[cursor:])
        segments.append(_Segment(start, len(output), cursor, len(pre), None))
    return bytes(output), segments


def _edit_postconditions(path: Path, pre: bytes, edits: Sequence[_Edit]) -> list[str]:
    failures: list[str] = []
    for edit in edits:
        if edit.operation == "replace" and edit.emitted == pre[edit.start : edit.end]:
            failures.append(
                f"{path}: P2 failed for hunk {edit.hunk_index}: expected old text gone at "
                "the matched span, observed it unchanged; hint: supply a real replacement"
            )
        insertion = edit.end if edit.operation == "after" else edit.start
        consumed_end = insertion if edit.operation == "after" else edit.end
        candidate = pre[:insertion] + edit.emitted + pre[consumed_end:]
        for seam in {insertion, insertion + len(edit.emitted)}:
            violation = _seam_violation(candidate, seam)
            if violation is not None:
                failures.append(
                    f"{path}: P3 failed for hunk {edit.hunk_index}: expected an intact seam, "
                    f"observed {violation}; hint: include enough anchor context to own the join"
                )
    return failures


def _seam_violation(data: bytes, seam: int) -> str | None:
    if seam <= 0 or seam >= len(data):
        return None
    text = data.decode("utf-8")
    char_seam = len(data[:seam].decode("utf-8"))
    if not _same_markdown_block(text, char_seam):
        return None
    line_start = text.rfind("\n", 0, char_seam) + 1
    line_end = text.find("\n", char_seam)
    if line_end < 0:
        line_end = len(text)
    left_tokens = _TOKEN_RE.findall(text[line_start:char_seam])
    right_tokens = _TOKEN_RE.findall(text[char_seam:line_end])
    for width in range(min(len(left_tokens), len(right_tokens)), 0, -1):
        repeated = left_tokens[-width:]
        if repeated == right_tokens[:width] and any(_word_token(token) for token in repeated):
            return f"duplicated token sequence {' '.join(repeated)!r} at byte {seam}"
    return None


def _same_markdown_block(text: str, seam: int) -> bool:
    if text[max(0, seam - 2) : seam + 2].count("\n") >= 2:
        return False
    if seam > 0 and text[seam - 1] == "\n":
        right_line = text[seam : text.find("\n", seam) if "\n" in text[seam:] else len(text)]
        if re.match(r"\s*(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|\||```|~~~)", right_line):
            return False
    if seam < len(text) and text[seam] == "\n":
        left_start = text.rfind("\n", 0, seam) + 1
        if re.match(r"\s*(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|\||```|~~~)", text[left_start:seam]):
            return False
    return True


def _word_token(value: str) -> bool:
    return any(character.isalnum() for character in value)


def _structural_delta_failures(
    path: Path,
    pre: bytes,
    post: bytes,
    segments: Sequence[_Segment],
    edits: Sequence[_Edit],
) -> list[str]:
    before = _parse_structure(pre)
    after = _parse_structure(post)
    blocking = _unmapped_post_violations(before, after, segments, edits)
    return [
        f"{path}: {violation.condition} failed: expected no new structural violation, "
        f"observed {violation.message}; hint: repair the edited table/list in the same fold"
        for violation in blocking
    ]


def _parse_structure(data: bytes) -> _Structure:
    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    offsets = _line_offsets(lines)
    parser = MarkdownIt("commonmark").enable("table")
    tokens = parser.parse(text)
    headings = _heading_stacks(tokens)
    containers: list[_Container] = []
    violations: list[_StructuralViolation] = []

    for token_index, token in enumerate(tokens):
        if token.type == "table_open" and token.map is not None:
            container, found = _table_container(
                len(containers), token_index, token, tokens, lines, offsets, headings
            )
            containers.append(container)
            violations.extend(found)
        elif token.type == "ordered_list_open" and token.map is not None:
            depth = _list_depth(tokens, token_index)
            container, found = _list_container(
                len(containers), token_index, token, depth, tokens, lines, offsets, headings
            )
            containers.append(container)
            violations.extend(found)
    return _Structure(tuple(containers), tuple(violations))


def _line_offsets(lines: Sequence[str]) -> list[int]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    return offsets


def _heading_stacks(tokens: Sequence[Token]) -> list[tuple[int, tuple[str, ...]]]:
    stack: list[str] = []
    result: list[tuple[int, tuple[str, ...]]] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        level = int(token.tag[1])
        title = tokens[index + 1].content if index + 1 < len(tokens) else ""
        stack = stack[: level - 1]
        stack.append(_normalize(title))
        result.append((token.map[0], tuple(stack)))
    return result


def _heading_at(line: int, headings: Sequence[tuple[int, tuple[str, ...]]]) -> tuple[str, ...]:
    current: tuple[str, ...] = ()
    for heading_line, stack in headings:
        if heading_line > line:
            break
        current = stack
    return current


def _table_container(
    container_id: int,
    token_index: int,
    token: Token,
    tokens: Sequence[Token],
    lines: Sequence[str],
    offsets: Sequence[int],
    headings: Sequence[tuple[int, tuple[str, ...]]],
) -> tuple[_Container, list[_StructuralViolation]]:
    assert token.map is not None
    start_line, end_line = token.map
    row_lines: list[int] = []
    for child in tokens[token_index + 1 :]:
        if child.type == "table_close" and child.level == token.level:
            break
        if child.type == "tr_open" and child.map is not None:
            row_lines.append(child.map[0])
    assert row_lines
    header_cells = _split_table_row(lines[row_lines[0]])
    header_identity = _normalize(" | ".join(header_cells))
    items: list[_Item] = []
    violations: list[_StructuralViolation] = []
    for ordinal, line_number in enumerate(row_lines[1:], start=1):
        raw = lines[line_number]
        cells = _split_table_row(raw)
        identity = (header_identity, _normalize(raw))
        item = _Item(
            start=offsets[line_number],
            end=offsets[line_number + 1],
            ordinal=ordinal,
            identity=identity,
        )
        items.append(item)
        is_lazy_continuation = "|" not in raw
        if len(cells) != len(header_cells) and not is_lazy_continuation:
            violations.append(
                _StructuralViolation(
                    condition="P5",
                    container_id=container_id,
                    item_ordinal=ordinal,
                    item_start=item.start,
                    item_end=item.end,
                    identity=identity,
                    message=(
                        f"table body row {ordinal} has {len(cells)} cells; "
                        f"header has {len(header_cells)}"
                    ),
                )
            )
    return (
        _Container(
            container_id,
            "P5",
            None,
            offsets[start_line],
            offsets[end_line],
            tuple(items),
        ),
        violations,
    )


def _split_table_row(raw: str) -> list[str]:
    line = raw.rstrip("\r\n").strip()
    cells: list[str] = []
    current: list[str] = []
    code_run: int | None = None
    index = 0
    while index < len(line):
        character = line[index]
        if character == "`":
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            run = end - index
            if code_run is None:
                code_run = run
            elif code_run == run:
                code_run = None
            current.extend(line[index:end])
            index = end
            continue
        if character == "|" and code_run is None and not _escaped_pipe(line, index):
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        index += 1
    cells.append("".join(current).strip())
    if line.startswith("|"):
        cells = cells[1:]
    if line.endswith("|") and not _escaped_pipe(line, len(line) - 1):
        cells = cells[:-1]
    return cells


def _escaped_pipe(line: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and line[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _list_depth(tokens: Sequence[Token], target: int) -> int:
    depth = 0
    stack: list[str] = []
    for token in tokens[:target]:
        if token.type in {"ordered_list_open", "bullet_list_open"}:
            stack.append(token.type)
        elif token.type in {"ordered_list_close", "bullet_list_close"} and stack:
            stack.pop()
    depth = len(stack)
    return depth


def _list_container(
    container_id: int,
    token_index: int,
    token: Token,
    depth: int,
    tokens: Sequence[Token],
    lines: Sequence[str],
    offsets: Sequence[int],
    headings: Sequence[tuple[int, tuple[str, ...]]],
) -> tuple[_Container, list[_StructuralViolation]]:
    assert token.map is not None
    start_line, end_line = token.map
    heading = _heading_at(start_line, headings)
    items: list[_Item] = []
    for child in tokens[token_index + 1 :]:
        if child.type == "ordered_list_close" and child.level == token.level:
            break
        if (
            child.type == "list_item_open"
            and child.level == token.level + 1
            and child.map is not None
        ):
            line_number = child.map[0]
            marker = _LIST_MARKER_RE.match(lines[line_number])
            if marker is None:
                continue
            number = int(marker.group("number"))
            raw = lines[line_number]
            items.append(
                _Item(
                    start=offsets[child.map[0]],
                    end=offsets[child.map[1]],
                    ordinal=len(items) + 1,
                    identity=heading + (_normalize(raw),),
                    marker=number,
                )
            )
    violations: list[_StructuralViolation] = []
    for previous, current in zip(items, items[1:]):
        assert previous.marker is not None and current.marker is not None
        if current.marker != previous.marker + 1:
            violations.append(
                _StructuralViolation(
                    condition="P6",
                    container_id=container_id,
                    item_ordinal=current.ordinal,
                    item_start=current.start,
                    item_end=current.end,
                    identity=current.identity,
                    message=(
                        f"ordered-list item {current.ordinal} is numbered {current.marker}; "
                        f"expected {previous.marker + 1}"
                    ),
                )
            )
    return (
        _Container(
            container_id,
            "P6",
            depth,
            offsets[start_line],
            offsets[end_line],
            tuple(items),
        ),
        violations,
    )


def _normalize(value: str) -> str:
    collapsed = " ".join(value.split())
    return collapsed.rstrip(".,;:!?")


def _unmapped_post_violations(
    before: _Structure,
    after: _Structure,
    segments: Sequence[_Segment],
    edits: Sequence[_Edit],
) -> list[_StructuralViolation]:
    pre_by_id = {container.container_id: container for container in before.containers}
    post_by_id = {container.container_id: container for container in after.containers}
    edges: dict[tuple[str, int], set[tuple[str, int]]] = defaultdict(set)
    for post in after.containers:
        for pre in before.containers:
            if pre.partition != post.partition:
                continue
            if _post_has_pre_origin(post, pre, segments):
                left = ("pre", pre.container_id)
                right = ("post", post.container_id)
                edges[left].add(right)
                edges[right].add(left)

    violations_by_pre_item = {
        (violation.container_id, violation.item_ordinal): violation
        for violation in before.violations
    }
    blocking: list[_StructuralViolation] = []
    for violation in after.violations:
        component = _component(("post", violation.container_id), edges)
        pre_ids = [node[1] for node in component if node[0] == "pre"]
        post_ids = [node[1] for node in component if node[0] == "post"]
        if len(pre_ids) != 1 or len(post_ids) != 1:
            blocking.append(violation)
            continue
        pre = pre_by_id[pre_ids[0]]
        post = post_by_id[post_ids[0]]
        if not _violation_maps_to_pre(
            violation,
            pre,
            post,
            segments,
            edits,
            violations_by_pre_item,
        ):
            blocking.append(violation)
    return blocking


def _post_has_pre_origin(post: _Container, pre: _Container, segments: Sequence[_Segment]) -> bool:
    for segment in segments:
        if segment.pre_start is None or segment.pre_end is None:
            continue
        origin = _segment_pre_origin(post.start, post.end, segment)
        if origin is not None and _intersects(pre.start, pre.end, *origin):
            return True
    return False


def _component(
    start: tuple[str, int], edges: dict[tuple[str, int], set[tuple[str, int]]]
) -> set[tuple[str, int]]:
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in edges.get(node, set()):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return seen


def _violation_maps_to_pre(
    violation: _StructuralViolation,
    pre: _Container,
    post: _Container,
    segments: Sequence[_Segment],
    edits: Sequence[_Edit],
    pre_violations: dict[tuple[int, int], _StructuralViolation],
) -> bool:
    # Unchanged source bytes retain their exact item origin.
    for segment in segments:
        if segment.edit_id is not None or segment.pre_start is None or segment.pre_end is None:
            continue
        origin = _segment_pre_origin(violation.item_start, violation.item_end, segment)
        if origin is None:
            continue
        for item in pre.items:
            prior = pre_violations.get((pre.container_id, item.ordinal))
            if (
                _intersects(item.start, item.end, *origin)
                and prior is not None
                and prior.identity == violation.identity
            ):
                return True

    # Span replacements pair affected rows/direct items in document order.
    edit_by_id = {edit.edit_id: edit for edit in edits}
    for segment in segments:
        if segment.edit_id is None or not _intersects(
            violation.item_start, violation.item_end, segment.post_start, segment.post_end
        ):
            continue
        edit = edit_by_id[segment.edit_id]
        if edit.footprint_kind != "span":
            continue
        pre_items = [
            item for item in pre.items if _intersects(item.start, item.end, edit.start, edit.end)
        ]
        post_items = [
            item
            for item in post.items
            if _intersects(item.start, item.end, edit.post_start, edit.post_end)
        ]
        for old, new in zip(pre_items, post_items):
            prior = pre_violations.get((pre.container_id, old.ordinal))
            if (
                new.ordinal == violation.item_ordinal
                and prior is not None
                and prior.identity == violation.identity
            ):
                return True
    return False


def _segment_pre_origin(
    post_start: int, post_end: int, segment: _Segment
) -> tuple[int, int] | None:
    overlap_start = max(post_start, segment.post_start)
    overlap_end = min(post_end, segment.post_end)
    if overlap_start >= overlap_end or segment.pre_start is None or segment.pre_end is None:
        return None
    if segment.edit_id is not None:
        return (segment.pre_start, segment.pre_end)
    return (
        segment.pre_start + overlap_start - segment.post_start,
        segment.pre_start + overlap_end - segment.post_start,
    )


def _intersects(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return max(left_start, right_start) < min(left_end, right_end)


def _atomic_write(path: Path, data: bytes) -> None:
    mode = path.stat().st_mode
    directory = path.parent
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=directory, prefix=".cao-fold-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode & 0o7777)
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
