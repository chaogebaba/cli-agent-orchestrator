"""F702 (#557) D4/AC2: table-driven tests for the pure fleet STATUS cell.

The table covers every ``status`` value and every ``condition`` value the
server can emit, their cross product, the ``delegating (N)`` and
``wedge_suspect`` overrides, and the unknown-value fallbacks. Each case asserts
``.plain`` and ``.style`` (blueprint B12), so mutating one value's glyph fails
exactly one case.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.condition import ConditionKind
from cli_agent_orchestrator.tui.columns import (
    ALL_COLUMNS,
    ELAPSED_COLUMN,
    NEW_COLUMNS,
    PARITY_COLUMNS,
)
from cli_agent_orchestrator.tui.status_cell import status_cell

FIXTURES = Path(__file__).parent / "fixtures"

# Every TerminalStatus value (models/terminal.py:23-32) -> expected cell.
STATUS_CASES: List[Tuple[str, str, str]] = [
    ("unknown", "· unknown", "dim"),
    ("idle", "◌ idle", "yellow"),
    ("processing", "● working", "green"),
    ("completed", "· completed", "dim"),
    ("waiting_user_answer", "◌ waiting", "yellow"),
    ("render_uncertain", "· render_uncertain", "dim"),
    ("error", "· error", "dim"),
]

# Every condition value the server publishes (providers/condition.py:743-752:
# CAPPED/BLOCKED/AUTH are rewritten labels, the rest are raw ConditionKind
# values) plus the two raw kinds the rewrite consumes, accepted defensively.
CONDITION_CASES: List[Tuple[str, str]] = [
    ("CAPPED", "bold red"),
    ("BLOCKED", "bold red"),
    ("AUTH", "bold red"),
    ("NET_INTERRUPTED", "yellow"),
    ("CONTEXT_EXHAUSTED", "yellow"),
    ("PROC_EXITED", "bold red"),
    ("TRANSIENT_OVERLOAD", "yellow"),
    ("BUSY", "green"),
    ("DIALOG_BLOCKED", "bold red"),
    ("AUTH_EXPIRED", "bold red"),
]


def row(**kw: Any) -> Dict[str, Any]:
    """A minimal fleet row; every key status_cell reads is present."""
    base: Dict[str, Any] = {
        "status": "idle",
        "condition": None,
        "delegating": False,
        "children_count": 0,
        "wedge_suspect": False,
    }
    base.update(kw)
    return base


# ─── vocabulary completeness ──────────────────────────────────────────────────


def test_status_table_covers_every_terminal_status() -> None:
    assert {c[0] for c in STATUS_CASES} == {s.value for s in TerminalStatus}


def test_condition_table_covers_every_condition_kind() -> None:
    """Every ConditionKind reaches the wire either verbatim or as a label."""
    rewritten = {"DIALOG_BLOCKED": "BLOCKED", "AUTH_EXPIRED": "AUTH"}
    covered = {c[0] for c in CONDITION_CASES}
    for kind in ConditionKind:
        assert kind.value in covered
        assert rewritten.get(kind.value, kind.value) in covered


# ─── status × condition ───────────────────────────────────────────────────────


@pytest.mark.parametrize("status,plain,style", STATUS_CASES)
def test_status_without_condition(status: str, plain: str, style: str) -> None:
    cell = status_cell(row(status=status))
    assert cell.plain == plain
    assert cell.style == style


# F752 (#609): the (status, condition) pairs the cell refuses to render — a
# BUSY tag claims live work, which `idle` and `completed` both deny.
SUPPRESSED_PAIRS: List[Tuple[str, str]] = [
    ("idle", "BUSY"),
    ("completed", "BUSY"),
]


@pytest.mark.parametrize("status,plain,style", STATUS_CASES)
@pytest.mark.parametrize("condition,cond_style", CONDITION_CASES)
def test_status_with_condition(
    status: str, plain: str, style: str, condition: str, cond_style: str
) -> None:
    """The condition is appended and owns the cell style.

    The exception is a contradiction: on a quiescent status a BUSY tag is
    dropped entirely and the row renders as the bare status.
    """
    cell = status_cell(row(status=status, condition=condition))
    if (status, condition) in SUPPRESSED_PAIRS:
        assert cell.plain == plain
        assert cell.style == style
        return
    assert cell.plain == f"{plain} [{condition}]"
    assert cell.style == cond_style


# ─── F752 (#609): a stale BUSY never rides a resting row ──────────────────────


@pytest.mark.parametrize("status", ["idle", "completed"])
def test_busy_tag_is_dropped_on_a_quiescent_status(status: str) -> None:
    """The reported row: `◌ idle [BUSY]` renders as `◌ idle`.

    Terminal 1243fb68 was parked for two hours with status=idle while the F611
    condition field still read BUSY. The server no longer writes that; the cell
    refuses to render it either way.
    """
    plain, style = dict((s, (p, st)) for s, p, st in STATUS_CASES)[status]
    cell = status_cell(row(status=status, condition="BUSY"))
    assert cell.plain == plain
    assert "BUSY" not in cell.plain
    assert cell.style == style


@pytest.mark.parametrize("status", ["processing", "waiting_user_answer", "unknown", "error"])
def test_busy_tag_survives_every_non_quiescent_status(status: str) -> None:
    """Only `idle` and `completed` contradict BUSY — nothing else is touched."""
    cell = status_cell(row(status=status, condition="BUSY"))
    assert cell.plain.endswith(" [BUSY]")


@pytest.mark.parametrize("condition,_cond_style", CONDITION_CASES)
def test_only_busy_is_suppressed_on_an_idle_row(condition: str, _cond_style: str) -> None:
    """A cap, an auth expiry or a blocked dialog are all TRUE of a resting seat."""
    cell = status_cell(row(status="idle", condition=condition))
    if condition == "BUSY":
        assert cell.plain == "◌ idle"
    else:
        assert cell.plain == f"◌ idle [{condition}]"


def test_busy_is_dropped_on_a_delegating_row() -> None:
    """`delegating` is IDLE/COMPLETED underneath, so the same contradiction holds."""
    cell = status_cell(row(status="idle", delegating=True, children_count=2, condition="BUSY"))
    assert cell.plain == "◇ delegating (2)"
    assert cell.style == "cyan"


def test_busy_still_shows_on_a_wedge_row_whose_status_is_not_quiescent() -> None:
    """A wedge suspect at `unknown` keeps its tag — the fixture row (term-0032)."""
    cell = status_cell(row(status="unknown", wedge_suspect=True, condition="BUSY"))
    assert cell.plain == "x WEDGE? [BUSY]"
    assert cell.style == "bold red"


# ─── overrides ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status,_plain,_style", STATUS_CASES)
def test_wedge_suspect_outranks_every_status(status: str, _plain: str, _style: str) -> None:
    cell = status_cell(row(status=status, wedge_suspect=True))
    assert cell.plain == "x WEDGE?"
    assert cell.style == "bold red"


@pytest.mark.parametrize("condition,_cond_style", CONDITION_CASES)
def test_wedge_keeps_its_style_under_any_condition(condition: str, _cond_style: str) -> None:
    cell = status_cell(row(status="error", wedge_suspect=True, condition=condition))
    assert cell.plain == f"x WEDGE? [{condition}]"
    assert cell.style == "bold red"


@pytest.mark.parametrize("status", ["idle", "completed"])
@pytest.mark.parametrize("count", [1, 2, 7])
def test_delegating_renders_child_count(status: str, count: int) -> None:
    cell = status_cell(row(status=status, delegating=True, children_count=count))
    assert cell.plain == f"◇ delegating ({count})"
    assert cell.style == "cyan"


def test_delegating_without_count_renders_zero() -> None:
    assert status_cell(row(delegating=True)).plain == "◇ delegating (0)"
    assert status_cell(row(delegating=True, children_count=None)).plain == "◇ delegating (0)"
    assert status_cell(row(delegating=True, children_count="two")).plain == "◇ delegating (0)"


def test_wedge_outranks_delegating() -> None:
    cell = status_cell(row(delegating=True, children_count=3, wedge_suspect=True))
    assert cell.plain == "x WEDGE?"


def test_delegating_with_condition() -> None:
    cell = status_cell(row(delegating=True, children_count=2, condition="CAPPED"))
    assert cell.plain == "◇ delegating (2) [CAPPED]"
    assert cell.style == "bold red"


# ─── unknown / malformed values render visibly, never raise ───────────────────


def test_unknown_status_renders_visibly() -> None:
    cell = status_cell(row(status="teleporting"))
    assert cell.plain == "? teleporting"
    assert cell.style == "magenta"


def test_unknown_condition_renders_visibly() -> None:
    cell = status_cell(row(status="idle", condition="ON_FIRE"))
    assert cell.plain == "◌ idle [? ON_FIRE]"
    assert cell.style == "magenta"


@pytest.mark.parametrize("value", [None, "", 0])
def test_missing_status(value: Any) -> None:
    cell = status_cell(row(status=value))
    assert cell.plain == "· ?"
    assert cell.style == "dim"


def test_empty_row_never_raises() -> None:
    cell = status_cell({})
    assert cell.plain == "· ?"


def test_non_string_status_never_raises() -> None:
    assert status_cell(row(status=17)).plain == "? 17"


# ─── columns (D3 / AC5) ───────────────────────────────────────────────────────


def test_parity_columns_are_the_six_script_headers_in_order() -> None:
    """Five verbatim from the script; the sixth renamed IDLE -> ELAPSED.

    The column no longer shows how long a pane has been quiet under a header
    that asserts the seat is idle — it shows time in the current status, for
    every status — so the header had to stop naming one of them.
    """
    assert PARITY_COLUMNS == ("WIN", "ID", "PROFILE", "TASK", "STATUS", "ELAPSED")
    assert PARITY_COLUMNS[-1] == ELAPSED_COLUMN
    assert PARITY_COLUMNS[:5] == ("WIN", "ID", "PROFILE", "TASK", "STATUS")


def test_new_columns_are_the_five_unrendered_keys() -> None:
    assert NEW_COLUMNS == ("COND", "DELEG", "*", "LIFE", "MODEL")


def test_all_columns_is_parity_then_new() -> None:
    assert ALL_COLUMNS == PARITY_COLUMNS + NEW_COLUMNS
    assert len(set(ALL_COLUMNS)) == len(ALL_COLUMNS)


# ─── fixtures (D5) ────────────────────────────────────────────────────────────

PAYLOAD_FIXTURES = ["healthy", "error_latched", "delegating", "wake_alarm", "empty_session"]
MARKER_FIXTURES = ["fetch_timeout", "never_fetched"]

TERMINAL_KEYS = {
    "id",
    "profile",
    "provider",
    "window_index",
    "window_name",
    "parent_id",
    "depth",
    "orphan",
    "status",
    "condition",
    "fusion_changed",
    "fusion_reason",
    "delegating",
    "children_count",
    "init_state",
    "init_health",
    "since_last_input",
    "lifecycle",
    "resolved_model",
    "reparented_from",
    "config_stale",
    "wedge_suspect",
}


def load(name: str) -> Dict[str, Any]:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_seven_fixtures_exist() -> None:
    assert sorted(p.stem for p in FIXTURES.glob("*.json")) == sorted(
        PAYLOAD_FIXTURES + MARKER_FIXTURES
    )
    assert (FIXTURES / "README.md").exists()


@pytest.mark.parametrize("name", PAYLOAD_FIXTURES)
def test_payload_fixture_has_build_fleet_shape(name: str) -> None:
    data = load(name)
    assert set(data) == {"session_name", "terminals", "wake_exhaustion_alarms"}
    for terminal in data["terminals"]:
        assert set(terminal) == TERMINAL_KEYS


@pytest.mark.parametrize("name", MARKER_FIXTURES)
def test_marker_fixture_carries_no_payload(name: str) -> None:
    data = load(name)
    assert data["payload"] is None
    assert data["__fixture__"] in {"fetch_failure", "never_fetched"}
    assert set(data) >= {"__fixture__", "payload", "error", "error_class", "consecutive_failures"}


@pytest.mark.parametrize("name", PAYLOAD_FIXTURES)
def test_every_fixture_row_renders(name: str) -> None:
    for terminal in load(name)["terminals"]:
        cell = status_cell(terminal)
        assert cell.plain
        assert not cell.plain.startswith("? ")


def test_healthy_fixture_cells() -> None:
    cells = [status_cell(t) for t in load("healthy")["terminals"]]
    assert [c.plain for c in cells] == ["◌ idle", "● working", "· completed"]
    assert [c.style for c in cells] == ["yellow", "green", "dim"]


def test_error_latched_fixture_cells() -> None:
    """#439: status=error while init_state stayed ready."""
    terminals = load("error_latched")["terminals"]
    assert [t["init_state"] for t in terminals[1:]] == ["ready", "ready"]
    cells = [status_cell(t) for t in terminals]
    assert [c.plain for c in cells] == ["◌ idle", "· error", "· error [CAPPED]"]
    assert [c.style for c in cells] == ["yellow", "dim", "bold red"]


def test_delegating_fixture_cells() -> None:
    cells = [status_cell(t) for t in load("delegating")["terminals"]]
    assert [c.plain for c in cells] == ["◇ delegating (2)", "● working", "● working"]
    assert cells[0].style == "cyan"


def test_wake_alarm_fixture() -> None:
    data = load("wake_alarm")
    assert len(data["wake_exhaustion_alarms"]) == 1
    assert set(data["wake_exhaustion_alarms"][0]) == {
        "mailbox_id",
        "session_name",
        "role",
        "stuck_row_id",
        "wake_streak",
    }
    cells = [status_cell(t) for t in data["terminals"]]
    assert [c.plain for c in cells] == ["◌ idle", "x WEDGE? [BUSY]"]
    assert [c.style for c in cells] == ["yellow", "bold red"]


def test_empty_session_fixture() -> None:
    data = load("empty_session")
    assert data["terminals"] == []
    assert data["wake_exhaustion_alarms"] == []
