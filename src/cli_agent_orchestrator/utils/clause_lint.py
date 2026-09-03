"""F497 AC14 — required-clause lint driven by a supervisor-owned clause table.

Regression protection for persona BODIES (D11 dropped byte-identity for authored
personas) moves to a REQUIRED-SECTIONS lint: a clause table
(``profiles/positions/_clauses.toml``) owned OUTSIDE the personas under test maps

  * each clause id -> a match rule (a ``heading`` OR an inline ``marker``), and
  * each position -> the set of clause ids it MUST carry.

The lint composes every position (optionally per provider) and asserts every
required clause is present. It is FAIL-CLOSED both directions (r6 S3):

  * a clause id required by a position but absent from ``[clauses]``      -> error
  * a composed position that has no ``[required]`` row                    -> error
  * a ``[required]`` row naming a position with no position file          -> error

A position file's frontmatter ``requires:`` may only ADD ids on top of the
table's set for that position — never subtract (checked here). Each entry is
classified: an id-shaped token (``^[a-z0-9]+(-[a-z0-9]+)*$``, no whitespace)
MUST be a known clause id or the lint fails closed (naming the position + id);
anything else is a free-text prose sentence and is ignored.

The table is POLICY (lintian precedent): it lives beside the positions but is a
``.toml`` (outside install.sh's ``profiles/*.md`` glob) and is never edited in
the same commit as a persona body. Inline markers ship VERBATIM to the agent —
HTML comments are inert in every provider context file and MUST NOT be stripped
by install or composition.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import frontmatter

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


logger = logging.getLogger(__name__)


# A frontmatter `requires:` entry is treated as a clause-id reference only when
# it is id-shaped: lowercase alnum groups joined by single hyphens, no
# whitespace. Anything else is a free-text prose sentence and is ignored.
_ID_SHAPED = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# A position name is a lowercase identifier with underscores (e.g.
# ``empirical_reviewer``), unlike clause ids which use hyphens. Used by the
# AC17 budget lint to tell a forward-declared position budget key from a typo.
_POSITION_KEY_SHAPED = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")


class ClauseLintError(ValueError):
    """The clause table or a position persona failed the AC14 lint."""


@dataclass(frozen=True)
class ClauseRule:
    """One clause id's match rule: exactly one of heading/marker."""

    clause_id: str
    heading: str | None
    marker: str | None

    def matches(self, body: str) -> bool:
        if self.marker is not None:
            return self.marker in body
        if self.heading is not None:
            # Heading match: the exact heading line must appear.
            for line in body.splitlines():
                if line.strip() == self.heading.strip():
                    return True
            return False
        return False  # pragma: no cover - constructor guarantees one is set


@dataclass(frozen=True)
class ClauseTable:
    rules: Dict[str, ClauseRule]
    required: Dict[str, List[str]]
    budget: Dict[str, int]


# F497 AC17 (D13) — reserved ``[budget]`` keys that are NOT position names.
#   ``overlay``        — the byte ceiling applied to EVERY overlay fragment.
#   ``composed_slack`` — the allowance ABOVE ``position + overlay`` for a
#                        composed body (composed ≤ position_budget + overlay_budget
#                        + composed_slack).
_BUDGET_RESERVED_KEYS = ("overlay", "composed_slack")


def load_clause_table(path: Path) -> ClauseTable:
    """Parse + validate the clause table. Raises ClauseLintError on malformed input."""
    if not path.exists():
        raise ClauseLintError(f"clause table not found at {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ClauseLintError(f"clause table is not valid TOML: {exc}") from exc

    raw_clauses = data.get("clauses")
    if not isinstance(raw_clauses, dict) or not raw_clauses:
        raise ClauseLintError("clause table has no [clauses] section")
    rules: Dict[str, ClauseRule] = {}
    for cid, spec in raw_clauses.items():
        if not isinstance(spec, dict):
            raise ClauseLintError(f"clause '{cid}' is not a table")
        heading = spec.get("heading")
        marker = spec.get("marker")
        if (heading is None) == (marker is None):
            raise ClauseLintError(
                f"clause '{cid}' must set EXACTLY one of heading/marker (got "
                f"heading={heading!r}, marker={marker!r})"
            )
        rules[cid] = ClauseRule(
            cid,
            heading if isinstance(heading, str) else None,
            marker if isinstance(marker, str) else None,
        )

    raw_required = data.get("required")
    if not isinstance(raw_required, dict) or not raw_required:
        raise ClauseLintError("clause table has no [required] section")
    required: Dict[str, List[str]] = {}
    for pos, ids in raw_required.items():
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ClauseLintError(f"[required].{pos} must be a list of clause-id strings")
        for cid in ids:
            if cid not in rules:
                raise ClauseLintError(f"[required].{pos} references unknown clause id '{cid}'")
        required[pos] = list(ids)

    # F497 AC17 (D13) — optional ``[budget]`` table: position/reserved key -> int
    # bytes. Parsed permissively here (values must be positive ints); the
    # per-position "must have a row" and "unknown key" fail-closed checks live in
    # ``lint_budgets`` where the position corpus is known.
    raw_budget = data.get("budget")
    budget: Dict[str, int] = {}
    if raw_budget is not None:
        if not isinstance(raw_budget, dict):
            raise ClauseLintError("[budget] must be a table of name -> byte count")
        for key, val in raw_budget.items():
            if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
                raise ClauseLintError(
                    f"[budget].{key} must be a positive integer byte count (got {val!r})"
                )
            budget[key] = val
    return ClauseTable(rules=rules, required=required, budget=budget)


def _position_required_ids(
    table: ClauseTable, position: str, requires_extra: List[str]
) -> List[str]:
    """The effective required-id set for a position: table row + frontmatter adds.

    ``requires:`` may only ADD (all extras must be known clause ids); it can
    never remove a table-mandated id.
    """
    base = table.required.get(position)
    if base is None:
        raise ClauseLintError(
            f"position '{position}' has no [required] row in the clause table "
            f"(fail-closed: a new family's first commit must add its row)"
        )
    out = list(base)
    for cid in requires_extra:
        if cid not in table.rules:
            raise ClauseLintError(
                f"position '{position}' requires: names unknown clause id '{cid}'"
            )
        if cid not in out:
            out.append(cid)
    return out


def lint_positions(
    positions_dir: Path, clause_table_path: Path | None = None
) -> Dict[str, List[str]]:
    """Lint every position persona against the clause table.

    Returns ``{position: [matched clause ids]}`` on success. Raises
    ``ClauseLintError`` on the first failure (fail-closed both directions):
      * a table row naming a position with no file,
      * a position file with no table row,
      * a required clause not present in the composed body,
      * a ``requires:`` extra that is an unknown clause id.
    """
    table_path = clause_table_path or (positions_dir / "_clauses.toml")
    table = load_clause_table(table_path)

    position_files = {p.stem: p for p in positions_dir.glob("*.md")}

    # Fail-closed: a [required] row naming a position with no file is an error.
    for pos in table.required:
        if pos not in position_files:
            raise ClauseLintError(
                f"clause table [required] row '{pos}' names a position with no "
                f"{positions_dir}/{pos}.md file (fail-closed)"
            )

    results: Dict[str, List[str]] = {}
    for pos, path in position_files.items():
        parsed = frontmatter.loads(path.read_text(encoding="utf-8"))
        body = parsed.content
        requires_extra = parsed.metadata.get("requires") or []
        if not isinstance(requires_extra, list):
            raise ClauseLintError(f"position '{pos}' requires: must be a list")
        # Frontmatter `requires:` may carry free-text sentences OR clause ids.
        # Classify each entry: an id-shaped token MUST be a known clause id
        # (fail-closed — a bogus id is a mistake, not silently dropped);
        # anything else is prose and ignored.
        extra_ids: List[str] = []
        for r in requires_extra:
            if not isinstance(r, str):
                continue
            if _ID_SHAPED.match(r):
                if r not in table.rules:
                    raise ClauseLintError(
                        f"position '{pos}' requires: names unknown clause id '{r}'"
                    )
                extra_ids.append(r)
            # else: free-text prose sentence — legal, ignored.
        required_ids = _position_required_ids(table, pos, extra_ids)
        matched: List[str] = []
        for cid in required_ids:
            if not table.rules[cid].matches(body):
                raise ClauseLintError(
                    f"position '{pos}' is missing required clause '{cid}' "
                    f"(rule: {table.rules[cid].heading or table.rules[cid].marker!r})"
                )
            matched.append(cid)
        results[pos] = matched
    return results


# --------------------------------------------------------------------------
# F497 AC17 (D13) — persona byte-budget lint
# --------------------------------------------------------------------------
#
# ``clause_lint`` reads ``[budget]`` from ``positions/_clauses.toml`` and fails
# CLOSED when any position body, overlay fragment, or composed body exceeds its
# budget. Body bytes = the UTF-8 markdown body (frontmatter excluded),
# deterministic (≈4 B/token, no tokenizer dependency). The three ceilings:
#
#   * position body       ≤ ``[budget].<position>``
#   * each overlay body    ≤ ``[budget].overlay``
#   * composed body        ≤ ``[budget].<position>`` + ``[budget].overlay``
#                            + ``[budget].composed_slack``   (per (position, provider) cell)
#
# Fail-closed extras: a composed position with NO ``[budget]`` row is an error;
# a ``[budget]`` key that is neither a position file stem nor a reserved key
# (``overlay`` / ``composed_slack``) is an unknown-key error.


def _body_bytes(text: str) -> int:
    """UTF-8 byte length of a markdown fragment's BODY (frontmatter excluded).

    Measures ``frontmatter.loads(text).content`` — the exact body the D5 merge
    engine composes from (``compose_source_body`` strips only leading/trailing
    newlines, which do not change the budget class). Frontmatter is excluded so
    a metadata edit never spends the persona's prose budget.
    """
    return len(frontmatter.loads(text).content.encode("utf-8"))


def _overlay_provider_cells(overlays_dir: Path, position: str) -> Dict[str, List[Path]]:
    """Map each provider to its overlay fragment(s) applicable to ``position``.

    Mirrors the resolver's D4 layer order: ``overlays/<provider>.md`` (base
    overlay) then ``overlays/<provider>.<position>.md`` (per-position overlay).
    A provider is any overlay filename stem's leading segment; the returned list
    is in compose order (base first, per-position second) and omits absent
    files. Providers that touch this position AT ALL (either fragment) appear.
    """
    if not overlays_dir.exists():
        return {}
    providers: set[str] = set()
    for f in overlays_dir.glob("*.md"):
        stem = f.stem  # e.g. "codex" or "codex.empirical_reviewer"
        providers.add(stem.split(".", 1)[0])
    cells: Dict[str, List[Path]] = {}
    for provider in sorted(providers):
        frags: List[Path] = []
        base = overlays_dir / f"{provider}.md"
        if base.exists():
            frags.append(base)
        per_pos = overlays_dir / f"{provider}.{position}.md"
        if per_pos.exists():
            frags.append(per_pos)
        # Only a provider that supplies AT LEAST the per-position OR base overlay
        # forms a cell worth measuring; a provider with neither does not.
        if frags:
            cells[provider] = frags
    return cells


def lint_budgets(
    positions_dir: Path,
    overlays_dir: Path | None = None,
    clause_table_path: Path | None = None,
) -> Dict[str, int]:
    """Fail-closed AC17 byte-budget lint over positions + overlays + composed cells.

    Returns ``{measured-name: bytes}`` on success (position bodies, overlay
    fragments keyed ``overlay:<file>``, and composed cells keyed
    ``composed:<position>+<provider>``). Raises ``ClauseLintError`` on the first
    violation, naming the file, bytes, and budget.

    ``overlays_dir`` defaults to the ``overlays`` sibling of ``positions_dir``.
    When absent, only position-body budgets are checked (no overlay/composed
    cells to measure).
    """
    table_path = clause_table_path or (positions_dir / "_clauses.toml")
    table = load_clause_table(table_path)
    budget = table.budget
    if not budget:
        raise ClauseLintError(
            f"clause table {table_path} has no [budget] section (AC17 requires one)"
        )
    if overlays_dir is None:
        overlays_dir = positions_dir.parent / "overlays"

    position_files = {p.stem: p for p in positions_dir.glob("*.md")}

    # Fail-closed: every [budget] key must be a reserved key, an existing
    # position file, OR an id-shaped identifier (a legitimately forward-declared
    # position budget — the supervisor-owned policy table may carry a budget for
    # a position not yet extracted, e.g. design_reviewer). A key that is neither
    # reserved nor id-shaped is a typo and fails closed. An id-shaped key with no
    # position file AND no [required] row is a FORWARD DECLARATION: log a single
    # warning and continue (supervisor decision 2026-08-28, D13 clarification).
    for key in budget:
        if key in _BUDGET_RESERVED_KEYS or key in position_files:
            continue
        if not _POSITION_KEY_SHAPED.match(key):
            raise ClauseLintError(
                f"[budget] key '{key}' is neither a reserved key "
                f"{_BUDGET_RESERVED_KEYS}, an existing position file, nor an "
                f"id-shaped position name (fail-closed: unknown budget key)"
            )
        if key not in table.required:
            logger.warning("forward-declared budget key %s (no position file)", key)

    overlay_budget = budget.get("overlay")
    composed_slack = budget.get("composed_slack")
    results: Dict[str, int] = {}

    # 1. Position bodies. Fail-closed: a composed position with no budget row.
    for pos, path in sorted(position_files.items()):
        pos_budget = budget.get(pos)
        if pos_budget is None:
            raise ClauseLintError(
                f"position '{pos}' has no [budget] row in {table_path} "
                f"(fail-closed: every position needs a byte budget)"
            )
        body = path.read_text(encoding="utf-8")
        n = _body_bytes(body)
        if n > pos_budget:
            raise ClauseLintError(
                f"position '{pos}' body is {n} B, over its budget of {pos_budget} B " f"({path})"
            )
        results[pos] = n

    # 2. Overlay fragments (each ≤ overlay budget).
    if overlay_budget is not None and overlays_dir.exists():
        for ov in sorted(overlays_dir.glob("*.md")):
            n = _body_bytes(ov.read_text(encoding="utf-8"))
            if n > overlay_budget:
                raise ClauseLintError(
                    f"overlay '{ov.name}' body is {n} B, over the overlay budget "
                    f"of {overlay_budget} B ({ov})"
                )
            results[f"overlay:{ov.name}"] = n

    # 3. Composed cells (position + provider overlays ≤ position + overlay + slack).
    if overlay_budget is not None and composed_slack is not None:
        from cli_agent_orchestrator.utils.profile_composition import (
            Layer,
            compose_source_body,
        )

        for pos, path in sorted(position_files.items()):
            pos_budget = budget[pos]
            cell_ceiling = pos_budget + overlay_budget + composed_slack
            pos_body = frontmatter.loads(path.read_text(encoding="utf-8")).content
            for provider, frags in _overlay_provider_cells(overlays_dir, pos).items():
                layers = [Layer(kind=f"position:{pos}", metadata={}, body=pos_body)]
                for frag in frags:
                    fparsed = frontmatter.loads(frag.read_text(encoding="utf-8"))
                    layers.append(
                        Layer(
                            kind=f"overlay:{frag.stem}",
                            metadata=dict(fparsed.metadata),
                            body=fparsed.content,
                            provider=provider,
                            replaces=list(fparsed.metadata.get("replaces") or []),
                        )
                    )
                composed = compose_source_body(layers)
                n = len(composed.encode("utf-8"))
                if n > cell_ceiling:
                    raise ClauseLintError(
                        f"composed cell '{pos}+{provider}' body is {n} B, over its "
                        f"budget of {cell_ceiling} B (= position {pos_budget} + overlay "
                        f"{overlay_budget} + composed_slack {composed_slack})"
                    )
                results[f"composed:{pos}+{provider}"] = n

    return results
