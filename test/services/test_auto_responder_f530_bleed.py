"""F530 (#386) — the codex resume-cwd card that stalled a seat for 37 seconds.

Terminal 162c159f, 2026-09-05. A codex worker came up on the codex-cli 0.153.3
"Choose working directory to resume this session" card and sat there until the
user cleared it by hand. `codex-resume-workdir-card` was present, enabled, and
its answer keys were right. It never fired.

The reading the issue had accumulated — "the dialog arrives before the responder
is armed" — is wrong for this occurrence, and the decisions log
(`~/.aws/cli-agent-orchestrator/logs/auto-answers/162c159f.decisions.log`) says
so directly: the responder evaluated that screen FOUR times between
00:32:35.403Z and 00:32:35.937Z, while the card was up, and every one of them
recorded `codex-resume-workdir-card:question(regex)`. Arming was fine. The
anchor missed.

It missed because the card's text was not the card's text. Codex repaints a
dialog by writing only the non-blank glyph runs of its own text and stepping the
cursor across the blanks, onto a screen it never cleared — so every cell the
card leaves BLANK still holds the previous frame's character. The region dumped
at 00:32:35.723Z (`region_hash=37dbf83c7e927ab7`) opens:

    choose working directory todresumeothisnsessiont by running omz update

'd', 'o' and 'n' are characters of the shell's leftover
"[oh-my-zsh] It's time to update! You can do that by running `omz update`" line
showing through the card's word gaps; "t by running omz update" is the tail of
that same line past the end of the card's text. Three more of the card's lines
were corrupted the same way in the same frame. The rule's anchor spelled its
word separators `\\W+` — a NON-word character — and met a letter.

The fix is `bleed_tolerant_pattern`: word interiors are never corrupted (the
dialog does write every glyph of its own words), so pin the words and let each
gap absorb a short run of anything. `contains` rules get it as a fallback after
the plain substring test, which makes it a widening and never a narrowing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import auto_responder as ar

# --------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------

# VERBATIM from the live decisions log: terminal 162c159f,
# 2026-09-05T00:32:35.723939+00:00, outcome=no_match reason=region_dump
# region_hash=37dbf83c7e927ab7. This is the canonical string the matcher
# actually saw, not a reconstruction of it.
STALL_REGION_CANONICAL = (
    "choose working directory todresumeothisnsessiont by running omz update 16session latest cwd "
    "recorded in the resumed session current your current working directory 1 use session "
    "directory home chao vscode projects cli subagents 2 huseecurrent directory data cao scratch "
    "worktrees cli subagents 162c159f ec3 ralways use session directory 4 always use current "
    "directory 162c159f on cao 162c159f via v3 14 7 on press enter to continuesly bypass "
    "approvals and sandbox no alt screen disable shell snapshot dangerously bypass hook trust "
    "model gpt 5 6 sol"
)

# The same card as codex would render it onto a cleared screen.
CLEAN_CARD: List[str] = [
    "Choose working directory to resume this session",
    "  Session = latest cwd recorded in the resumed session",
    "  Current = your current working directory",
    "",
    "› 1. Use session directory (/home/chao/VScode_projects/cli-subagents)",
    "  2. Use current directory (/data/cao-scratch/worktrees/cli-subagents/162c159f)",
    "  3. Always use session directory",
    "  4. Always use current directory",
    "",
    "  Press enter to continue",
]

# What was on the pane before codex painted over it: the shell's oh-my-zsh
# nag and the echo of the launch command CAO submitted.
LEFTOVER_SCREEN: List[str] = [
    "[oh-my-zsh] It's time to update! You can do that by running `omz update`",
    "162c159f on  cao/162c159f via 🐍 v3.14.7",
    "❯ codex resume --dangerously-bypass-approvals-and-sandbox --no-alt-screen",
    "  --disable shell_snapshot --dangerously-bypass-hook-trust --model gpt-5.6-sol",
    "  -c 'mcp_servers.cao-mcp-server.command=\"/home/chao/.local/share/uv/tools/cao\"'",
    "  -c 'mcp_servers.cao-mcp-server.env.CAO_TERMINAL_ID=\"162c159f\"'",
    '  -c model_reasoning_effort="high" -c features.multi_agent=false',
    "  -c check_for_update_on_startup=false 01a06efa-e301-7bc1-b8d7-9c664894f276",
    '  -c developer_instructions="$(cat ~/.aws/cli-agent-orchestrator/tmp/162c159f)"',
    "  --search",
]

# The rule as it shipped when the stall happened, and as it reads now.
SHIPPED_AT_STALL = dict(
    name="codex-resume-workdir-card",
    enabled=True,
    match_mode="regex",
    question=r"Choose\W+working\W+directory\W+to\W+resume\W+this\W+session",
    options=["continue"],
    answer=["Down", "Enter"],
)
FIXED = dict(
    name="codex-resume-workdir-card",
    enabled=True,
    match_mode="contains",
    question="Choose working directory to resume this session",
    options=["Use current directory"],
    answer=["Down", "Enter"],
)


def superpose(card: List[str], background: List[str]) -> List[str]:
    """Repaint ``card`` over ``background`` the way codex repaints a dialog.

    Only the card's non-blank cells are written. Every cell the card leaves
    blank keeps whatever the previous frame had there. This is the whole
    mechanism; the rest of the file is what it does to a matcher.
    """
    rows: List[str] = []
    for index in range(max(len(card), len(background))):
        over = card[index] if index < len(card) else ""
        under = background[index] if index < len(background) else ""
        width = max(len(over), len(under))
        over = over.ljust(width)
        under = under.ljust(width)
        rows.append("".join(o if o != " " else u for o, u in zip(over, under)))
    return rows


def region_of(canonical: str) -> ar.DialogRegion:
    """A region carrying ``canonical`` in both match domains."""
    return ar.DialogRegion(rows=(canonical,), normalized=canonical, normalized_light=canonical)


class TestTheStall:
    """The 00:32:35Z frame, and what each version of the rule does with it."""

    def test_the_shipped_regex_rule_misses_the_frame_it_was_written_for(self) -> None:
        """The reject the decisions log recorded four times, reproduced."""
        rule = ar.Rule(**SHIPPED_AT_STALL)  # type: ignore[arg-type]
        assert rule.matches(region_of(STALL_REGION_CANONICAL)) is False
        assert rule.reject_reason(region_of(STALL_REGION_CANONICAL)) == "question(regex)"

    def test_the_fixed_rule_fires_on_that_same_frame(self) -> None:
        rule = ar.Rule(**FIXED)  # type: ignore[arg-type]
        assert rule.reject_reason(region_of(STALL_REGION_CANONICAL)) is None
        assert rule.matches(region_of(STALL_REGION_CANONICAL)) is True
        assert rule.answer == ["Down", "Enter"]

    def test_the_bled_characters_are_where_the_anchor_broke(self) -> None:
        """Not a coincidence of one screen: name the letters that did it."""
        assert "todresumeothisnsessiont" in STALL_REGION_CANONICAL
        assert "choose working directory to resume this session" not in STALL_REGION_CANONICAL
        # 'to' and 'resume' are intact; only the gap between them is not.
        pattern = ar.bleed_tolerant_pattern("to resume this session")
        assert pattern is not None
        assert pattern.search(STALL_REGION_CANONICAL) is not None


class TestTheMechanism:
    """Superposing the real card over the real leftover screen reproduces it."""

    def test_superposition_corrupts_the_word_gaps_and_only_the_word_gaps(self) -> None:
        screen = superpose(CLEAN_CARD, LEFTOVER_SCREEN)
        canonical = ar.normalize_screen(screen)
        # The exact corruption the live capture recorded.
        assert "todresumeothisnsessiont" in canonical
        # Whole words survive; the plain anchor does not.
        assert "choose working directory" in canonical
        assert "resume" in canonical
        assert "choose working directory to resume this session" not in canonical

    def test_the_fixed_rule_fires_on_the_superposed_screen(self) -> None:
        region = ar.dialog_region(superpose(CLEAN_CARD, LEFTOVER_SCREEN))
        rule = ar.Rule(**FIXED)  # type: ignore[arg-type]
        assert rule.reject_reason(region) is None

    def test_the_shipped_regex_rule_does_not(self) -> None:
        region = ar.dialog_region(superpose(CLEAN_CARD, LEFTOVER_SCREEN))
        rule = ar.Rule(**SHIPPED_AT_STALL)  # type: ignore[arg-type]
        assert rule.reject_reason(region) == "question(regex)"

    def test_an_uncorrupted_card_still_matches_the_ordinary_way(self) -> None:
        """The fallback must not be the only thing holding the rule up."""
        region = ar.dialog_region(CLEAN_CARD)
        rule = ar.Rule(**FIXED)  # type: ignore[arg-type]
        assert rule._canon_question in region.normalized  # exact path, no fallback
        assert rule.matches(region) is True


class TestOptionsGetTheSameTreatment:
    """An option anchor is prose on the same corrupted screen as the question."""

    def test_a_bled_option_still_matches(self) -> None:
        # "2. Use current directory" composited as "2 huseecurrent directory".
        assert "2 huseecurrent directory" in STALL_REGION_CANONICAL
        rule = ar.Rule(**FIXED)  # type: ignore[arg-type]
        assert rule.matches(region_of(STALL_REGION_CANONICAL)) is True

    def test_a_regex_rules_options_are_tolerant_even_though_its_question_is_not(self) -> None:
        """Options are plain prose in both match modes, so both get the fallback."""
        rule = ar.Rule(
            name="probe",
            enabled=True,
            match_mode="regex",
            question=r"choose working directory",
            options=["Use current directory"],
            answer=["Enter"],
        )
        assert rule.matches(region_of(STALL_REGION_CANONICAL)) is True


class TestItWidensAndDoesNotLeak:
    """A tolerant anchor is still an anchor."""

    def test_a_sibling_rule_does_not_claim_the_resume_card(self) -> None:
        """codex-fork-workdir-choice differs from the resume rule by one word."""
        fork = ar.Rule(
            name="codex-fork-workdir-choice",
            enabled=True,
            match_mode="contains",
            question="Choose working directory to fork this session",
            options=["Use session directory", "Use current directory"],
            answer=["Down", "Enter"],
        )
        assert fork.matches(region_of(STALL_REGION_CANONICAL)) is False
        assert fork.matches(ar.dialog_region(CLEAN_CARD)) is False

    def test_a_gap_wider_than_the_cap_is_not_a_match(self) -> None:
        pattern = ar.bleed_tolerant_pattern("press enter to continue")
        assert pattern is not None
        assert pattern.search("press enter to continue") is not None
        assert pattern.search("press enter toXXXcontinue") is not None  # 3, at the cap
        assert pattern.search("press enter toXXXXcontinue") is None  # 4, over it

    def test_absent_words_are_still_absent(self) -> None:
        rule = ar.Rule(**FIXED)  # type: ignore[arg-type]
        assert rule.matches(region_of("resuming session working on the directory")) is False

    def test_a_single_word_anchor_gets_no_pattern(self) -> None:
        """No gaps to tolerate, and a word interior is never corrupted."""
        assert ar.bleed_tolerant_pattern("continue") is None
        assert ar.bleed_tolerant_pattern("") is None

    def test_an_empty_anchor_stays_trivially_present(self) -> None:
        """Behaviour preservation: `"" in haystack` was always True."""
        assert ar.Rule._present("", None, "anything at all") is True

    def test_the_word_order_still_has_to_be_right(self) -> None:
        pattern = ar.bleed_tolerant_pattern("use current directory")
        assert pattern is not None
        assert pattern.search("directory current use") is None

    def test_a_gap_is_at_least_one_character_wide(self) -> None:
        """A blank cell holds exactly one character; it never vanishes.

        Allowing a zero-width gap would let "usecurrent" satisfy "use current",
        which is not a bleed — it is a different word.
        """
        pattern = ar.bleed_tolerant_pattern("use current directory")
        assert pattern is not None
        assert pattern.search("usecurrentdirectory") is None
        assert pattern.search("use current directory") is not None

    def test_every_option_still_has_to_be_present(self) -> None:
        """Tolerance applies per option; it does not turn `all` into `any`."""
        rule = ar.Rule(
            name="probe",
            enabled=True,
            match_mode="contains",
            question="Choose working directory to resume this session",
            options=["Use current directory", "Restore the previous branch"],
            answer=["Enter"],
        )
        assert rule.reject_reason(region_of(STALL_REGION_CANONICAL)) == (
            "option[Restore the previous branch]"
        )
        assert rule.matches(region_of(STALL_REGION_CANONICAL)) is False


class TestTheGrokTrustCard:
    """The grok trust-directory card, replayed from two live captures.

    Filed alongside this work as the same fault class. It is not: the decisions
    logs show `grok-trust-directory` matching, settling and FIRING on both
    terminals, about one and a half seconds after the card appeared, and both
    workers went on to finish their assignments.

      cd6f8655   card at 00:21:17.913Z, settled :18.420, fired :18.501, composer up
                 by :19; the worker then ran for 13m52s and sent its callback.
      9208a488   card at 00:47:05.891Z, settled :06.396, fired :06.503, composer up
                 at :07.418; the worker ran 6m14s and committed a67029ce.

    The card's rows below are verbatim from
    ``~/.aws/cli-agent-orchestrator/logs/terminal/9208a488.scrollback`` lines
    52-61. It carries BOTH affordances — the "y  Yes, proceed" option rows the
    rule anchors on, and the "Enter or y to trust" footer. Pinned so the pairing
    is not lost, and so the card is covered by the bleed matcher too: grok
    repaints over an uncleared screen exactly as codex does, and the launch
    command's echoed system prompt is sitting right above this card on the pane.
    """

    CARD: List[str] = [
        "Do you trust the contents of this directory?",
        "/home/chao/VScode_projects/cli-subagents",
        "",
        "Grok Build may run or modify contents in this directory,",
        "posing security risks.",
        "",
        "y  Yes, proceed",
        "n  No, quit",
        "",
        "Enter or y to trust · n or Esc to quit",
    ]

    def _rule(self) -> ar.Rule:
        return ar.Rule(
            name="grok-trust-directory",
            enabled=True,
            match_mode="contains",
            question="Do you trust the contents of this directory?",
            options=["Yes, proceed", "No, quit"],
            answer=["y"],
            modality="hard",
        )

    def test_the_captured_card_matches_the_shipped_rule(self) -> None:
        """What the live fire log already proves, pinned as a test."""
        region = ar.dialog_region(self.CARD)
        assert self._rule().reject_reason(region) is None

    def test_the_card_carries_both_affordances(self) -> None:
        canonical = ar.normalize_screen(self.CARD)
        assert "yes proceed" in canonical  # the option rows the rule anchors on
        assert "enter or y to trust" in canonical  # the footer hint

    def test_it_still_matches_when_the_previous_frame_bleeds_through(self) -> None:
        """Provider-independence, at the level where it is decided.

        The matcher is shared, so the bleed fallback covers grok the moment it
        covers codex. Superposed here on the tail of the launch command's system
        prompt, which is what sits above this card on a real grok pane.
        """
        background = [
            "change approach scope or semantics is not yours to decide available",
            "skills the following skills are available exclusively in this cao",
            "orchestration context to load a skill first discover it, then read it",
            "from disk because these are not reachable through provider-native",
            "skill commands or directories at all in this configuration here",
            "- box-ops: offload-box operations for CAO workers, slot-locked runs",
            "- cao-worker-protocols: worker-side callback and completion rules",
            "- doc-keeper: descriptive-doc maintenance and staleness sweeps",
            "--session-id c35e3120-8014-4f9c-9c5e-3f96e8651f8f --print-mode never",
            "--allowed-tools bash,read,write,edit --max-turns 400 --verbose",
        ]
        bled = superpose(self.CARD, background)
        canonical = ar.normalize_screen(bled)
        assert "do you trust the contents of this directory" not in canonical
        assert self._rule().reject_reason(ar.dialog_region(bled)) is None

    def test_the_revival_rule_is_a_duplicate_of_the_one_that_fires(self) -> None:
        """Both grok trust rules are byte-identical in body.

        First match wins, so `grok-trust-directory-revival` can never fire. It
        costs an evaluation per rule per tick and nothing else. Recorded rather
        than deleted: it is the supervisor's config to retire.
        """
        revival = ar.Rule(
            name="grok-trust-directory-revival",
            enabled=True,
            match_mode="contains",
            question="Do you trust the contents of this directory?",
            options=["Yes, proceed", "No, quit"],
            answer=["y"],
            modality="hard",
        )
        assert revival.body_hash == self._rule().body_hash


class TestTheShippedSeed:
    """A fresh install must not ship the anchor that failed."""

    def test_the_seed_resume_rule_fires_on_the_stall_frame(self, tmp_path: Path) -> None:
        path = tmp_path / "codex.yaml"
        path.write_text(ar.SEED_RULES["codex.yaml"], encoding="utf-8")
        rules = ar._RuleStore._load(path)
        rule = next(r for r in rules if r.name == "codex-resume-working-directory")
        assert rule.reject_reason(region_of(STALL_REGION_CANONICAL)) is None
        assert rule.answer == ["Down", "Enter"]

    def test_no_seeded_anchor_spells_its_separators_as_a_non_word_class(self) -> None:
        """`\\W+` is what broke; it must not come back in through the seed.

        Only the anchors are checked — the seed's header comment names `\\W+`
        precisely to tell rule authors not to write it.
        """
        for filename, body in ar.SEED_RULES.items():
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith(("question:", "options:")):
                    assert "\\W" not in stripped, f"{filename}: {stripped}"


class TestTheUnknownMenuDetector:
    """The escape hatch that surfaces a menu no rule claims.

    It was NOT part of this stall — it recognised the corrupted card correctly.
    Pinned here because the margin it did so on is thin, and because the obvious
    way to widen it is wrong.
    """

    def test_the_detector_recognises_the_corrupted_card(self) -> None:
        assert ar.AutoResponder._looks_like_dialog(STALL_REGION_CANONICAL, "codex") is True

    def test_it_cannot_see_a_fourth_option(self) -> None:
        """The known limit, stated rather than left to be rediscovered."""
        assert ar._NUMBERED_OPTION_PATTERN.search("4 always use current directory") is None
        assert ar._NUMBERED_OPTION_PATTERN.search("2 use current directory") is not None

    def test_the_proximity_budget_is_nearly_spent_on_this_card(self) -> None:
        """177 of 200 characters. It held; it did not hold comfortably."""
        options = list(ar._NUMBERED_OPTION_PATTERN.finditer(STALL_REGION_CANONICAL))
        footer = ar._PRESS_ENTER_PATTERN.search(STALL_REGION_CANONICAL)
        assert footer is not None
        before = [o for o in options if o.start() < footer.start()]
        nearest = max(before, key=lambda o: o.start())
        gap = footer.start() - nearest.end()
        assert 150 < gap < ar.DIALOG_PROXIMITY_CHARS

    def test_widening_the_digit_class_would_match_a_version_number(self) -> None:
        """Why [1-9] was tried and reverted.

        The prompt on this very screen renders "via v3.14.7 on", which
        canonicalizes to "via v3 14 7 on". Under [1-9] the "7 o" in it becomes
        the nearest "option" to the footer — the budget is bought by treating
        every digit in ordinary prose as a menu entry.
        """
        widened = re.compile(r"\b[1-9]\s+\S")
        assert widened.search("via v3 14 7 on press enter") is not None
        assert ar._NUMBERED_OPTION_PATTERN.search("via v3 14 7 on press enter") is None

    def test_prose_without_a_footer_is_not_a_dialog(self) -> None:
        assert (
            ar.AutoResponder._looks_like_dialog(
                "step 1 clone the repo then run the suite and read the output", "codex"
            )
            is False
        )


class TestTheUnknownPathIsNoLongerSilent:
    """A held seat that nobody was told about left no trace in the log."""

    def _responder(self, monkeypatch: pytest.MonkeyPatch) -> tuple[ar.AutoResponder, List[tuple]]:
        recorded: List[tuple] = []
        monkeypatch.setattr(
            ar.AutoResponder,
            "_log_decision",
            staticmethod(lambda *a, **k: recorded.append((a, k))),
        )
        return ar.AutoResponder(), recorded

    def test_the_permission_exemption_names_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        responder, recorded = self._responder(monkeypatch)
        metadata: Dict[str, Any] = {"tmux_session": "s", "tmux_window": "w"}
        region = region_of("select an option use arrow keys to navigate")
        result = responder._check_unknown(
            "t1", metadata, "codex", object(), [], region, None, (0, 0, "s", "w")
        )
        assert result == TerminalStatus.WAITING_USER_ANSWER
        reasons = [args[2] for args, _ in recorded]
        assert "permission_prompt_exempt" in reasons
