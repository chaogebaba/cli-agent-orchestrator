"""AC-LITE-2 — infrastructure never reaches for a skill-owned knowledge path.

``wp-arch-modular-core.md`` A.5: "no non-test module under ``src/cli_agent_orchestrator/``,
excluding ``cli/orchestrator_main.py`` and the three commands it owns, opens/stats/globs/
parent-walks any path in the A.2 closed list."

The mutation arm is the point of the file. A boundary test that only ever runs green on a
clean tree proves nothing about whether it can see a violation, so every one of the four
reads slice 1 removed is replayed here VERBATIM from ``ad4339e9`` and must come back RED.

The snippets are embedded rather than read back through ``git show`` on purpose: the test
has to keep working from an unpacked sdist, where there is no git history to consult.
"""

from __future__ import annotations

from pathlib import Path
from test.helpers.knowledge_io_scan import (
    SKILL_ALLOWLIST,
    Finding,
    scan_python,
    scan_source_tree,
)

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Verbatim from ad4339e9 — the four reads slice 1 removed. Each pair is
# (name, source), where `name` cites the pre-slice-1 location the amendment names.
NAMED_MUTANTS: tuple[tuple[str, str], ...] = (
    (
        "cli/commands/ledger.py:85 — parent-walk for orchestrator/HANDOFF.md",
        """
from pathlib import Path


def check() -> None:
    path = find_workspace_file(Path.cwd(), "orchestrator/HANDOFF.md")
    if path is None:
        path = find_workspace_file(Path.cwd(), "HANDOFF.md")
""",
    ),
    (
        "services/fold_service.py:465 — glob of orchestrator/blueprints and doctrine",
        """
from pathlib import Path


def _p10_corpus_paths(root: Path) -> tuple[Path, ...]:
    bp_dir = root / "orchestrator" / "blueprints"
    if not bp_dir.is_dir():
        bp_dir = root / "blueprints"
    candidates = list(bp_dir.glob("*.md")) if bp_dir.is_dir() else []
    candidates.extend((root / "doctrine").rglob("*.md"))
    tips = root / "orchestrator" / "GOLDEN-TIPS.md"
    if not tips.is_file():
        tips = root / "GOLDEN-TIPS.md"
    if tips.is_file():
        candidates.append(tips)
    return tuple(candidates)
""",
    ),
    (
        "services/session_service.py:76-78 — is_dir() probe for an orchestrator/ subdir",
        """
import os
from pathlib import Path


def canonical_session_env(working_directory: str) -> Path:
    base = Path(working_directory or os.getcwd()).resolve()
    orch_sub = base / "orchestrator"
    if orch_sub.is_dir():
        return orch_sub / "tmp" / "orch"
    return base / "tmp" / "orch"
""",
    ),
    (
        "cli/commands/redeploy.py:117 — copy of <repo>/orchestrator/routing.toml",
        """
from pathlib import Path


def _sync_composition_stores(workspace_root: Path) -> None:
    routing = workspace_root / "orchestrator" / "routing.toml"
    if routing.is_file():
        local_agent_store_dir().mkdir(parents=True, exist_ok=True)
        _atomic_copy(routing, routing_toml_path())
""",
    ),
)

# The negative control. Every one of these does real I/O, or names a knowledge path in
# prose, and none of them is a boundary violation — so all must stay GREEN. Without this
# arm the scan could be made to pass by widening it until it flags nothing, or to look
# strong by flagging every mention of the word "orchestrator".
ALLOWED_SHAPES: tuple[tuple[str, str], ...] = (
    (
        "generic safety/journal/diag I/O on neutral paths",
        """
from pathlib import Path


def write_journal(root: Path) -> None:
    (root / "journal").mkdir(parents=True, exist_ok=True)
    (root / "journal" / "delivery.jsonl").write_text("{}\\n")
    (root / "tmp" / "orch" / "diag.json").read_text()
""",
    ),
    (
        "a historical citation in a docstring is prose, not I/O",
        '''
from pathlib import Path


def load(root: Path) -> str:
    """Restores the behaviour described in orchestrator/GOLDEN-TIPS.md (2026-09-11).

    See orchestrator/blueprints/wp-arch-modular-core.md A.2 for why this reads
    nothing under orchestrator/.
    """
    return (root / "state.json").read_text()
''',
    ),
    (
        "routing.toml inside CAO's own agent store is infrastructure (A.2 clarification)",
        """
from cli_agent_orchestrator.constants import routing_toml_path


def read_bindings() -> str:
    return routing_toml_path().read_text()
""",
    ),
    (
        "cao memory / workflow ledgers are unrelated concepts sharing a word (A.2)",
        """
from pathlib import Path


def ledger_rows(root: Path) -> list[str]:
    return (root / "delivery" / "children-ledger.jsonl").read_text().splitlines()
""",
    ),
)


def test_ac_lite2_no_infra_module_reads_a_knowledge_path() -> None:
    """The positive arm: the shipped tree is clean, with zero findings."""
    findings = scan_source_tree(REPO_ROOT)
    assert findings == [], "infrastructure reaches a skill-owned path:\n" + "\n".join(
        f"  {row.location}: {row.evidence}" for row in findings
    )


@pytest.mark.parametrize(
    "name,source", NAMED_MUTANTS, ids=[row[0].split()[0] for row in NAMED_MUTANTS]
)
def test_named_source_mutants_turn_boundary_red(name: str, source: str) -> None:
    """Every read slice 1 removed is still visible to the scan if it comes back."""
    findings = scan_python(source, "mutant.py")
    assert findings, f"reintroduced read went unnoticed: {name}"
    assert all(row.rule == "knowledge-io" for row in findings)


@pytest.mark.parametrize(
    "name,source", ALLOWED_SHAPES, ids=[row[0].split()[0] for row in ALLOWED_SHAPES]
)
def test_generic_safety_journal_diag_and_historical_citations_remain_allowed(
    name: str, source: str
) -> None:
    """The scan must not fire on neutral I/O, prose citations, or shared vocabulary."""
    findings = scan_python(source, "control.py")
    assert findings == [], f"false positive on {name}: {findings}"


def test_allowlist_is_load_bearing_and_names_only_skill_owned_modules() -> None:
    """The allowlist must be small, real, and actually covering something.

    If the skill modules stopped reading knowledge paths, the allowlist would be dead
    weight that silently widens the boundary later; if a path in it did not exist, a
    rename would quietly re-admit a whole module.
    """
    for location in SKILL_ALLOWLIST:
        assert (REPO_ROOT / location).is_file(), f"allowlisted path does not exist: {location}"
        assert location.startswith(
            (
                "src/cli_agent_orchestrator/cli/orchestrator_commands/",
                "src/cli_agent_orchestrator/cli/orchestrator_main.py",
            )
        ), f"allowlist may only cover the cao-orchestrator CLI: {location}"

    unfiltered = scan_source_tree(REPO_ROOT, allowlist=())
    assert unfiltered, "allowlist covers nothing — the skill CLI reads no knowledge path"
    assert {row.location.split(":")[0] for row in unfiltered} <= set(SKILL_ALLOWLIST), (
        "a module outside the skill CLI reads a knowledge path: "
        f"{sorted({row.location for row in unfiltered})}"
    )


def test_finding_carries_its_location_and_evidence() -> None:
    """A finding has to name the scope and the path, or it cannot be acted on."""
    findings = scan_python(NAMED_MUTANTS[0][1], "cli/commands/ledger.py")
    assert findings[0] == Finding(
        "cli/commands/ledger.py:check",
        "knowledge-io",
        "find_workspace_file -> HANDOFF.md",
    )
