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
gap absorb exactly one cell — one space, or one stale alphanumeric, never a
longer run, because a single blank cell cannot hold two characters. `contains`
rules get it as a fallback after the plain substring test, on anchors of three
words or more, which makes it a widening and never a narrowing.

An anchor match is not a keystroke. `TestTheFalsePositiveBound` states the
property that actually matters at the rule: question, every option, and a
two-capture settle gate, checked against the three adversarial screens the
EMPIRICAL gate built for r1 and three more taken from real pane scrollback.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

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


def shipped_at_stall() -> ar.Rule:
    """`codex-resume-workdir-card` exactly as it read when the seat stalled."""
    return ar.Rule(
        name="codex-resume-workdir-card",
        enabled=True,
        match_mode="regex",
        question=r"Choose\W+working\W+directory\W+to\W+resume\W+this\W+session",
        options=["continue"],
        answer=["Down", "Enter"],
    )


def fixed() -> ar.Rule:
    """The same rule as it reads now: plain prose, option-text anchor."""
    return ar.Rule(
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


# One recorded `_log_decision` call: its positional args and its keyword args.
DecisionCall = Tuple[Tuple[Any, ...], Dict[str, Any]]


def region_of(canonical: str) -> ar.DialogRegion:
    """A region carrying ``canonical`` in both match domains."""
    return ar.DialogRegion(rows=(canonical,), normalized=canonical, normalized_light=canonical)


class TestTheStall:
    """The 00:32:35Z frame, and what each version of the rule does with it."""

    def test_the_shipped_regex_rule_misses_the_frame_it_was_written_for(self) -> None:
        """The reject the decisions log recorded four times, reproduced."""
        rule = shipped_at_stall()
        assert rule.matches(region_of(STALL_REGION_CANONICAL)) is False
        assert rule.reject_reason(region_of(STALL_REGION_CANONICAL)) == "question(regex)"

    def test_the_fixed_rule_fires_on_that_same_frame(self) -> None:
        rule = fixed()
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
        rule = fixed()
        assert rule.reject_reason(region) is None

    def test_the_shipped_regex_rule_does_not(self) -> None:
        region = ar.dialog_region(superpose(CLEAN_CARD, LEFTOVER_SCREEN))
        rule = shipped_at_stall()
        assert rule.reject_reason(region) == "question(regex)"

    def test_an_uncorrupted_card_still_matches_the_ordinary_way(self) -> None:
        """The fallback must not be the only thing holding the rule up."""
        region = ar.dialog_region(CLEAN_CARD)
        rule = fixed()
        assert rule._canon_question in region.normalized  # exact path, no fallback
        assert rule.matches(region) is True


class TestOptionsGetTheSameTreatment:
    """An option anchor is prose on the same corrupted screen as the question."""

    def test_a_bled_option_still_matches(self) -> None:
        # "2. Use current directory" composited as "2 huseecurrent directory".
        assert "2 huseecurrent directory" in STALL_REGION_CANONICAL
        rule = fixed()
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

    def test_a_gap_is_exactly_one_cell_wide(self) -> None:
        """The allowance is the physics: one blank cell, one stale character.

        Two characters between two anchor words is not a bleed. A single blank
        cell cannot hold two characters, so a two-character run is different
        text and must not match.

        The strings here are lowercase because the canonical domain is: the fold
        lowercases before matching, so a stale glyph reaches the pattern as
        ``[a-z0-9]`` and nothing else.
        """
        pattern = ar.bleed_tolerant_pattern("press enter to continue")
        assert pattern is not None
        assert pattern.search("press enter to continue") is not None  # uncorrupted
        assert pattern.search("press enter todcontinue") is not None  # one stale glyph
        assert pattern.search("press enter to7continue") is not None  # a stale digit
        assert pattern.search("press enter todxcontinue") is None  # two: not a bleed
        assert pattern.search("press enter todxycontinue") is None

    def test_the_cell_may_hold_any_glyph_not_just_a_letter(self) -> None:
        """The previous frame is ordinary output, so the stale glyph can be
        anything: a path separator, a rule, a card wall, a bullet.

        Pinned against the pattern directly, in the light (punctuation-
        preserving) domain, because the full canonical fold would turn every one
        of these back into a space and the assertion would prove nothing.
        """
        pattern = ar.bleed_tolerant_pattern("use current directory")
        assert pattern is not None
        for glyph in "/─│·:%.-":
            bled = f"use{glyph}current{glyph}directory"
            assert pattern.search(bled) is not None, f"{glyph!r} should be one cell"
        assert pattern.search("use//current directory") is None  # two cells

    # Two rows taken from real panes under
    # ~/.aws/cli-agent-orchestrator/logs/terminal/*.scrollback: a box-drawing
    # separator, and one of the `-c` config fragments from the codex launch echo
    # that was sitting under the card in the 162c159f stall itself.
    PUNCTUATION_BACKGROUNDS: Dict[str, str] = {
        "box-drawing rule": "─" * 80,
        "launch -c fragment": (
            "-c 'mcp_servers.cao-mcp-server.env.CAO_TERMINAL_ID=\"162c159f\"' --search"
        ),
    }

    @pytest.mark.parametrize("label", sorted(PUNCTUATION_BACKGROUNDS))
    def test_a_punctuation_bleed_from_a_real_row_still_matches(self, label: str) -> None:
        """Replay, not construction: a real pane row under the card's title.

        The title's six word gaps come back holding whatever that row had at
        those columns — `─` from the separator, and `.`/`T` from the config
        fragment. Neither is alphanumeric-and-lowercase, which is exactly the
        assumption an `[a-z0-9 ]` cell class would have made.
        """
        title = "Choose working directory to resume this session"
        raw = superpose([title], [self.PUNCTUATION_BACKGROUNDS[label]])[0]
        assert title not in raw, "the background must actually have bled in"
        stale = [raw[i] for i, c in enumerate(title) if c == " "]
        assert any(not c.isalnum() for c in stale), f"no punctuation bled: {stale}"

        # Light domain: punctuation survives intact, and the one-cell class
        # spans it. This is the assertion an `[a-z0-9 ]` class fails.
        pattern = ar.bleed_tolerant_pattern(ar.canonicalize(title))
        assert pattern is not None
        assert pattern.search(ar.canonicalize_light(raw)) is not None

        # Full domain: each stale glyph is either folded back to a space or
        # kept as one lowercase alphanumeric, so it is still one cell and the
        # same pattern spans it.
        assert pattern.search(ar.canonicalize(raw)) is not None

    def test_the_fold_lowercases_so_case_is_never_a_reason_to_miss(self) -> None:
        """The `T` of CAO_TERMINAL_ID lands in one of the title's gaps.

        A case-sensitive cell class would miss on it. The fold lowercases before
        matching, so it arrives as `t` and the gap is spanned like any other.
        """
        title = "Choose working directory to resume this session"
        raw = superpose([title], [self.PUNCTUATION_BACKGROUNDS["launch -c fragment"]])[0]
        stale = [raw[i] for i, c in enumerate(title) if c == " "]
        assert any(c.isupper() for c in stale), f"no uppercase bled: {stale}"
        folded = ar.canonicalize(raw)
        assert folded == folded.lower()
        assert "thistsession" in folded  # the T, lowercased, sitting in the gap
        pattern = ar.bleed_tolerant_pattern(ar.canonicalize(title))
        assert pattern is not None
        assert pattern.search(folded) is not None

    def test_a_two_word_anchor_gets_no_tolerance_at_all(self) -> None:
        """A widening is paid for by the specificity around it.

        Two words with one wildcard between them is not a phrase. The short
        anchors in the shipped rules — "Yes, continue", "No, quit" — keep the
        exact substring test and nothing else.
        """
        assert ar.bleed_tolerant_pattern("yes continue") is None
        assert ar.bleed_tolerant_pattern("no quit") is None
        assert ar.bleed_tolerant_pattern("use current directory") is not None

    def test_absent_words_are_still_absent(self) -> None:
        rule = fixed()
        assert rule.matches(region_of("resuming session working on the directory")) is False

    def test_a_single_word_anchor_gets_no_pattern(self) -> None:
        """No gaps to tolerate, and a word interior is never corrupted."""
        assert ar.bleed_tolerant_pattern("continue") is None
        assert ar.bleed_tolerant_pattern("") is None
        assert ar.BLEED_MIN_ANCHOR_WORDS == 3

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

    BACKGROUND: List[str] = [
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

    def test_the_question_survives_a_bleed_but_the_short_options_do_not(self) -> None:
        """Provider-independence, and the exact edge of what it buys.

        The matcher is shared, so grok gets the tolerance the moment codex
        does — the seven-word question still matches this card superposed on the
        launch command's echoed system prompt, which is what sits above it on a
        real grok pane.

        Its OPTIONS do not, and that is the word floor working as designed:
        "Yes, proceed" and "No, quit" are two words each, so they keep the exact
        substring test, and a stale glyph in either gap defeats them. Recorded
        rather than fixed by loosening the floor — a two-word anchor with a
        wildcard in it is not specific enough to auto-answer on. The rule would
        become bleed-proof by anchoring on a three-word option instead, which is
        a config change for the supervisor and is written up in the follow-ups.

        This is synthetic. Neither live grok capture was bled: both matched
        cleanly and fired.
        """
        bled = superpose(self.CARD, self.BACKGROUND)
        canonical = ar.normalize_screen(bled)
        assert "do you trust the contents of this directory" not in canonical
        region = ar.dialog_region(bled)

        question_only = ar.Rule(
            name="probe",
            enabled=True,
            match_mode="contains",
            question="Do you trust the contents of this directory?",
            options=[],
            answer=["y"],
        )
        assert question_only.reject_reason(region) is None

        assert self._rule().reject_reason(region) == "option[Yes, proceed]"

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


class TestTheSecondStall:
    """b637333f, 2026-09-05 ~03:00Z. Same fault, and a lesson about evidence.

    Reported as a case the bleed model does not explain: the responder pushed
    "unknown blocking dialog… no rule matched" carrying text that is completely
    clean, so the anchor should have matched and the matcher must be at fault.

    The clean text is real, but it is not the text that was evaluated. The
    unknown-dialog push takes a FRESH capture for its payload
    (`_capture_for_analysis`), and by the time it ran the pane had settled into
    its final repaint. The frames the rules were actually judged on are in the
    decisions log, and they are bled exactly like 162c159f's:

        ec2 ruseycurrent directory
        b64 3always useacurrent3directory
        comm anpress enter to continueare uv tools

    The live build was `ca94554c`, which has no bleed tolerance, so:

      * 24 evaluations rejected on `question(contains)` — the title itself bled
      * 7 evaluations rejected on `option[Use current directory]` — the title
        rendered clean, the option did not

    `useycurrent` is one stale cell between two intact words. It is the whole
    thesis in one token, and the fix spans it.
    """

    # VERBATIM from ~/.aws/cli-agent-orchestrator/logs/auto-answers/
    # b637333f.decisions.log, reason=region_dump, the frame carrying the card.
    EVALUATED_REGION = (
        "choose working directory to resume this session b637333f on cao b637333f via v3 14 7 on "
        "session latest cwd recorded in the resumed session current your current working directory "
        "b637333f on cao b637333f via v3 14 7 on 1 use session directory home chao vscode projects "
        "cli subagents ec2 ruseycurrent directory data cao scratch worktrees cli subagents b637333f "
        "3 always use session directory b64 3always useacurrent3directory v3 14 7 on 5 codex resume "
        "dangerously bypass approvals and sandbox no alt screen disable shell snapshot dangerously "
        "bypass hook trust model gpt 5 6 sol c mcp servers cao mcp server comm anpress enter to "
        "continueare uv tools cli agent orchestrator bin cao"
    )

    # The push payload, verbatim. A later frame of the same pane.
    PUSH_PAYLOAD = (
        "choose working directory to resume this session session latest cwd recorded in the "
        "resumed session current your current working directory 1 use session directory home chao "
        "vscode projects cli subagents 2 use current directory data cao scratch worktrees cli "
        "subagents b637333f 3 always use session directory 4 always use current directory press "
        "enter to continue"
    )

    def test_the_evaluated_frame_was_bled_even_though_the_payload_was_clean(self) -> None:
        """The two are different frames of one pane. That is the whole confusion."""
        assert "use current directory" in self.PUSH_PAYLOAD
        assert "use current directory" not in self.EVALUATED_REGION
        assert "ruseycurrent directory" in self.EVALUATED_REGION

    def test_the_live_build_could_not_have_fired_on_it(self) -> None:
        """Plain substring, which is all `ca94554c` has. The title is there; the
        option is not. That is the `option[Use current directory]` in the log."""
        assert ar.canonicalize("Choose working directory to resume this session") in (
            self.EVALUATED_REGION
        )
        assert ar.canonicalize("Use current directory") not in self.EVALUATED_REGION

    def test_the_fix_fires_on_it(self) -> None:
        rule = fixed()
        region = region_of(self.EVALUATED_REGION)
        assert rule.reject_reason(region) is None
        assert rule.matches(region) is True
        assert rule.answer == ["Down", "Enter"]

    def test_each_bled_gap_in_it_is_one_cell(self) -> None:
        """Not a new mechanism needing a wider allowance — the same one."""
        pattern = ar.bleed_tolerant_pattern(ar.canonicalize("Use current directory"))
        assert pattern is not None
        match = pattern.search(self.EVALUATED_REGION)
        assert match is not None
        assert match.group() == "useycurrent directory"

    def test_the_clean_payload_would_have_matched_all_along(self) -> None:
        """Which is why reading it alone leads straight to the wrong conclusion."""
        assert fixed().matches(region_of(self.PUSH_PAYLOAD)) is True
        assert ar.canonicalize("Use current directory") in self.PUSH_PAYLOAD

    # A third seat, 5d2d125c at 05:06Z, reported the same way: clean push text,
    # no match. Its evaluated frame is in its own decisions log and carries the
    # identical corruption — `ruseycurrent`, one stale cell, in a worktree whose
    # path differs entirely. Three independent captures, one mechanism.
    THIRD_REGION = (
        "choose working directory to resume this session 5d2d125c on cao 5d2d125c is v2 5 0 via "
        "v3 14 7 on session latest cwd recorded in the resumed session current your current "
        "working directory 5d2d125c on cao 5d2d125c is v2 5 0 via v3 14 7 on 1 use session "
        "directory home chao vscode projects cli subagents cli agent orchestrator ec2 ruseycurrent "
        "directory data cao scratch worktrees cli agent orchestrator 5d2d125c 3 always use session "
        "directory 5d4 1always useacurrent2directory v2 5 0 via v3 14 7 on codex resume dangerously "
        "bypass approvals and sandbox no alt screen disable shell snapshot dangerously bypass hook "
        "trust model gpt 5 6 sol c mcp servers cao mcp server command hpress enter to continuev"
    )

    def test_the_third_seat_is_the_same_one_cell_bleed(self) -> None:
        assert "ruseycurrent directory" in self.THIRD_REGION
        assert ar.canonicalize("Use current directory") not in self.THIRD_REGION
        rule = fixed()
        assert rule.reject_reason(region_of(self.THIRD_REGION)) is None
        assert rule.matches(region_of(self.THIRD_REGION)) is True

    @pytest.mark.parametrize("label", ["second", "third"])
    def test_both_later_seats_bleed_the_same_option_the_same_way(self, label: str) -> None:
        """`useycurrent` in two unrelated worktrees. Not a coincidence of paths."""
        region = self.EVALUATED_REGION if label == "second" else self.THIRD_REGION
        pattern = ar.bleed_tolerant_pattern(ar.canonicalize("Use current directory"))
        assert pattern is not None
        match = pattern.search(region)
        assert match is not None
        assert match.group() == "useycurrent directory"


class TestTheFalsePositiveBound:
    """The property that matters, stated where it is decided: at the RULE.

    EMPIRICAL gate r1 rejected the first cut for letting a ≤3-character letter
    run stand in for a word gap, and it was right — that is not what the physics
    produces. The gap is one cell wide now, which removes the gate's
    `sessionXYZ use` and `currentABCdirectory` cases outright.

    `workingXdirectory` is a single cell and still satisfies that FRAGMENT, and
    it should: at the matcher there is no way to tell a stale glyph from a
    letter someone typed. The bound is therefore not "the anchor never matches
    prose" — it is that a RULE never fires on prose. Three gates stand between a
    fragment and a keystroke: every word of the question, every word of every
    option, and a two-capture settle proving the frame is byte-stable.
    """

    # The three screens the gate built, verbatim in shape.
    GATE_SCREENS: Dict[str, str] = {
        "workingXdirectory": (
            "choose workingXdirectory to resume this session before the next step"
        ),
        "sessionXYZ use": "restore the sessionXYZ use of the previous working directory",
        "currentABCdirectory": "listing the currentABCdirectory for the resumed session",
    }

    # Real lines lifted from panes under
    # ~/.aws/cli-agent-orchestrator/logs/terminal/*.scrollback: doctrine prose,
    # a launch command, and a CLI help table. All three carry anchor words.
    REAL_PROSE: Dict[str, List[str]] = {
        "worktree-containment doctrine": [
            "11. WORKTREE CONTAINMENT (F452): when your task was provisioned with",
            "an isolated worktree, your working directory AT SPAWN is that worktree",
            "— it is the ONLY place you may build, commit, or resume a session.",
        ],
        "launch command echo": [
            "cao launch --agents chao_supervisor --provider claude_code \\",
            '  --session-name "$SESSION_CLI_NAME" --working-directory .',
            "  --headless --yolo   # resume the session directory afterwards",
        ],
        "cli help table": [
            "  -l/--list-sessions   list saved sessions for the current directory",
            "  --session-source <v1|v2>   narrow --delete-session target store",
            "  Use current directory as the root when no session is chosen.",
        ],
    }

    def _shipped_codex_rules(self) -> List[ar.Rule]:
        path = Path(str(ar.AUTO_ANSWER_DIR / "codex.yaml"))
        if path.exists():
            return [r for r in ar._RuleStore._load(path) if r.enabled]
        return [fixed()]

    @pytest.mark.parametrize("label", sorted(GATE_SCREENS))
    def test_no_shipped_rule_fires_on_the_gates_screens(self, label: str) -> None:
        region = region_of(ar.canonicalize(self.GATE_SCREENS[label]))
        fired = [r.name for r in self._shipped_codex_rules() if r.matches(region)]
        assert fired == [], f"{label} fired {fired}"

    @pytest.mark.parametrize("label", sorted(REAL_PROSE))
    def test_no_shipped_rule_fires_on_real_pane_prose(self, label: str) -> None:
        region = ar.dialog_region(self.REAL_PROSE[label])
        fired = [r.name for r in self._shipped_codex_rules() if r.matches(region)]
        assert fired == [], f"{label} fired {fired}"

    def test_the_multi_character_gaps_die_at_the_anchor(self) -> None:
        """Two of the gate's three never reach the option check at all."""
        pattern = ar.bleed_tolerant_pattern("use session directory")
        assert pattern is not None
        assert pattern.search("use sessionXYZ directory") is None
        pattern = ar.bleed_tolerant_pattern("use current directory")
        assert pattern is not None
        assert pattern.search("use currentABCdirectory") is None

    def test_a_single_cell_fragment_needs_the_whole_rule_to_be_harmless(self) -> None:
        """`workingXdirectory` DOES satisfy its fragment. It still cannot fire.

        This is the bound in one test: the fragment matches, the full question
        matches, and the rule is still refused because the option is not there.
        """
        prose = ar.canonicalize(self.GATE_SCREENS["workingXdirectory"])
        fragment = ar.bleed_tolerant_pattern("choose working directory")
        assert fragment is not None
        assert fragment.search(prose) is not None
        rule = fixed()
        assert rule._bleed_question is not None
        assert rule._bleed_question.search(prose) is not None
        assert rule.reject_reason(region_of(prose)) == "option[Use current directory]"
        assert rule.matches(region_of(prose)) is False

    def test_a_matched_frame_that_keeps_changing_sends_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The settle gate is the third guard, and it is load-bearing.

        Even a frame that matches every part of a rule sends no key until two
        captures a beat apart are byte-identical. A moving frame withholds.
        """
        responder = ar.AutoResponder()
        rule = fixed()
        frames = [ar.dialog_region(CLEAN_CARD), ar.dialog_region(CLEAN_CARD + ["working"])]
        seen = 0

        def moving(
            terminal_id: str, chrome_patterns: "List[re.Pattern[str]] | None"
        ) -> "ar.DialogRegion | None":
            nonlocal seen
            seen += 1
            return frames[min(seen - 1, 1)]

        monkeypatch.setattr(responder, "_settle_capture", moving)
        monkeypatch.setattr(ar, "_clock_sleep", lambda _seconds: None)
        region = ar.dialog_region(CLEAN_CARD)
        settled = responder._settle_before_first_send(
            "t1", _StubProvider(), region.with_digests(settle="d", consume="d"), rule
        )
        assert settled is False, "a moving frame must not be settled"
        assert seen == 2, "the gate must sample twice before deciding"


class _StubProvider:
    """Just enough provider for the settle gate's chrome-pattern lookup."""

    def chrome_row_patterns(self) -> List["re.Pattern[str]"]:
        return []


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

    def _responder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> "tuple[ar.AutoResponder, List[DecisionCall]]":
        recorded: List[DecisionCall] = []
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
