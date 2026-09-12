"""F581 (#438) — grok_cli rule3a_busy_marker, and why cline has no leg.

Same D12d contract as claude_code / kiro / codex. The grok corpus under
`test/providers/fixtures/status_truth/grok_cli/` is eight byte-exact pane
captures; none of them was written by hand (each sidecar names the capture it
was copied from, and the whole corpus predates this branch).

The verdict that MATTERS is `False`: it is the only one that changes rule 3a's
outcome (`pane_liveness._rule3a_would_downgrade` and `status_monitor.fuse_status`
then admit the published status tagged `pane_delta_vetoed` instead of holding the
seat at PROCESSING on pane churn alone). `True` and `None` are the status-quo
path. So grok's whole fixture table is asserted, verdict by verdict: a wrong
`False` on a working pane would publish idle over live work.

The cline leg is deliberately ABSENT — see `TestClineHasNoLegAndWhy`, which pins
the live evidence that killed it.
"""

from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.cline_cli import ClineCliProvider
from cli_agent_orchestrator.providers.grok_cli import (
    GrokCliProvider,
    grok_busy_marker_live,
)

_CORPUS = Path(__file__).parent / "fixtures" / "status_truth"
_GROK = _CORPUS / "grok_cli"
_CLINE = _CORPUS / "cline_cli"


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8", "replace")


# ---- grok_cli --------------------------------------------------------------

# The whole grok corpus, verdict by verdict.
#   working-1   live spinner row "⠦ Waiting for response… 0.7s … [stop]"  -> True
#   idle-1/2/3  composer box drawn, no spinner (idle-2 is raw alt-screen)  -> False
#   wua-2       telemetry opt-in banner OVER the composer box              -> False
#   wua-1       permission dialog replaced the composer                    -> None
#   wua-3       device-login approval wait, no TUI                         -> None
#   error-1     argument-parse error, no TUI at all                        -> None
_GROK_TABLE = [
    ("working-1.txt", True),
    ("idle-1.txt", False),
    ("idle-2.txt", False),
    ("idle-3.txt", False),
    ("waiting_user_answer-1.txt", None),
    ("waiting_user_answer-2.txt", False),
    ("waiting_user_answer-3.txt", None),
    ("error-1.txt", None),
]


@pytest.mark.parametrize("name,expected", _GROK_TABLE, ids=[n for n, _ in _GROK_TABLE])
def test_grok_marker_matches_the_live_corpus(name, expected):
    assert grok_busy_marker_live(_read(_GROK / name)) is expected


def test_grok_provider_delegates_to_helper():
    for path in sorted(_GROK.glob("*.txt")):
        text = _read(path)
        assert GrokCliProvider.rule3a_busy_marker(None, text) is grok_busy_marker_live(text)


def test_grok_spinner_above_the_completion_row_is_not_live_work():
    """Position anchor: prior-turn spinner scrollback never asserts busy.

    Mirrors get_status's last_processing/last_completed ordering — without it a
    post-turn pane whose scrollback still holds a spinner row reads as busy.
    """
    pane = (
        "  ⠦ Waiting for response… 0.7s\n"
        "  Worked for 4.9s\n"
        "  ╭────────────────╮\n"
        "  │ ❯              │\n"
        "  ╰── Grok 4.5 (high) · always-approve ─╯\n"
    )
    assert grok_busy_marker_live(pane) is False
    # Same pane with the spinner BELOW the completion row is live work again.
    assert grok_busy_marker_live(pane + "  ⠦ Waiting for response… 0.2s\n") is True


def test_grok_needs_both_halves_of_the_composer_box():
    """A quoted box-drawing rule alone never fakes an idle verdict."""
    assert grok_busy_marker_live("  ╰──────────── some quoted table ─╯\n") is None
    assert grok_busy_marker_live("plain scrollback, no TUI\n") is None


# ---- cline_cli: no leg, and the live pane that says why --------------------


class TestClineHasNoLegAndWhy:
    """cline keeps the BaseProvider default; this pins the evidence, not a taste.

    A cline leg was written for this issue and REMOVED after a live capture
    disproved its only outcome-changing rule. The rule was "the ClinePass
    composer chrome is the newest content ⇒ the seat's turn is over ⇒ False".
    On cline 3.0.61 the composer is pinned to the bottom of the pane in EVERY
    state, so that rule returns `False` on a pane that is provably working —
    the one verdict that publishes idle over live work.

    The two fixtures below are byte-exact captures of the same 3.0.61 TUI forty
    seconds apart (sidecars carry the provenance). They differ in the spinner
    and in nothing else that a chrome rule can see.
    """

    def test_cline_provider_does_not_override_the_marker(self):
        assert ClineCliProvider.rule3a_busy_marker is BaseProvider.rule3a_busy_marker
        assert BaseProvider.rule3a_busy_marker(None, _read(_CLINE / "working-3.txt")) is None

    def test_the_working_pane_really_is_working(self):
        """working-3 holds a live braille-spinner tool row: the turn is in flight."""
        text = _read(_CLINE / "working-3.txt")
        assert any(
            row.lstrip().startswith(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")) and "run_commands(" in row
            for row in text.splitlines()
        ), "no live spinner tool row in working-3.txt"

    def test_both_panes_end_in_the_same_chrome(self):
        """Working and idle are indistinguishable to a 'chrome is newest' rule."""
        chrome = ("⏵⏵", "○ Plan ● Act")

        def last_chrome_row(text: str) -> int:
            rows = text.splitlines()
            return max((i for i, r in enumerate(rows) if any(c in r for c in chrome)), default=-1)

        def last_content_row(text: str) -> int:
            rows = text.splitlines()
            return max((i for i, r in enumerate(rows) if r.strip()), default=-1)

        for name in ("working-3.txt", "idle-2.txt"):
            text = _read(_CLINE / name)
            assert last_chrome_row(text) == last_content_row(text), name


# ---- the base default is untouched -----------------------------------------


def test_base_provider_default_still_none():
    """Providers without a corpus keep the BaseProvider no-signal default."""
    assert BaseProvider.rule3a_busy_marker(None, _read(_GROK / "working-1.txt")) is None
