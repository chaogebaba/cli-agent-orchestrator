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
table's set for that position — never subtract (checked here).

The table is POLICY (lintian precedent): it lives beside the positions but is a
``.toml`` (outside install.sh's ``profiles/*.md`` glob) and is never edited in
the same commit as a persona body. Inline markers ship VERBATIM to the agent —
HTML comments are inert in every provider context file and MUST NOT be stripped
by install or composition.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import frontmatter

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


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
    return ClauseTable(rules=rules, required=required)


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
        # Frontmatter `requires:` may carry free-text sentences OR clause ids;
        # only clause ids (present in the table) are treated as ADD directives.
        extra_ids = [r for r in requires_extra if isinstance(r, str) and r in table.rules]
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
