"""F900 (#752): herdr must honour ``strip_escapes`` in BOTH directions.

`TmuxClient.get_history` adds ``capture-pane -e`` exactly when ``strip_escapes``
is False, so False means "give me the escapes". `HerdrBackend.get_history` only
ever asked for ``--format text`` (for True) and otherwise left the format unset —
and herdr's default for ``pane read`` is ALREADY escape-stripped, so
``strip_escapes=False`` silently returned plain text on herdr and never on tmux.

Measured on the pinned herdr 0.9.0 binary (grok-box-006, protocol 22), reading a
pane whose prompt carries SGR:

    pane read <id> --source recent --lines 40                 -> 0 escape runs
    pane read <id> --source recent --lines 40 --format text   -> 0 escape runs
    pane read <id> --source recent --lines 40 --format ansi   -> SGR present
                                       (^[[0m^[[1m^[[38;5;2mbox@grok-box-006…)

`draft_guard._read_provider_draft` requests escapes on behalf of a provider that
sets ``composer_parse_accepts_escapes`` (codex, which discriminates dim-SGR ghost
hints from real drafts). Stripped input makes codex's own recorded ghost frame
read as a real human draft — the composer read is wrong in a way tmux never is.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.providers.codex import CodexProvider

# Recorded codex composer frame SHAPE (empirical probe, codex 0.143 — the exact
# escape sequence test/providers/test_codex_provider_unit.py pins for a ghost
# hint): a bold prompt glyph followed by a DIM (SGR 2) body, which is a ghost
# suggestion and NOT a human draft.
# The hint TEXT is deliberately not one of codex's stock suggestions: the parser
# also carries a placeholder-string fallback, which would mask the escapes and
# make this prove nothing.
CODEX_GHOST_FRAME_SGR = [
    "\x1b[1m›\x1b[0m \x1b[2mzzz ghost hint not a placeholder\x1b[0m",
    "  gpt-5.5 high · ~/VScode_projects/cli-subagents",
]
# The same frame as herdr used to hand it over: escapes gone.
CODEX_GHOST_FRAME_PLAIN = [
    "› zzz ghost hint not a placeholder",
    "  gpt-5.5 high · ~/VScode_projects/cli-subagents",
]


def _herdr_with_pane(stdout_for):
    """A HerdrBackend whose `pane read` answers like the real 0.9.0 binary."""
    backend = HerdrBackend.__new__(HerdrBackend)
    backend._resolve_pane_id_from_window = MagicMock(return_value="w1:p1")  # type: ignore[method-assign]
    calls: list[list[str]] = []

    def run(args, check=False):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout=stdout_for(list(args)), stderr="")

    backend._run_herdr = MagicMock(side_effect=run)  # type: ignore[method-assign]
    return backend, calls


class TestFormatFlagIsExplicitBothWays:
    @pytest.mark.parametrize(
        "strip_escapes,expected",
        [(False, "ansi"), (True, "text")],
    )
    def test_format_is_always_requested(self, strip_escapes, expected):
        backend, calls = _herdr_with_pane(lambda args: "out")
        backend.get_history("s", "w", tail_lines=40, strip_escapes=strip_escapes)
        argv = calls[0]
        assert "--format" in argv, argv
        assert argv[argv.index("--format") + 1] == expected, argv

    def test_default_call_asks_for_escapes(self):
        """The signature default is strip_escapes=False, i.e. escapes wanted."""
        backend, calls = _herdr_with_pane(lambda args: "out")
        backend.get_history("s", "w")
        argv = calls[0]
        assert argv[argv.index("--format") + 1] == "ansi", argv

    def test_viewport_capture_still_asks_for_plain_text(self):
        """capture_viewport's contract is escape-stripped; it must not drift."""
        backend, calls = _herdr_with_pane(lambda args: "plain")
        backend.capture_viewport("s", "w")
        argv = calls[0]
        assert argv[argv.index("--format") + 1] == "text", argv


class TestRecordedCodexFrameSurvivesToTheParser:
    """The escapes must actually reach the provider's parser through the port."""

    def _pane_reader(self):
        """Answer exactly as herdr 0.9.0 does: ANSI only for --format ansi."""

        def stdout_for(args):
            fmt = args[args.index("--format") + 1] if "--format" in args else "text"
            frame = CODEX_GHOST_FRAME_SGR if fmt == "ansi" else CODEX_GHOST_FRAME_PLAIN
            return "\n".join(frame)

        return stdout_for

    def test_escapes_reach_a_codex_composer_read(self):
        backend, _ = _herdr_with_pane(self._pane_reader())
        captured = backend.get_history("s", "w", tail_lines=40, strip_escapes=False)
        assert "\x1b[2m" in captured, "dim SGR must survive the backend read"

    def test_plain_read_is_still_available_when_asked_for(self):
        backend, _ = _herdr_with_pane(self._pane_reader())
        captured = backend.get_history("s", "w", tail_lines=40, strip_escapes=True)
        assert "\x1b[" not in captured

    def test_pre_f900_the_escape_request_was_silently_downgraded(self):
        """Guard on the defect, not just the flag.

        Before F900 an escapes-wanted read left --format unset, and herdr's
        default is already plain — so a provider that opted into escape-preserving
        capture received exactly the same bytes as a provider that asked for plain
        text. The two reads must now differ.
        """
        backend, _ = _herdr_with_pane(self._pane_reader())
        with_escapes = backend.get_history("s", "w", tail_lines=40, strip_escapes=False)
        without = backend.get_history("s", "w", tail_lines=40, strip_escapes=True)
        assert with_escapes != without
