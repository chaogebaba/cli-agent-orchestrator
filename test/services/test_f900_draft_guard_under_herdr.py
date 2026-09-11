"""F900 (#752): the escapes must survive all the way into the DRAFT GUARD.

`test/backends/test_f900_get_history_escapes.py` pins the backend half — that
``HerdrBackend.get_history`` asks herdr for ``--format ansi`` when escapes are
wanted.  It stops at the backend, and the defect did not: what actually died was
``draft_guard._read_provider_draft``, which could not recognise the codex
composer and raised ``Composer state is unreadable`` for every codex worker on
its first delivery under the herdr backend.

So this module runs the REAL ``draft_guard`` read against the REAL
``CodexProvider`` parser over a ``HerdrBackend`` whose ``herdr`` CLI answers the
way the pinned 0.9.0 binary does.  Re-measured on herdr 0.9.0 (protocol 22)
against a live pane carrying SGR, at the argv level:

    pane read <id> --source recent --lines 40                  ->  0 escapes
    pane read <id> --source recent --lines 40 --format text    ->  0 escapes
    pane read <id> --source recent --lines 40 --format ansi    -> 41 escapes

(``--ansi`` and ``--raw`` are accepted spellings of the third; ``--format`` is
the one this backend uses, and it is the one the usage line documents.)

The frame is codex's dim-SGR ghost hint — the exact shape
``test/providers/test_codex_provider_unit.py`` pins — because that is the read
whose ANSWER CHANGES with the escapes: dim body means "ghost suggestion, not a
human draft".  Strip the escapes and the same bytes read as a real draft the
guard would stash and restore.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.services import draft_guard

# Empirical probe (codex 0.143): a bold prompt glyph, then a DIM (SGR 2) body.
# `_composer_body_is_dim_ghost` reads the composer body raw and calls it a ghost
# only when EVERY non-space character is dim.
#
# Two properties of the frame are load-bearing, and both were measured against
# the parser rather than assumed:
#
# 1. The hint text is NOT one of `CODEX_EMPTY_COMPOSER_PLACEHOLDERS`.  The parser
#    has a placeholder-string fallback that returns "" for a stock suggestion
#    whether or not escapes survive, so a stock hint makes the stripped and the
#    escape-preserving read agree and the frame proves nothing.
# 2. The composer row is separated from the footer row by a BLANK line.  The body
#    the dim test runs over spans from the prompt glyph to `search_end`, so any
#    intervening undimmed chrome (a "? for shortcuts" row, or the footer itself
#    when it is not structurally recognised) makes `saw_undimmed` true and the
#    ghost test false.  The blank row both anchors the footer
#    (`_find_composer_anchor_index` needs `saw_blank` with the composer at a
#    boundary) and trims `search_end` back to the prompt row.
#
# Get either wrong and the test still passes — for a reason unrelated to escapes.
_HINT = "zzz ghost hint not a placeholder"
_FOOTER = "  gpt-5.5 high · ~/VScode_projects/cli-subagents"
#: How herdr hands the pane over with ``--format ansi`` (escapes intact).
GHOST_FRAME_SGR = ["", f"\x1b[1m›\x1b[0m \x1b[2m{_HINT}\x1b[0m", "", _FOOTER]
#: The same pane as herdr used to hand it over before F900: escapes gone.
GHOST_FRAME_PLAIN = ["", f"› {_HINT}", "", _FOOTER]

METADATA = {"tmux_session": "cao", "tmux_window": "w2-codex"}


def _herdr_backend(record_argv: list[list[str]] | None = None) -> HerdrBackend:
    """A real HerdrBackend whose ``herdr`` CLI behaves like the 0.9.0 binary."""
    backend = HerdrBackend.__new__(HerdrBackend)
    backend._resolve_pane_id_from_window = MagicMock(return_value="w1:p2")  # type: ignore[method-assign]

    def run(args, check=False):
        if record_argv is not None:
            record_argv.append(list(args))
        fmt = args[args.index("--format") + 1] if "--format" in args else "text"
        frame = GHOST_FRAME_SGR if fmt == "ansi" else GHOST_FRAME_PLAIN
        return MagicMock(returncode=0, stdout="\n".join(frame), stderr="")

    backend._run_herdr = MagicMock(side_effect=run)  # type: ignore[method-assign]
    return backend


@pytest.fixture
def codex():
    return CodexProvider.__new__(CodexProvider)


class TestDraftGuardSeesTheEscapesItKeysOn:
    def test_the_guard_read_reaches_the_parser_with_sgr_intact(self, codex):
        """The bytes the parser is handed still carry the dim run."""
        seen: list[list[str]] = []
        spy = MagicMock(wraps=codex)
        spy.composer_parse_accepts_escapes = True

        def capture(lines):
            seen.append(list(lines))
            return CodexProvider.read_composer_draft(codex, lines)

        spy.read_composer_draft = MagicMock(side_effect=capture)
        backend = _herdr_backend()

        with patch.object(draft_guard, "get_backend", return_value=backend):
            draft_guard._read_provider_draft("t1", METADATA, spy)

        assert seen, "the guard never reached the provider parser"
        assert any("\x1b[2m" in line for line in seen[0]), (
            "draft_guard's read handed the codex parser escape-stripped lines — "
            "this is F900 #752 as the worker experienced it"
        )

    def test_the_ghost_is_classified_as_a_ghost_not_a_human_draft(self, codex):
        """The read is not merely non-None; it is the RIGHT answer."""
        spy = MagicMock(wraps=codex)
        spy.composer_parse_accepts_escapes = True
        spy.read_composer_draft = MagicMock(
            side_effect=lambda lines: CodexProvider.read_composer_draft(codex, lines)
        )
        backend = _herdr_backend()

        with patch.object(draft_guard, "get_backend", return_value=backend):
            draft = draft_guard._read_provider_draft("t1", METADATA, spy)

        assert draft == "", f"dim-SGR ghost must read as an empty composer, got {draft!r}"

    def test_the_guard_asks_herdr_for_ansi_first(self, codex):
        """The escape-preserving capture is the FIRST read, not a retry."""
        argv: list[list[str]] = []
        spy = MagicMock(wraps=codex)
        spy.composer_parse_accepts_escapes = True
        spy.read_composer_draft = MagicMock(
            side_effect=lambda lines: CodexProvider.read_composer_draft(codex, lines)
        )
        backend = _herdr_backend(argv)

        with patch.object(draft_guard, "get_backend", return_value=backend):
            draft_guard._read_provider_draft("t1", METADATA, spy)

        assert argv, "no herdr call was made"
        assert argv[0][argv[0].index("--format") + 1] == "ansi", argv[0]


class TestTheDefectItself:
    """Guard on the failure, not only on the fix."""

    def test_a_stripped_read_misclassifies_the_same_pane(self, codex):
        """What herdr used to return turns a ghost hint into a 'human draft'.

        This is the second half of the F900 damage and the reason the fix is not
        cosmetic: the pre-fix read did not merely lose colour, it changed the
        guard's verdict about whether a human had typed something.
        """
        plain = CodexProvider.read_composer_draft(codex, GHOST_FRAME_PLAIN)
        with_sgr = CodexProvider.read_composer_draft(codex, GHOST_FRAME_SGR)
        assert with_sgr == "", "with escapes the dim body is a ghost"
        assert plain == _HINT, (
            "without escapes the SAME pane reads as a real human draft — the "
            "guard would stash it, clear the composer and restore it afterwards"
        )

    def test_a_plain_only_provider_is_still_given_plain_text(self, codex):
        """Non-opt-in providers must not start receiving ANSI."""
        argv: list[list[str]] = []
        spy = MagicMock(wraps=codex)
        spy.composer_parse_accepts_escapes = False
        spy.read_composer_draft = MagicMock(return_value="")
        backend = _herdr_backend(argv)

        with (
            patch.object(draft_guard, "get_backend", return_value=backend),
            patch.object(draft_guard, "_read_screen_lines", return_value=None),
        ):
            draft_guard._read_provider_draft("t1", METADATA, spy)

        assert argv, "no herdr call was made"
        assert all(a[a.index("--format") + 1] == "text" for a in argv), argv
