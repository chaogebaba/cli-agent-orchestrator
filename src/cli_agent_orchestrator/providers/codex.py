"""Codex CLI provider implementation."""

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from cli_agent_orchestrator.utils.persona_context import PersonaPlan

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import BLOCKED_WAIT_CAP_S, CAO_HOME_DIR, PYTE_SCREEN_ROWS
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
from cli_agent_orchestrator.providers.base import (
    BaseProvider,
    RetryableArtifactValidation,
    TerminalArtifactValidation,
)
from cli_agent_orchestrator.providers.screen_classification import (
    ScreenClassificationResult,
    ScreenSignal,
    screen_classification_result,
)
from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_provider_profile_defaults,
    get_server_settings,
    resolve_provider_string_option,
)
from cli_agent_orchestrator.utils import provider_plane
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.binary_resolution import resolve_provider_binary
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.provider_plane import provider_home
from cli_agent_orchestrator.utils.sandbox_guard import bind_mcp_server_identity
from cli_agent_orchestrator.utils.terminal import (
    BlockedWaitPolicy,
    wait_for_shell,
    wait_until_status,
)
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)


def _resolved_codex_home(terminal_id: str | None) -> Path:
    from cli_agent_orchestrator.utils.persona_context import resolve_codex_home

    resolved = resolve_codex_home(terminal_id)
    if resolved == provider_plane.provider_home("codex").home:
        return provider_home("codex").home
    return resolved


# Regex patterns for Codex output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
IDLE_PROMPT_PATTERN = r"(?:❯|›|»|codex>)"
# Number of lines from the bottom of capture to check for the idle prompt.
# With --no-alt-screen, codex output is inline (scrollback contains history),
# so we can't anchor to \Z. Instead, check the last few lines where the prompt
# and status bar appear.
IDLE_PROMPT_TAIL_LINES = 5
# The idle prompt character ❯ (U+276F) is rendered on-screen by capture-pane
# but is NOT written to the raw output stream captured by pipe-pane.  Instead,
# the TUI footer text "? for shortcuts" is reliably present whenever the TUI
# is active.  This is intentionally permissive — _has_idle_pattern() is a
# lightweight pre-check; the real status decision is made by get_status()
# which uses capture-pane (rendered screen).
# Match assistant response start: "assistant:/codex:/agent:" (label style from synthetic
# test fixtures) or "•" bullet point (real Codex interactive output format).
# [^\S\n]* matches horizontal whitespace only (not newlines) so the match anchors
# on the actual bullet line — using \s* would let the match start on a blank
# line above the bullet, breaking per-line tool-call filtering downstream.
ASSISTANT_PREFIX_PATTERN = r"^(?:(?:assistant|codex|agent)\s*:|[^\S\n]*•)"
# MCP tool call marker emitted by Codex when invoking a tool, e.g.
# "• Called cao-mcp-server.load_skill({...})". The body that follows
# (└ ... lines) is the tool's return value, not the model's reply.
# Used to skip these markers when locating the actual response start.
# The "<server>.<tool>(" shape (identifier.identifier followed by an open
# paren) is required so legitimate model bullets like "• Called attention
# to the bug" don't get filtered as tool calls.
MCP_TOOL_CALL_PATTERN = r"^[^\S\n]*•\s+Called\s+[\w-]+\.[\w-]+\("
# Codex startup/system notice bullets that are NOT model replies, e.g.
# "• You have 3 usage limit resets available. Run /usage to use one."
# These render with the same "•" prefix as assistant messages; without this
# filter a fresh terminal showing only the banner is classified COMPLETED and
# the banner text gets extracted as the model's reply (false handoff success).
SYSTEM_NOTICE_PATTERN = r"^[^\S\n]*•\s+You have \d+ usage limit reset"
# Match user input: "You ..." (label style), "› text" (older Codex interactive
# prompt), or "» text" (Codex 0.149+ interactive prompt). The prompt alternative
# requires a non-whitespace character on the same line to distinguish user input
# from an empty idle prompt.
# [^\S\n] matches horizontal whitespace only (spaces/tabs), preventing the pattern
# from crossing newline boundaries into subsequent lines.
USER_PREFIX_PATTERN = r"^(?:You\b|[›»][^\S\n]*\S)"
# Strict idle prompt pattern for extraction: matches empty prompt lines only.
# Distinguishes "› " (idle) from "› user message" (user input with text).
IDLE_PROMPT_STRICT_PATTERN = r"^\s*(?:❯|›|»|codex>)\s*$"
IDLE_PROMPT_SCREEN_PATTERN = rf"^\s*{IDLE_PROMPT_PATTERN}"

PROCESSING_PATTERN = r"\b(thinking|working|running|executing|processing|analyzing)\b"
WAITING_PROMPT_PATTERN = r"^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b"
# F516 D2: the resume-working-directory chooser Codex renders on session resume.
# It is a numbered menu with a "Press enter to continue" footer, not an approval
# modal, so none of the existing waiting signals fire on it and the classifier
# lands IDLE — the F509 shape (codex.py pattern table). The title line is a
# stable, bottom-anchored hook; classify it WAITING_USER_ANSWER so the responder
# fast-path corroborates the codex-resume-working-directory rule on the first
# eval. Over-matching is safe here (a false WAITING merely withholds work).
RESUME_CWD_CHOOSER_PATTERN = re.compile(
    r"Choose working directory to resume this session", re.IGNORECASE
)
ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"
CONTENT_POLICY_REFUSAL_PATTERN = r"(?i)^ⓘ This content can't be shown"
CONTENT_POLICY_SCREEN_RULE_ID = "codex.screen.content-policy-refusal.v1"
CONTENT_POLICY_ARTIFACT_RULE_ID = "codex.artifact.refusal-echo.v1"

TRANSIENT_API_ERROR_PATTERNS = (
    r"(?i)invalid_request_error",
    r"(?i)too many requests",
    r"(?i)\b429\b.*(too many|rate)",
    r"(?i)400 bad request",
    r"(?i)502 bad gateway",
    r"(?i)nginx(/[0-9.]+)?",
    r"(?i)stream (error|disconnected)",
    r"(?i)^⚠ Selected model is at capacity",
)
TRANSIENT_ERROR_EXCLUSIONS = (
    r"(?i)invalid_api_key|authentication|unauthorized",
    r"(?i)model_not_found|unknown model",
    r"(?i)usage limit|insufficient_quota|quota",
    r"(?i)content policy|safety",
    r"(?i)\b403\b|forbidden",
    CONTENT_POLICY_REFUSAL_PATTERN,
)

# Codex TUI footer indicators (status bar below the idle prompt).
# Keep this case-insensitive: status_line labels and model names vary by version.
_TUI_CONTEXT_RE = r"(?:context\s+\d+%\s+left|\d+%\s+(?:context\s+)?left)"
_TUI_LIMIT_RE = (
    r"(?:(?:\d+\s*h(?:\s+\d+\s*m)?(?:\s+\d+\s*s)?)|" r"(?:\d+\s*m(?:\s+\d+\s*s)?))\s+left"
)
_TUI_MODEL_EFFORT_RE = r"[^·\n]+?\s+(?:minimal|low|medium|high|xhigh|max|ultra)"
TUI_FOOTER_PATTERN = (
    r"(?i)^\s*(?:\?\s+for shortcuts(?:\s+.*)?|"
    + _TUI_CONTEXT_RE
    + r"|"
    + _TUI_LIMIT_RE
    + r"|[^›\n]+·\s+[~/][^\n]*)\s*$"
)
_TUI_PATH_WITH_KNOWN_TAIL_RE = re.compile(
    rf"^[~/][^\n›]*\s+·\s+{_TUI_MODEL_EFFORT_RE}"
    rf"(?:\s+·\s+(?:{_TUI_CONTEXT_RE}|{_TUI_LIMIT_RE}))?$",
    re.IGNORECASE,
)
_TUI_PATH_BRANCH_ONLY_RE = re.compile(r"^[~/][^\n›]*\s+·\s+\S[^\n]*$")
_TUI_PATH_ONLY_RE = re.compile(r"^[~/][^\n›]*$")
_TUI_PATH_TRUNCATED_RE = re.compile(r"^[~/][^\n›]*…$")
# Codex TUI progress spinner: "• Working (0s • esc to interrupt)",
# "• Thinking (3m 39s ...)",
# "• Starting script creation (1h 2m 3s • esc to interrupt)".
# The prefix text and elapsed-time format vary, but the interrupt hint is stable.
# Appears inline with --no-alt-screen when the agent is actively processing.
# Must be checked before COMPLETED to avoid false positives (the • matches
# ASSISTANT_PREFIX_PATTERN and the TUI footer › matches idle prompt).
TUI_PROGRESS_PATTERN = r"•.*\([^)]*\besc to interrupt\)"
SCREEN_FALLBACK_PROCESSING_PATTERN = re.compile(r"\A[\s\S]*\Z")

# Workspace trust/approval prompt shown when Codex opens a new directory
TRUST_PROMPT_PATTERN = (
    r"(?:allow Codex to work in this folder" r"|Do you trust the contents of this directory)"
)
TRUST_SELECTOR_PATTERN = re.compile(
    r"^\s*›\s*1\.\s*(?:Yes|Allow|Trust|Continue)\b",
    re.IGNORECASE | re.MULTILINE,
)
DIALOG_ACTION_FOOTER_PATTERN = re.compile(
    r"(?:Press enter to\s+(?:confirm|continue|view)|"
    r"Press space to select|"
    r"left/right\s+group\s+.*enter\s+edit shortcut.*esc\s+close)",
    re.IGNORECASE,
)
# Shared footer used by trust-v2 / login menu bottom-anchored matches.
TRUST_PROMPT_FOOTER = r"Press enter to continue"

# First-run auth menu (no credentials). Cannot be auto-dismissed — operator task.
# Bottom-anchored with footer to avoid scrollback false matches. See initialize()
# WAITING_USER_ANSWER target set and blocks_orchestrated_input_while_waiting_user_answer.
LOGIN_MENU_PATTERN = r"Sign in with ChatGPT"
LOGIN_MENU_FOOTER = TRUST_PROMPT_FOOTER

# Startup "Update available!" dialog. Codex shows this at startup when a newer
# release exists, with a numbered menu whose cursor default is option 1:
#   ✨ Update available! 0.142.5 -> 0.144.5
#   1. Update now (runs npm install -g @openai/codex)
#   2. Skip
#   3. Skip until next version
#   Press enter to continue
# A blind Enter would run a GLOBAL npm install that swaps the codex binary under
# every other running CAO worker. We suppress with
# -c check_for_update_on_startup=false at launch AND detect+dismiss with
# '3'+Enter as defense-in-depth.
UPDATE_DIALOG_PATTERN = r"Update available!\s+\S+\s+->\s+\S+"
UPDATE_DIALOG_MENU_PATTERN = r"Skip until next version"
UPDATE_DIALOG_FOOTER = r"Press enter to continue"
_UPDATE_DIALOG_ROW_PATTERNS = (
    re.compile(rf"^(?:✨\s*)?{UPDATE_DIALOG_PATTERN}$", re.IGNORECASE),
    re.compile(r"^(?:›\s*)?1\.\s+Update now(?:\s+\(.*\))?$", re.IGNORECASE),
    re.compile(r"^(?:›\s*)?2\.\s+Skip$", re.IGNORECASE),
    re.compile(rf"^(?:›\s*)?3\.\s+{UPDATE_DIALOG_MENU_PATTERN}$", re.IGNORECASE),
    re.compile(rf"^{UPDATE_DIALOG_FOOTER}$", re.IGNORECASE),
)
STARTUP_PROMPT_BOTTOM_LINES = 15
STARTUP_ACTIVITY_PATTERN = r"^\s*•[^\S\n]+\S"
_MCP_INTERRUPT_PATTERN = re.compile(r"MCP startup interrupted", re.IGNORECASE)
# Codex's runtime approval prompt as actually rendered by codex-cli 0.147.0,
# verified against a live tmux capture (test/providers/fixtures/
# codex_approval_modal_raw.txt):
#
#     Would you like to run the following command?
#
#     Environment: local
#
#     $ mkdir -p /private/tmp/codex-work-567
#
#   › 1. Yes, proceed (y)
#     2. Yes, and don't ask again for commands that start with `mkdir -p ...` (p)
#     3. No, and tell Codex what to do differently (esc)
#
#     Press enter to confirm or esc to cancel
#
# It is NOT a box-drawn modal and carries no "[a] Accept"/"[d] Decline" keys: it
# is a numbered menu with a `›` selection cursor and a confirm footer.
#
# THE TITLE IS NOT THE HOOK. An earlier revision of this detector required one of
# three enumerated question titles inside a fixed-height bottom window, and both
# halves of that were wrong:
#
#   * The enumeration is incomplete, and cannot be completed. `strings` over the
#     0.147.0 native binary turns up at least two more approval titles driving the
#     same menu -- `Do you want to approve network access to "<host>"?` (emitted
#     when features.network_proxy is on, with its own `Yes, and allow this host in
#     ...` / `No, and block this host in the future` options) and `Approve app tool
#     call?` -- so any title list is a list of the variants someone happened to
#     have already seen.
#   * The fixed window fails OPEN. The command preview between the title and the
#     menu is the verbatim command, untruncated by the renderer, so a multi-line
#     heredoc pushes the title out of any fixed row budget; the detector then found
#     no title and returned IDLE for a hard-blocked pane, which is the dangerous
#     direction to be wrong in.
#
# So detection is structural and bottom-up instead -- see
# :func:`_has_approval_prompt_in_bottom`. These title patterns survive only as the
# permissive NEGATIVE gate in STARTUP_BLOCKING_INPUT_PATTERN below, where an extra
# match merely keeps the startup poll going and is therefore free; the network
# title is folded in there for the same reason. Nothing positive is classified off
# a title any more.
APPROVAL_PROMPT_PATTERN = (
    r"(?:Would you like to (?:run the following command"
    r"|make the following edits"
    r"|grant these permissions)\?"
    r"|Do you want to approve network access to)"
)
# The footer under a blocking list menu is OPTIONAL CORROBORATION, not an
# anchor: both list actions are unbindable (`tui.keymap.list.accept = []` --
# an empty list explicitly unbinds; config/src/tui_keymap.rs at rust-v0.147.0),
# and accept_cancel_hint_line (tui/src/bottom_pane/popup_consts.rs) renders one
# of FOUR shapes accordingly:
#   both bound     -> "Press <a> to confirm or <c> to cancel"
#   accept unbound -> "Press <c> to cancel"
#   cancel unbound -> "Press <a> to confirm"
#   both unbound   -> no footer line at all
# The approval overlay may append " or <k> to open thread"
# (approval_overlay.rs), which is also the WHOLE line when both list actions
# are unbound, and the generic popups (model picker) say "to go back" where
# the approval says "to cancel". So this pattern's job is only to recognise a
# footer line as part of the menu block wherever one is rendered.
#
# The key labels are emitted as separately styled spans -- the sentence is not
# one literal in the binary -- and a label is NOT a single token: a two-stroke
# chord such as "ctrl-x ctrl-s" renders via ShortcutHint::display_label() as
# `ctrl + x ctrl + s` (strokes joined by a space, modifiers by " + ";
# key_hint.rs). Worst legal label is a chord whose strokes each carry
# ctrl+shift+alt: 7 tokens per stroke, 14 in total, so a label is matched as
# 1-15 whitespace-separated tokens. The bound keeps a same-line prose sentence
# from bridging an unrelated "Press" to a distant "to confirm".
APPROVAL_PROMPT_FOOTER = (
    r"Press \S+(?:[^\S\n]+\S+){0,14} to (?:confirm|cancel|go back|open thread)\b"
)
# One numbered menu option: "› 1. Yes, proceed (y)", "  2. No, ... (esc)". The
# selection cursor is optional here because it sits on exactly one option at a
# time and moves as the operator arrows around.
APPROVAL_MENU_OPTION_PATTERN = r"^[^\S\n]*(?:›[^\S\n]+)?\d+\.[^\S\n]+\S"
# The selected option with its cursor flush at column 0, which is where Codex
# draws every transcript gutter marker. This is the same left-margin argument
# _modal_line_content makes for the boxed modal: quoted or continuation prose is
# indented under its bullet, so a menu the model merely PASTED into its own reply
# carries its cursor at column >= 2 and fails this while a live one passes.
APPROVAL_MENU_CURSOR_PATTERN = r"^›[^\S\n]+\d+\.[^\S\n]+\S"
# A wrapped option's continuation row. ListSelectionView renders wrapped rows by
# default (SelectionRowDisplay::Wrapped, list_selection_view.rs at
# rust-v0.147.0), and word_wrap_line indents every continuation to the option
# TEXT column -- the width of the "{prefix} {n}. " gutter, so 5 columns for a
# single-digit menu and one more per extra digit (build_rows sets wrap_indent to
# the prefix width; wrap_standard_row feeds it to subsequent_indent). The
# question, command preview, and footer rows all sit at 2 columns of indent, so
# >= 5 columns directly under an option row is the menu's own wrapping, never
# new content. Only honoured while inside an option (see the below-anchor scan)
# -- indented prose elsewhere still disqualifies the block.
APPROVAL_MENU_CONTINUATION_PATTERN = r"^[^\S\n]{5,}\S"
# Minimum numbered options required above the footer. Two is the floor for a
# genuine approval (accept and decline); requiring specific option COPY instead
# would reintroduce exactly the enumeration fragility described above.
APPROVAL_MENU_MIN_OPTIONS = 2

# Codex's boxed command-approval modal:
#   ╭─ Command Approval Required ─╮
#   │ [a] Accept  [d] Decline     │
#   ╰─────────────────────────────╯
# WARNING: this copy is NOT emitted by codex-cli 0.147.0. `strings` over the
# vendored native binary finds zero occurrences of "Command Approval Required",
# "] Accept", or "] Decline" -- the live prompt is APPROVAL_PROMPT_PATTERN above.
# The two patterns are kept because this copy predates the numbered menu and is
# already load-bearing in STARTUP_BLOCKING_INPUT_PATTERN below, so dropping them
# would silently un-guard whichever older Codex builds still render it. Treat
# _has_approval_modal_in_bottom as legacy/defensive: APPROVAL_PROMPT_PATTERN is
# what fires on current Codex.
#
# Split into header and choice-key halves because the two paths that consume
# them need different strictness. The startup path (_has_startup_idle_composer)
# uses the permissive OR below as a NEGATIVE gate — any one token vetoes
# "ready", and a false veto merely keeps polling, so over-matching is free.
# get_status() uses them as a POSITIVE classifier where over-matching would
# strand a healthy pane in WAITING_USER_ANSWER, so it corroborates the two
# halves separately (see _has_approval_modal_in_bottom). Box-drawing characters
# are deliberately NOT required: the frame chrome has changed across Codex
# releases while this copy has not.
APPROVAL_MODAL_HEADER_PATTERN = r"Command Approval Required"
APPROVAL_MODAL_CHOICE_PATTERN = r"(?:\[[aA]\]\s+Accept\b|\[[dD]\]\s+Decline\b)"
# Box-drawing frame and padding stripped from a modal line before matching, so a
# framed line ("│ [a] Accept  [d] Decline     │") reduces to its text content.
# Stripped as a character SET from both ends, hence no ordering assumption about
# corner/edge glyphs. Light, heavy, and double variants are all covered because
# only light glyphs have been observed and the frame style is not contractual.
#
# ASCII frame characters (+ - |) are deliberately EXCLUDED. They are markdown
# table syntax, so including them would let a table the model wrote in its own
# reply ("| Command Approval Required |" / "| [a] Accept | [d] Decline |")
# reduce to the exact modal shape. No Codex release has been observed using
# ASCII frames, so that trade buys a hypothetical false negative at the cost of
# a plausible false positive.
#
# Note this set also strips leading whitespace, so an INDENTED plain-text quote
# reduces to the modal shape too. That look-alike is excluded positionally
# instead — see _has_approval_modal_in_bottom.
MODAL_FRAME_CHARS = "─│╭╮╰╯├┤━┃┏┓┗┛┣┫═║╔╗╚╝╠╣ \t"
# The same set minus padding, used to tell "this line began with box chrome"
# from "this line began with a prose indent".
MODAL_FRAME_GLYPHS = frozenset(MODAL_FRAME_CHARS) - frozenset(" \t")
STARTUP_BLOCKING_INPUT_PATTERN = (
    rf"(?:{APPROVAL_MODAL_HEADER_PATTERN}|{APPROVAL_MODAL_CHOICE_PATTERN}|"
    rf"{APPROVAL_PROMPT_PATTERN}|{APPROVAL_PROMPT_FOOTER}|{TRUST_PROMPT_FOOTER})"
)
# MERGE NOTE (upstream bfc4d71f). Upstream's `_has_startup_idle_composer` reuses
# TUI_FOOTER_PATTERN, which upstream defines UNANCHORED
# (r"(?:\?\s+for shortcuts|context left|\d+%\s+left|·\s+[~/])") so it matches
# anywhere in a line. THIS FORK deliberately anchored TUI_FOOTER_PATTERN with
# ^...$ to stop mid-line prose from latching a footer. Codex 0.145 renders the
# status bar as "  gpt-5.6-sol medium · Context 100% left" — model name FIRST —
# so our anchored pattern does not match it and upstream's helper returned False
# for every placeholder (10 test failures on merge).
#
# Resolved by giving the STARTUP path its own footer predicate rather than
# loosening TUI_FOOTER_PATTERN, whose anchoring guards the runtime status
# classifier that F29/F31 hardened. Startup readiness and runtime status are
# different questions and now have different predicates.
STARTUP_FOOTER_PATTERN = (
    r"(?i)(?:\?\s+for shortcuts|context\s+\d+%\s+left|\d+%\s+(?:context\s+)?left|·\s+[~/])"
)
STARTUP_IDLE_PLACEHOLDER_PATTERN = (
    rf"^\s*{IDLE_PROMPT_PATTERN}[^\S\n]+(?:"
    r"Explain this codebase|"
    r"Summarize recent commits|"
    r"Implement \{feature\}|"
    r"Find and fix a bug in @filename|"
    r"Write tests for @filename|"
    r"Improve documentation in @filename|"
    r"Run /review on my current changes|"
    r"Use /skills to list available skills|"
    r"Ask Codex to do anything"
    r")\s*$"
)

# Codex welcome banner indicating normal startup (no trust prompt)
CODEX_WELCOME_PATTERN = r"OpenAI Codex"
CODEX_EMPTY_COMPOSER_PLACEHOLDERS = {
    "Explain this codebase",
    "Ask Codex to do anything",
    "Find and fix a bug in @filename",
    "Implement {feature}",
    "Improve documentation in @filename",
    "Run /review on my current changes",
    "Summarize recent commits",
    "Use /skills to list available skills",
    "Write tests for @filename",
}
# CSI SGR sequences only (colour/intensity). Used to walk dim state on
# escape-preserving capture-pane (-e) lines without treating cursor CSI as text.
_SGR_CSI_RE = re.compile(r"\x1b\[([0-9;]*)m")

# --- F435: paste-submit race recovery -------------------------------------
# When several codex TUIs initialize/receive input concurrently, the submit
# Enter after a bracketed paste is sometimes lost to a render race: the task
# text lands in the composer as a "[Pasted Content NNNN chars]" chip but is
# never submitted. The pane then sits idle at the drafted chip until the
# stalled-callback watchdog fires (~120s) or a human spots it.
#
# The stuck signature is the composer prompt line carrying the paste chip:
#   › [Pasted Content 3048 chars]
# A SUBMITTED composer instead shows an empty idle placeholder (e.g.
# "› Ask Codex to do anything") or the Working/Thinking spinner
# ("• Working (0s • esc to interrupt)"). We recover by re-sending Enter ONLY
# while the stuck chip is still present — never blind-Enter a submitted
# composer (idempotent, no double-submit).
CODEX_PASTE_CHIP_PATTERN = re.compile(r"[›»]\s*\[Pasted Content\s+\d+\s+chars\]")
# Grace before the first submission check: give the TUI a beat to register the
# paste and process the submit Enter under load.
CODEX_SUBMIT_VERIFY_GRACE_SECONDS = 2.0
# Bounded re-Enter attempts once the stuck chip is confirmed present.
CODEX_SUBMIT_VERIFY_MAX_RETRIES = 3
# Backoff between re-Enter attempts (seconds); grows per attempt.
CODEX_SUBMIT_VERIFY_BACKOFF_SECONDS = 1.0
# BLOCKER B1/B2 (r3): confirmation now requires POSITIVE evidence that the
# composer submitted, never mere absence of the chip. A single post-send
# capture can be a stale pre-paste frame (empty composer, chip not rendered
# yet) or a failed capture — treating either as success reopens the concurrent
# submit-loss window. Each capture is classified into three states:
#   submitted     — positive boundary crossed (empty idle placeholder/prompt in
#                   the active composer, or the Working/Thinking spinner).
#   stuck         — the active composer still carries the unsubmitted paste chip.
#   indeterminate — neither observed yet (stale pre-paste frame, mid-redraw, or
#                   a capture failure). NOT success: must be resolved by bounded
#                   re-observation before the grace window, and re-Enter + poll
#                   after it; if it never resolves to `submitted`, delivery is
#                   UNCONFIRMED and we raise CodexSubmitStuckError.
CODEX_SUBMIT_STATE_SUBMITTED = "submitted"
CODEX_SUBMIT_STATE_STUCK = "stuck"
CODEX_SUBMIT_STATE_INDETERMINATE = "indeterminate"
# Bounded re-observation polls for the grace window (BLOCKER B1): after the
# grace sleep we poll a few times for a POSITIVE submitted/stuck verdict before
# concluding indeterminate, so a stale pre-paste first frame cannot commit as
# success — a later frame that renders the durable chip is caught.
CODEX_SUBMIT_VERIFY_POLL_ATTEMPTS = 4
# Interval between confirmation polls (seconds), for both the initial grace
# window and the post-re-Enter re-verification.
CODEX_SUBMIT_VERIFY_POLL_INTERVAL_SECONDS = 0.5
# BLOCKERS 1/2/3 (r4): the durable submission boundary is the pasted task
# echoed as a SUBMITTED user turn in scrollback (its collapsed chip, or its raw
# text). For the raw-text case we match a distinctive normalized PREFIX of the
# task so pane soft-wrap / truncation does not defeat the check while an
# unrelated short line cannot coincidentally match. Messages shorter than the
# minimum are matched only via the unambiguous chip echo (and secondary
# spinner), never by raw text.
CODEX_SUBMIT_TASK_SIGNATURE_MIN_CHARS = 12
CODEX_SUBMIT_TASK_SIGNATURE_CHARS = 40
# r5 (BLOCKERS 1/2/3 root cause: no DISPATCH-RELATIVE boundary). r4's
# submission predicate scanned ALL captured history with no pre-send cursor, so
# any historical paste chip / identical task / identical 40-char prefix
# false-confirmed the CURRENT unsent chip (B1); wrapped continuations were
# discarded and short tasks produced no signature (B2); and a fixed 200-row tail
# could evict the evidence while an unrelated active draft got recovery Enters
# (B3). r5 makes the evidence dispatch-relative by BASELINE DIFF: immediately
# BEFORE the paste/submit we capture a baseline observation of the pane (the set
# of submitted-user-turn fingerprints already present, plus a turn-count
# watermark). Submission evidence is then a NEW submitted-turn artifact that was
# ABSENT from that baseline. Historical collisions live in the baseline and are
# excluded BY CONSTRUCTION (kills B1); wrap is normalized before fingerprinting
# (B2); and a watermark that shows scrollback advanced/evicted past the capture
# window is INDETERMINATE → bounded → defer, never a blind Enter into a busy
# pane (B3).
#
# Full-history capture rows for the baseline so eviction of the *baseline* set
# is itself observable as a shrunk watermark rather than a silent miss.
CODEX_SUBMIT_BASELINE_TAIL_LINES = 500

# ---------------------------------------------------------------------------
# r6 STRUCTURAL ROLLOUT SIGNAL — replaces pane heuristics as PRIMARY signal.
# ---------------------------------------------------------------------------
# Maximum time (seconds) to poll the rollout JSONL for the user-event record.
# This bounds the total verify window. The rollout write is typically flushed
# within 1–2s of the submit landing in Codex, but can lag under load.
CODEX_ROLLOUT_POLL_TIMEOUT_SECONDS = 12.0
# Interval between rollout file polls (seconds).
CODEX_ROLLOUT_POLL_INTERVAL_SECONDS = 0.3
# Maximum time to wait for the rollout file to be created (fresh session start
# where the file may not exist at dispatch time yet).
CODEX_ROLLOUT_CREATION_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class CodexSubmitBaseline:
    """A dispatch-relative snapshot taken BEFORE the paste/submit.

    r6 (DIRECTION CHANGE): the PRIMARY confirmation signal is now STRUCTURAL —
    the Codex session rollout JSONL file gains a user-turn record whose content
    matches the dispatched message. Pane-content heuristics are retained ONLY as
    a fast-path early-exit hint (if pane clearly shows submission, skip waiting
    for the rollout write to flush). Correctness NEVER depends on pane content.

    Fields:
      * ``rollout_path`` — resolved Path to the session's rollout JSONL file at
        baseline time.  ``None`` if the file could not be resolved (poll for
        creation during verify).
      * ``rollout_offset`` — byte offset at end of the rollout file at baseline
        time.  New user events from this dispatch will appear AFTER this offset.
        A dispatch-relative cursor: any record written after this point is new.
      * ``turn_fingerprints`` — (legacy hint) normalized pane turn fingerprints.
      * ``chip_counter`` — (legacy hint) chip multiset.
      * ``turn_count`` — (legacy hint) submitted turn count watermark.
      * ``captured_ok`` — (legacy hint) False if pane baseline capture failed.
    """

    rollout_path: "Path | None" = None
    rollout_offset: int = 0
    turn_fingerprints: frozenset[str] = field(default_factory=frozenset)
    chip_counter: tuple[tuple[str, int], ...] = ()
    turn_count: int = 0
    captured_ok: bool = False
    # B2 r7: active composer chip count at pre-paste time. None means no chip
    # was visible on the composer (or capture failed). Used to detect ambiguous
    # same-length drafts: if pre-paste composer already holds a draft whose
    # length is within ±1 of the dispatched message, ownership is unresolvable.
    pre_paste_chip_count: "int | None" = None

    def chip_count(self, chip: str) -> int:
        for key, count in self.chip_counter:
            if key == chip:
                return count
        return 0


class CodexSubmitStuckError(Exception):
    """Raised when a codex composer never submits a pasted task after retries.

    The send seams (``terminal_service.send_input`` /
    ``send_prepared_input``) raise this from INSIDE the dispatch transaction so
    it drives ``abort_dispatch`` — a coherent rollback, not a half-committed
    send (BLOCKER B2). Those seams then translate it into a
    ``DeliveryDeferredError`` so the inbox delivery layer treats it as a
    retry-safe deferred delivery rather than a hard crash or a pretend-success.
    Defined as a plain ``Exception`` here (not a ``DeliveryDeferredError``
    subclass) to avoid a provider→draft_guard→status_monitor→manager→provider
    import cycle; the translation lives in the seam, which already imports
    ``DeliveryDeferredError``.
    """


def _apply_sgr_params_to_dim(params: str, dim: bool) -> bool:
    """Update dim/faint state from one SGR parameter list.

    Walks codes left-to-right. Extended colour payloads after 38/48 are
    consumed so their sub-parameters cannot be mistaken for intensity codes:
    ``38;5;N`` (256-colour) skips N; ``38;2;R;G;B`` (truecolour) skips R,G,B.
    Only a standalone intensity ``2`` sets dim; ``22`` clears it; ``0``/empty
    resets all attributes (dim off).
    """
    if params == "":
        return False
    parts = params.split(";")
    idx = 0
    while idx < len(parts):
        code = parts[idx]
        idx += 1
        if code == "" or code == "0":
            dim = False
            continue
        if code in ("38", "48"):
            # Select graphic rendition extended colour: next token is mode.
            if idx >= len(parts):
                break
            mode = parts[idx]
            idx += 1
            if mode == "5":
                # 256-colour: one colour index follows.
                idx += 1
            elif mode == "2":
                # Truecolour: three RGB components follow.
                idx += 3
            # Unknown mode: stop consuming; remaining tokens are not intensity.
            continue
        if code == "2":
            dim = True
            continue
        if code == "22":
            dim = False
            continue
        # Other SGR codes (bold, italic, plain colours, …) leave dim as-is.
    return dim


def _composer_body_is_dim_ghost(raw_body: str) -> bool:
    """Return True when composer body text is entirely dim/faint (SGR 2).

    Empirical (codex 0.143 capture-pane -e): ghost suggestions render as
    ``\\x1b[1m›\\x1b[0m \\x1b[2mHINT\\x1b[0m`` (sometimes with a bg SGR before
    dim). Typed drafts have no dim on the body text. pyte drops dim, so this
    only works on escape-preserving captures.

    Truecolour / 256-colour SGR (``38;2;…`` / ``38;5;…``) must not be read as
    dim: the ``2`` after ``38`` is a colour-space selector, not intensity.
    """
    dim = False
    saw_text = False
    saw_undimmed = False
    i = 0
    n = len(raw_body)
    while i < n:
        if raw_body[i] == "\x1b":
            m = _SGR_CSI_RE.match(raw_body, i)
            if m:
                dim = _apply_sgr_params_to_dim(m.group(1), dim)
                i = m.end()
                continue
            # Non-SGR CSI/OSC: skip to final byte or drop the ESC.
            if i + 1 < n and raw_body[i + 1] == "[":
                j = i + 2
                while j < n and not ("A" <= raw_body[j] <= "Z" or "a" <= raw_body[j] <= "z"):
                    j += 1
                i = j + 1 if j < n else n
                continue
            i += 1
            continue
        ch = raw_body[i]
        if not ch.isspace():
            saw_text = True
            if not dim:
                saw_undimmed = True
        i += 1
    return saw_text and not saw_undimmed


def _compute_tui_footer_cutoff(all_lines: list[str]) -> int:
    """Compute the character position where the TUI footer area starts.

    Scans backward from the last line to find the TUI footer status bar
    (matches TUI_FOOTER_PATTERN), then continues upward to include any
    blank lines and the suggestion hint line (› with text) that appear
    above the status bar as part of the footer area.

    Returns the character position in the joined text (``'\\n'.join(all_lines)``)
    where the footer starts. Returns ``len('\\n'.join(all_lines))`` if no
    footer is found.
    """
    n = len(all_lines)
    last_nonempty = next(
        (i for i in range(n - 1, -1, -1) if all_lines[i].strip()),
        -1,
    )
    footer_start_idx = _find_tui_footer_index(all_lines)

    if footer_start_idx is None:
        # A five-hour-only config can render no status row at all. In that
        # case, recognize Codex's dim suggestion text as chrome so it cannot
        # become a false user message in status or extraction parsing.
        if last_nonempty >= 0 and _is_known_composer_placeholder(all_lines[last_nonempty]):
            footer_start_idx = last_nonempty
        else:
            return len("\n".join(all_lines))

    # Scan upward from the status bar to include both chrome rows when present:
    # the shortcuts hint and the suggestion prompt. The old walk stopped at
    # the shortcuts row, leaving a ghost prompt above the cutoff.
    for j in range(footer_start_idx - 1, max(footer_start_idx - 7, -1), -1):
        line = all_lines[j]
        if not line.strip():
            footer_start_idx = j
        elif re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line):
            footer_start_idx = j
        elif re.search(r"\?\s+for shortcuts", line, re.IGNORECASE):
            footer_start_idx = j
        else:
            break

    return len("\n".join(all_lines[:footer_start_idx]))


def _tui_footer_candidate_strength(line: str) -> str | None:
    """Return ``strong``/``weak`` for a whole-row footer candidate."""
    clean = strip_terminal_escapes(line).strip()
    if (
        not clean
        or len(clean) > 240
        or "›" in clean
        or re.match(ASSISTANT_PREFIX_PATTERN, clean, re.IGNORECASE)
        or re.match(USER_PREFIX_PATTERN, clean, re.IGNORECASE)
    ):
        return None
    if re.fullmatch(r"\?\s+for shortcuts(?:\s+.*)?", clean, re.IGNORECASE):
        return "strong"
    if re.fullmatch(TUI_FOOTER_PATTERN, clean):
        legacy_path_last = bool(re.search(r"·\s+[~/]", clean))
        return "strong" if legacy_path_last else "weak"
    # Anchor multi-segment status rows from the known right-hand grammar. Do
    # not split on middle dots: paths and branch names may contain them.
    if _TUI_PATH_WITH_KNOWN_TAIL_RE.fullmatch(clean):
        return "strong"
    if _TUI_PATH_BRANCH_ONLY_RE.fullmatch(clean):
        return "strong"
    if _TUI_PATH_TRUNCATED_RE.fullmatch(clean):
        return "weak"
    if _TUI_PATH_ONLY_RE.fullmatch(clean):
        return "weak"
    return None


def _find_composer_anchor_index(all_lines: list[str], footer_idx: int) -> int | None:
    """Return the composer row corroborating a footer candidate, if any."""
    saw_blank = False
    saw_shortcuts = False
    lower_bound = max(0, footer_idx - IDLE_PROMPT_TAIL_LINES)
    for index in range(footer_idx - 1, lower_bound - 1, -1):
        clean = strip_terminal_escapes(all_lines[index]).strip()
        if not clean:
            saw_blank = True
            continue
        if re.fullmatch(r"\?\s+for shortcuts(?:\s+.*)?", clean, re.IGNORECASE):
            saw_shortcuts = True
            continue
        if re.match(rf"{IDLE_PROMPT_SCREEN_PATTERN}", clean, re.IGNORECASE):
            if re.fullmatch(IDLE_PROMPT_STRICT_PATTERN, clean, re.IGNORECASE) is not None:
                return index
            # Non-empty composer rows are content-shaped. This adjacency is
            # sufficient for semantic status, while extraction and draft
            # handling apply their own conservative ambiguity policies.
            previous_is_boundary = (
                index == 0 or not strip_terminal_escapes(all_lines[index - 1]).strip()
            )
            return index if saw_shortcuts or (saw_blank and previous_is_boundary) else None
        # Every candidate strength uses the same adjacency rule: arbitrary
        # assistant/user content between the composer and footer rejects it.
        return None
    return None


def _has_composer_anchor(all_lines: list[str], footer_idx: int) -> bool:
    """Tie a footer candidate to adjacent, corroborated composer chrome."""
    return _find_composer_anchor_index(all_lines, footer_idx) is not None


def _find_ambiguous_footer_region(
    all_lines: list[str], *, minimum_prompt_index: int = 0
) -> tuple[int, int] | None:
    """Find content-shaped composer/footer rows below assistant output.

    Such rows are useful semantic chrome evidence, but rendered cells cannot
    prove whether Codex or the assistant owns them. Extraction therefore uses
    this only to preserve the whole ambiguous region, never as a cutoff.
    """
    for footer_idx, line in enumerate(all_lines):
        if _tui_footer_candidate_strength(line) is None:
            continue
        prompt_idx = _find_composer_anchor_index(all_lines, footer_idx)
        if prompt_idx is None or prompt_idx < minimum_prompt_index:
            continue
        prompt = strip_terminal_escapes(all_lines[prompt_idx]).strip()
        if re.fullmatch(IDLE_PROMPT_STRICT_PATTERN, prompt, re.IGNORECASE):
            # An empty prompt contributes no answer text and is safe to trim.
            continue
        if any(
            re.match(
                ASSISTANT_PREFIX_PATTERN,
                strip_terminal_escapes(candidate).strip(),
                re.IGNORECASE,
            )
            for candidate in all_lines[:prompt_idx]
        ):
            return prompt_idx, footer_idx
    return None


def _find_tui_footer_index(all_lines: list[str]) -> int | None:
    """Return the structurally anchored bottom footer row, if present."""
    last_nonempty = next(
        (i for i in range(len(all_lines) - 1, -1, -1) if all_lines[i].strip()),
        -1,
    )
    if last_nonempty < 0:
        return None
    strength = _tui_footer_candidate_strength(all_lines[last_nonempty])
    if strength is None:
        return None
    if not _has_composer_anchor(all_lines, last_nonempty):
        return None
    return last_nonempty


def _is_known_composer_placeholder(line: str) -> bool:
    """Recognize a Codex suggestion row after ANSI escapes are removed."""
    clean = strip_terminal_escapes(line).strip()
    match = re.fullmatch(r"(?:›|❯|codex>)\s*(.*)", clean)
    return match is not None and match.group(1) in CODEX_EMPTY_COMPOSER_PLACEHOLDERS


def _has_known_composer_placeholder_at_bottom(all_lines: list[str]) -> bool:
    last_nonempty = next(
        (i for i in range(len(all_lines) - 1, -1, -1) if all_lines[i].strip()),
        -1,
    )
    return last_nonempty >= 0 and _is_known_composer_placeholder(all_lines[last_nonempty])


def _has_tui_footer_in_tail(all_lines: list[str]) -> bool:
    """Detect footer chrome only within the configured pane-tail window."""
    last_nonempty = next(
        (i for i in range(len(all_lines) - 1, -1, -1) if all_lines[i].strip()),
        -1,
    )
    if last_nonempty < 0:
        return False
    trailing_rows = len(all_lines) - last_nonempty - 1
    last_clean = strip_terminal_escapes(all_lines[last_nonempty]).strip()
    legacy_path_last = bool(re.search(r"·\s+[~/]", last_clean)) and not last_clean.startswith(
        ("/", "~")
    )
    if trailing_rows >= IDLE_PROMPT_TAIL_LINES and legacy_path_last:
        # Preserve the historical full-screen capture behavior: old path-last
        # fixtures with a large blank viewport tail did not activate cutoff.
        return False
    return _find_tui_footer_index(all_lines) is not None


def _toml_scalar(value: Any) -> str:
    """Serialize a Python scalar to a TOML literal for a ``-c key=<value>`` override.

    Strings become quoted TOML basic strings (backslash, quote, tab, CR, and newline escaped so
    tmux ``send_keys`` keeps the launch command on one line); bools become
    ``true``/``false``; ints and floats are emitted bare. Non-scalar values (dict/list/None) raise ``TypeError`` so a misconfigured profile fails fast. ``bool`` is checked
    before ``int`` because ``bool`` is a subclass of ``int`` in Python, so the
    order here is load-bearing — a flipped order would render ``True`` as ``1``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        raise TypeError(
            "codexConfig values must be scalars (str, bool, int, or float); "
            f"got {type(value).__name__}"
        )
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


# codexConfig keys are dotted CONFIG PATHS ("features.fast_mode") — dots are
# the path separator and intentional. MCP server names and env keys are single
# TOML BARE KEYS: a dot there would silently create a NESTED table
# (mcp_servers.my.srv.command → mcp_servers['my']['srv'], not
# mcp_servers['my.srv']), so codex would never find the server.
_CODEX_CONFIG_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_CODEX_BARE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_config_key(key: Any, *, source: str, allow_dots: bool = False) -> str:
    """Validate a key that is interpolated into a Codex ``-c`` override path.

    Spaces, ``=``, quotes, or control characters are rejected so a
    misconfigured profile fails fast with a clear error instead of silently
    emitting a malformed ``-c`` override (an unescaped quote or newline in the
    KEY half would corrupt the TOML the same way an unescaped value would).

    ``allow_dots=True`` permits dotted config paths (codexConfig keys like
    ``features.fast_mode``). MCP server names and env keys must be single
    TOML bare keys: a dot there would nest the entry under the wrong TOML
    table (see pattern comment above). ``source`` names the profile field
    for the error message.
    """
    if allow_dots:
        pattern = _CODEX_CONFIG_KEY_PATTERN
        expected = "a dotted config path over [A-Za-z0-9_.-] (e.g. 'features.fast_mode')"
    else:
        pattern = _CODEX_BARE_KEY_PATTERN
        expected = (
            "a single TOML bare key over [A-Za-z0-9_-] (no dots -- a dot "
            "would nest the entry under the wrong TOML table)"
        )
    # fullmatch, not match: with ``$`` alone, re.match accepts a TRAILING
    # newline ("srv\n" passes ^...$), which is exactly the bug class this
    # validation exists to close.
    if not isinstance(key, str) or not pattern.fullmatch(key):
        raise ValueError(f"Invalid {source} key {key!r}: must be {expected}")
    return key


def _toml_override(key: str, value: Any) -> str:
    """Build one ``key=<toml-scalar>`` Codex ``-c`` override, validating the key.

    Key validation is delegated to :func:`_validate_config_key`.
    Value-serialization failures from :func:`_toml_scalar` are re-raised with
    the offending key for context.
    """
    _validate_config_key(key, source="codexConfig", allow_dots=True)
    try:
        return f"{key}={_toml_scalar(value)}"
    except TypeError as exc:
        raise TypeError(f"codexConfig key '{key}': {exc}") from exc


def _resolved_codex_profile_config(
    profile: Any, profile_name: str | None = None
) -> tuple[str | None, dict[str, Any]]:
    """Single model/config resolver shared by interactive and seed launches."""
    defaults = get_provider_defaults("codex")
    declared_name = getattr(profile, "name", None) if profile is not None else None
    resolved_profile_name = (
        declared_name if isinstance(declared_name, str) and declared_name else profile_name
    )
    profile_defaults = get_provider_profile_defaults(defaults, resolved_profile_name)
    model = resolve_provider_string_option(profile_defaults, defaults, profile, "model", "model")
    config = dict(getattr(profile, "codexConfig", None) or {})
    effort = None
    effort_configured = False
    for layer in (profile_defaults, defaults):
        candidate = layer.get("reasoning_effort")
        if "reasoning_effort" in layer and isinstance(candidate, str):
            effort = candidate
            effort_configured = True
            break
    if effort_configured:
        if effort:
            config["model_reasoning_effort"] = effort
        else:
            config.pop("model_reasoning_effort", None)
    return model, config


def _has_update_dialog_in_bottom(clean_output: str) -> bool:
    """Return True for an ordered update-menu block in the bottom region."""
    expected = 0
    border_chars = frozenset("╭╮╰╯│─┌┐└┘├┤┬┴┼")
    for raw_row in clean_output.splitlines()[-STARTUP_PROMPT_BOTTOM_LINES:]:
        row = raw_row.strip()
        if len(row) >= 2 and row.startswith("│") and row.endswith("│"):
            row = row[1:-1].strip()
        if not row or set(row) <= border_chars:
            continue
        if _UPDATE_DIALOG_ROW_PATTERNS[expected].fullmatch(row):
            expected += 1
            if expected == len(_UPDATE_DIALOG_ROW_PATTERNS):
                return True
            continue
        expected = 1 if _UPDATE_DIALOG_ROW_PATTERNS[0].fullmatch(row) else 0
    return False


def _modal_line_content(line: str) -> Optional[str]:
    """Reduce one line to its modal text, or None if the line reads as prose.

    Strips frame glyphs and padding so ``"│ [a] Accept  [d] Decline   │"``
    reduces to ``"[a] Accept  [d] Decline"``. Returns None when the leading run
    removed was whitespace ONLY while being non-empty — i.e. the line is
    indented plain text.

    That indent test is the discriminator against the model quoting a modal
    transcript back in its own reply:

        • The terminal output showed:
            Command Approval Required
            [a] Accept  [d] Decline
          so it was waiting on approval.

    Those quoted lines reproduce the modal's per-line structure exactly, so
    line structure alone cannot separate them. Position can: Codex draws the
    modal box flush at the left margin, whereas quoted or continuation prose is
    indented under its bullet. So a leading run of frame glyphs is accepted, a
    leading run of spaces/tabs is not, and column 0 is accepted either way
    (an unframed modal would still start there).
    """
    content = line.strip(MODAL_FRAME_CHARS)
    if not content:
        return None
    lead = line[: len(line) - len(line.lstrip(MODAL_FRAME_CHARS))]
    if lead and not (MODAL_FRAME_GLYPHS & set(lead)):
        return None
    return content


def _is_frame_padding(line: str) -> bool:
    """Return True when ``line`` carries nothing but frame glyphs and padding.

    True of a box's top/bottom rule ("╰────╯"), of an empty interior row
    ("│      │"), and of a blank line (space is in ``MODAL_FRAME_CHARS``).
    """
    return not line.strip(MODAL_FRAME_CHARS)


def _is_chrome_only(line: str) -> bool:
    """Return True when ``line`` is frame or TUI chrome rather than content.

    The union of what may legitimately sit BELOW a live modal: the box's own
    closing rule and interior padding, blank filler, the empty composer line
    ("›" with nothing typed), and the status-bar footer. Anything else -- a
    prose bullet, a spinner, a typed draft -- is content, which means the
    modal is no longer the bottom of the pane.

    The empty-composer and footer cases are matched explicitly rather than
    folded into :func:`_is_frame_padding` because neither ``›`` nor the footer
    text reduces to empty under ``MODAL_FRAME_CHARS``.
    """
    if _is_frame_padding(line):
        return True
    if re.fullmatch(rf"\s*{IDLE_PROMPT_PATTERN}\s*", line):
        return True
    return re.search(TUI_FOOTER_PATTERN, line) is not None


def _is_transcript_marker(line: str) -> bool:
    """Return True when ``line`` opens a new transcript cell (``›`` user / ``•`` bullet).

    Used as the upward bound on the header search: Codex draws the modal as ONE
    cell, so a user line or an assistant bullet is a hard boundary that the box
    cannot span. This replaces a fixed line count, which could not express
    "same box" and therefore failed open on a modal taller than the window.
    """
    return bool(
        re.match(USER_PREFIX_PATTERN, line, re.IGNORECASE)
        or re.match(ASSISTANT_PREFIX_PATTERN, line, re.IGNORECASE)
    )


def _has_approval_modal_in_bottom(clean_output: str) -> bool:
    """Return True when Codex's boxed command-approval modal is active at the bottom.

    NOTE: this detects the LEGACY "Command Approval Required" / "[a] Accept"
    modal, which codex-cli 0.147.0 does not render — see
    APPROVAL_MODAL_HEADER_PATTERN's comment and
    :func:`_has_approval_prompt_in_bottom` for the copy that is live today.

    Anchored BOTTOM-UP on the last choice line, because the thing being tested
    is an invariant about the bottom of the pane, not about a region of it: a
    live modal blocks the TUI, so it must BE the bottom, with only frame rows
    and footer chrome after it. Four guards:

    1. **Anchor.** The LAST line that reduces to a choice key. Taking the last
       rather than the first is what lets an already-answered modal sitting in
       scrollback above a live one be ignored instead of vetoing it.
    2. **Nothing but chrome below the anchor.** See :func:`_is_chrome_only`.
       This subsumes the older spinner test (a spinner is not chrome) and also
       rejects a modal transcript the model quoted mid-reply, since the reply
       continues below the quote. It replaces "footer must NOT appear below",
       which would have false-negatived every real modal: with
       ``--no-alt-screen`` the footer renders at the bottom regardless.
    3. **Corroborating header above the anchor,** found by walking up and
       stopping at the first :func:`_is_transcript_marker` — the box is one
       transcript cell, so the header must be inside it. No fixed window, so an
       arbitrarily tall modal still resolves; previously a >15-line modal lost
       its header and failed open to COMPLETED.
    4. **Line structure and left-margin position.** Each half must own its line
       (header an exact match, choice line a prefix match) and sit at the box's
       margin rather than under a prose indent — see :func:`_modal_line_content`.

    Known residual: a framed modal quote that ENDS a reply, with only the empty
    composer and footer after it, satisfies all four guards and reads as live.
    Distinguishing it needs semantics this detector does not have; it costs a
    spurious WAITING_USER_ANSWER (work withheld) rather than a COMPLETED (work
    pasted into a blocked pane), which is the safe direction to be wrong in.
    """
    lines = clean_output.splitlines()

    choice_idx = None
    for index in range(len(lines) - 1, -1, -1):
        content = _modal_line_content(lines[index])
        if content is not None and re.match(APPROVAL_MODAL_CHOICE_PATTERN, content, re.IGNORECASE):
            choice_idx = index
            break
    if choice_idx is None:
        return False

    if not all(_is_chrome_only(line) for line in lines[choice_idx + 1 :]):
        return False

    for index in range(choice_idx - 1, -1, -1):
        line = lines[index]
        content = _modal_line_content(line)
        if content is not None and re.fullmatch(
            APPROVAL_MODAL_HEADER_PATTERN, content, re.IGNORECASE
        ):
            return True
        if _is_transcript_marker(line):
            return False
    return False


def _has_approval_prompt_in_bottom(clean_output: str) -> bool:
    """Return True when Codex's runtime approval prompt is active at the bottom.

    This is the prompt codex-cli 0.147.0 actually renders (verified against three
    live captures). Detection is STRUCTURAL and bottom-up — the numbered menu
    itself, not the question title and not the footer — because none of the
    alternatives can be relied on: the title list cannot be completed, a fixed
    row budget fails open on a long command preview (see
    APPROVAL_PROMPT_PATTERN), and the footer is absent entirely when the list
    actions are unbound (`tui.keymap.list.accept = []` renders the same blocking
    menu with only "Press esc to cancel", or with no footer line at all — see
    APPROVAL_PROMPT_FOOTER).

    Three guards, in the order they are cheapest to refute:

    1. **Menu-cursor anchor.** The LAST line whose selection cursor sits flush
       at column 0 (APPROVAL_MENU_CURSOR_PATTERN). Taking the last, not the
       first, lets an already-answered prompt in scrollback be ignored rather
       than shadow a live one below it. Column 0 is where Codex draws every
       transcript gutter marker, so quoted or continuation prose — a menu the
       model merely PASTED into a reply — carries its cursor at column >= 2 and
       fails this.
    2. **Nothing below the anchor but the rest of the menu block**: the
       remaining (non-cursor) option rows — each an option-start row plus any
       wrapped continuation rows at the option text column
       (APPROVAL_MENU_CONTINUATION_PATTERN; the renderer wraps long options by
       default, so a narrow pane splits one option across lines) — at most one
       footer hint in any of its rendered forms, and chrome
       (:func:`_is_chrome_only`, shared with the boxed-modal detector).
       Continuations are honoured only while inside an option; indented prose
       after the footer or after blank filler still disqualifies. A live
       prompt blocks the TUI, so its menu must BE the bottom of the pane. This
       is the guard that rejects an ordinary COMPLETED reply which quotes the
       menu while a live composer and more prose sit underneath — the sticky
       WAITING_USER_ANSWER that case used to latch would wedge a ready worker.
    3. **Menu size.** At least APPROVAL_MENU_MIN_OPTIONS option-START rows
       counted contiguously around the anchor, stepping over wrapped
       continuation rows (a genuine approval always offers accept and
       decline). Contiguity matters: counting across the question or the
       command preview would let a numbered list INSIDE a quoted command
       inflate the tally.

    Deliberately NOT required: any particular question title, any particular
    option copy, and the footer. The footer, when present in any of its four
    rendered shapes, is accepted as part of the block; its absence proves
    nothing because unbinding the accept action removes it while the menu still
    blocks. Firing on Codex's other blocking numbered menus (the model picker,
    for one) is correct rather than tolerated — those panes are equally blocked
    on a keystroke, and WAITING_USER_ANSWER is the right answer for them too.

    Residual risks, disclosed:

    - The column-0 cursor test is what separates a live menu from one pasted
      into a reply, so a future renderer that indents the cursor would fail
      open to IDLE. The live captures in test/providers/fixtures/
      (codex_approval_{modal,edits,long_preview}_raw.txt) pin the current
      rendering against that.
    - A USER message that is itself a numbered list ("1. foo\\n2. bar") renders
      with the same column-0 gutter marker ("› 1. foo" over "  2. bar"), so if
      it is the last transcript cell with only the idle composer below — codex
      interrupted before replying, say — it now reads WAITING rather than IDLE.
      That errs toward withholding work, never toward pasting into a blocked
      pane, and clears as soon as codex renders any activity below the cell.
      The footer-anchored version rejected this shape, but only by failing open
      to IDLE on every unbound-keymap approval, which is the dangerous
      direction to be wrong in.
    """
    lines = clean_output.splitlines()

    anchor = None
    for index in range(len(lines) - 1, -1, -1):
        if re.match(APPROVAL_MENU_CURSOR_PATTERN, lines[index]):
            anchor = index
            break
    if anchor is None:
        return False

    options = 1  # the anchor row
    footer_seen = False
    in_option = True  # the anchor row itself may wrap onto the next line
    for line in lines[anchor + 1 :]:
        if not footer_seen and re.match(APPROVAL_MENU_OPTION_PATTERN, line):
            options += 1
            in_option = True
            continue
        if in_option and re.match(APPROVAL_MENU_CONTINUATION_PATTERN, line):
            continue
        if not footer_seen and re.search(APPROVAL_PROMPT_FOOTER, line):
            footer_seen = True
            in_option = False
            continue
        if _is_chrome_only(line):
            in_option = False
            continue
        return False

    for index in range(anchor - 1, -1, -1):
        line = lines[index]
        if re.match(APPROVAL_MENU_OPTION_PATTERN, line):
            options += 1
            continue
        if re.match(APPROVAL_MENU_CONTINUATION_PATTERN, line):
            # Walking up, a continuation belongs to the option row above it;
            # step over it and let that row (or anything else) decide.
            continue
        break

    return options >= APPROVAL_MENU_MIN_OPTIONS


def _has_startup_idle_composer(clean_output: str) -> bool:
    """Return True when the bottom of the pane shows Codex's idle composer."""
    all_lines = clean_output.splitlines()
    tail_lines = all_lines[-STARTUP_PROMPT_BOTTOM_LINES:]
    tail_output = "\n".join(tail_lines)

    # Filter out known informational MCP messages before activity check
    active_lines = [line for line in tail_lines if not _MCP_INTERRUPT_PATTERN.search(line)]
    active_tail = "\n".join(active_lines)

    if re.search(STARTUP_ACTIVITY_PATTERN, active_tail, re.MULTILINE):
        return False
    if re.search(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
        return False
    if re.search(STARTUP_BLOCKING_INPUT_PATTERN, tail_output, re.IGNORECASE):
        return False

    legacy_tail = all_lines[-IDLE_PROMPT_TAIL_LINES:]
    if any(re.match(IDLE_PROMPT_STRICT_PATTERN, line) for line in legacy_tail):
        return True

    # Codex 0.145 renders placeholder text inside the idle composer instead of
    # an empty prompt. Match only known placeholder copy and require its status
    # footer below it so typed drafts and ordinary output are not treated as ready.
    for index in range(len(tail_lines) - 1, -1, -1):
        if re.match(STARTUP_IDLE_PLACEHOLDER_PATTERN, tail_lines[index]):
            return any(re.search(STARTUP_FOOTER_PATTERN, line) for line in tail_lines[index + 1 :])
    return False


def _find_assistant_marker(text: str) -> Optional[re.Match[str]]:
    """Find the first ASSISTANT_PREFIX_PATTERN match in ``text`` whose line
    is not an MCP tool-call marker.

    Codex emits ``• Called <server>.<tool>(...)`` when invoking an MCP tool;
    that bullet matches ASSISTANT_PREFIX_PATTERN but is followed by tool
    output, not the model's reply. Anchoring on it would conflate tool
    output with the model response (status: false COMPLETED;
    extraction: skill-body leak).
    """
    for m in re.finditer(ASSISTANT_PREFIX_PATTERN, text, re.IGNORECASE | re.MULTILINE):
        line_end = text.find("\n", m.start())
        if line_end == -1:
            line_end = len(text)
        line = text[m.start() : line_end]
        if re.match(MCP_TOOL_CALL_PATTERN, line):
            continue
        if re.match(SYSTEM_NOTICE_PATTERN, line):
            continue
        return m
    return None


def _find_response_marker(text: str) -> Optional[re.Match[str]]:
    """Find the first model-reply marker after a structural activity prelude.

    Native Codex activity cells have a ``•`` summary followed by a ``└`` tree
    continuation.  Require at least two complete cells before advancing the
    response boundary: a single tree-formatted group may be a legitimate
    answer, while two consecutive cells are strong evidence of TUI activity.
    Compact bullet groups remain ambiguous and are preserved.  This trades a
    rare false positive for avoiding silent truncation of ordinary replies and
    deliberately avoids matching English verbs such as ``Read`` or ``Called``.
    """

    def line_end(start: int) -> int:
        newline = text.find("\n", start)
        return len(text) if newline == -1 else newline

    matches = []
    for match in re.finditer(ASSISTANT_PREFIX_PATTERN, text, re.IGNORECASE | re.MULTILINE):
        if not re.match(MCP_TOOL_CALL_PATTERN, text[match.start() : line_end(match.start())]):
            matches.append(match)

    if not matches:
        return None

    complete_cells = []
    prose_start = None
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        cell_tail = text[line_end(match.start()) : next_start]
        continuation = re.search(r"^[^\S\n]*└[^\n]*(?:\n|$)", cell_tail, re.MULTILINE)
        contains_mcp_call = re.search(MCP_TOOL_CALL_PATTERN, cell_tail, re.MULTILINE)
        if continuation and not contains_mcp_call:
            complete_cells.append(index)
            if index == len(matches) - 1:
                remaining = cell_tail[continuation.end() :]
                separator = re.search(r"^[^\S\n]*\n", remaining, re.MULTILINE)
                following = re.search(r"\S", remaining[separator.end() :]) if separator else None
                if separator and following:
                    candidate = (
                        line_end(match.start())
                        + continuation.end()
                        + separator.end()
                        + following.start()
                    )
                    if text[candidate] != "›":
                        prose_start = candidate

    if len(complete_cells) >= 2:
        last_cell = complete_cells[-1]
        if last_cell + 1 < len(matches):
            return matches[last_cell + 1]
        if prose_start is not None:
            return re.compile("").match(text, prose_start)

    return matches[0]


class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


class CodexProvider(BaseProvider):
    supports_fork_context = True
    supports_seed_resume_identity = True
    supports_reauth_rebind = True

    def capture_shell_baseline(self) -> str | None:
        """Capture through this module's backend seam before Codex starts."""
        return get_backend().get_pane_current_command(self.session_name, self.window_name)

    """Provider for Codex CLI tool integration."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
        skill_prompt: Optional[str] = None,
        fork_context: Optional[ForkContext] = None,
        persona_plan: Optional["PersonaPlan"] = None,
        model: Optional[str] = None,
    ):
        """Initialize provider state."""
        super().__init__(
            terminal_id, session_name, window_name, allowed_tools, skill_prompt, fork_context
        )
        self._initialized = False
        self._agent_profile = agent_profile
        self._persona_plan = persona_plan
        # Explicit per-call override for the existing profile/providers.toml
        # model chain, see _build_codex_command.
        self._model = model

    @classmethod
    def seed_resume_identity(cls, cwd: str, agent_profile: str) -> str:
        """Create and validate a native Codex rollout without CAO coordinates."""
        profile = load_agent_profile(agent_profile)
        argv = [resolve_provider_binary("codex"), "exec", "--skip-git-repo-check", "-C", cwd]
        model, config = _resolved_codex_profile_config(profile, agent_profile)
        if isinstance(model, str) and model:
            argv.extend(["--model", model])
        for key, value in config.items():
            argv.extend(["-c", _toml_override(key, value)])
        argv.append("Reply exactly: SEED_OK then stop.")
        logger.info(f"codex seed_resume_identity: starting seed for {agent_profile} in {cwd}")
        try:
            completed = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=90,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error(f"codex seed_resume_identity: TIMEOUT after 90s for {agent_profile}")
            raise RuntimeError("seed_timeout") from exc
        except OSError as exc:
            logger.error(f"codex seed_resume_identity: exec failed: {exc}")
            raise RuntimeError("seed_exec_failed") from exc
        logger.info(f"codex seed_resume_identity: completed rc={completed.returncode}")
        if completed.returncode != 0:
            raise RuntimeError("seed_exec_failed")
        matches: set[str] = set(
            re.findall(
                r"(?im)^\s*session id:\s*([0-9a-f]{8}-[0-9a-f-]{27,})\s*$",
                completed.stdout or "",
            )
        )
        if len(matches) != 1:
            raise RuntimeError("seed_uuid_unparseable")
        session_uuid: str = next(iter(matches))
        validator = cls("seed", "seed", "seed", agent_profile)
        try:
            validator.validate_session_artifact(session_uuid, cwd)
        except Exception as exc:
            raise RuntimeError("seed_artifact_invalid") from exc
        return str(session_uuid)

    def _developer_instructions_file_path(self) -> Path:
        """Path of this terminal's developer_instructions temp file.

        Single source of truth for the path -- both `_build_codex_command` (which
        writes it) and `cleanup` (which removes it) call this instead of each
        re-deriving the same path independently.
        """
        return CAO_HOME_DIR / "tmp" / f"{self.terminal_id}.codex_developer_instructions"

    def _build_codex_command(self) -> str:
        """Build Codex command with agent profile if provided.

        Returns properly escaped shell command string that can be safely sent via tmux.
        Uses codex's -c developer_instructions flag to inject agent system prompts.
        """
        # --yolo (alias for --dangerously-bypass-approvals-and-sandbox)
        # is the default because CAO runs codex non-interactively in tmux
        # where approval prompts would block handoff/assign. Profiles can
        # opt out via `codexProfile` (names a [profiles.<name>] block in
        # ~/.codex/config.toml), unless unrestricted allowed tools are enabled.
        # In practice, allowed_tools containing "*" is treated as yolo mode
        # and overrides codexProfile in the same way as an explicit yolo launch.
        yolo = bool(self._allowed_tools and "*" in self._allowed_tools)

        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        if profile and profile.codexProfile and not yolo:
            command_parts: list[str] = [
                resolve_provider_binary("codex"),
                "--profile",
                profile.codexProfile,
            ]
        else:
            command_parts = [resolve_provider_binary("codex"), "--yolo"]
        command_parts.extend(["--no-alt-screen", "--disable", "shell_snapshot"])

        model, codex_config = _resolved_codex_profile_config(profile, self._agent_profile)
        resolved_model = self._model if self._model is not None else model
        self._resolved_model = resolved_model if resolved_model else None
        if resolved_model:
            command_parts.extend(["--model", resolved_model])

        # Set below, only when there is a non-empty system_prompt to inject -- appended, raw and
        # deliberately unquoted by shlex, after the shlex.join() of everything else at the very
        # end of this method. See the long comment at its assignment site for why.
        developer_instructions_fragment: Optional[str] = None

        if profile is not None:
            system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
            system_prompt = self._apply_skill_prompt(system_prompt)
            if self._persona_plan is not None and self._persona_plan.memory_instructions:
                persona_memory = self._persona_plan.memory_instructions.rstrip()
                system_prompt = (
                    f"{system_prompt.rstrip()}\n\n{persona_memory}"
                    if system_prompt
                    else persona_memory
                )

            # Prepend security constraints for soft enforcement (Codex has no
            # native tool restriction mechanism). Only applied when tool
            # restrictions are active (not unrestricted "*").
            if self._allowed_tools and "*" not in self._allowed_tools:
                from cli_agent_orchestrator.constants import SECURITY_PROMPT

                tools_list = ", ".join(self._allowed_tools)
                tool_constraint = f"\nYou only have access to these tools: {tools_list}\n"
                system_prompt = SECURITY_PROMPT + tool_constraint + system_prompt

            if system_prompt:
                # Codex accepts developer_instructions via -c config override.
                # This is injected as a developer role message before AGENTS.md content.
                # Escape backslashes, double quotes, and newlines for TOML basic string.
                # Newlines must become literal \n to prevent tmux send_keys from
                # splitting the command across multiple lines.
                #
                # The escaped value is written to a CAO-owned temp file and referenced via a
                # shell command substitution ($(cat <file>)) instead of being inlined directly,
                # so the LAUNCH LINE ITSELF (what actually gets typed/pasted into the tmux pane)
                # stays short regardless of how long the instructions text is. A real profile
                # combining a security preamble, the caller's own system prompt, and the full
                # skill-list prompt (see _apply_skill_prompt) commonly produces several KB of
                # escaped text -- observed live at 8+KB. At launch time the pane is still a bare
                # shell (codex has not started yet), which correctly does not get bracketed-paste
                # framing (see clients/tmux.py's BRACKETED_PASTE_INCOMPATIBLE_SHELLS) since a bare
                # shell does not understand those escape sequences. But WITHOUT that framing, a
                # single pasted/typed line longer than the tty's canonical-mode line-length limit
                # (MAX_CANON, 4096 bytes on Linux) is silently truncated/dropped by the kernel's
                # tty line discipline before the shell ever sees a complete, valid command --
                # this manifests as the shell hanging at an unclosed-quote continuation prompt
                # forever (confirmed live: zero codex process ever spawned under the pane's shell,
                # even after an explicit trailing Enter), until CAO's own init-timeout eventually
                # fires with a generic "Codex initialization timed out" that gives no hint of the
                # real cause. $(cat <file>) is expanded internally by the shell BEFORE exec'ing
                # codex -- that internal expansion is not subject to the tty's per-line INPUT
                # limit at all, only the typed/pasted command line is. Wrapped in double quotes
                # (not left bare, not single-quoted) so the substitution still happens (command
                # substitution is disabled inside single quotes) while word-splitting/globbing of
                # the substituted content is suppressed (it is not inside single quotes either).
                # The file's own content is `_toml_scalar`'s output verbatim, already including
                # its own surrounding TOML double-quotes -- appended as a raw, deliberately
                # UNquoted-by-shlex fragment after the main shlex.join() below (shlex.join would
                # otherwise single-quote the whole "developer_instructions=$(cat ...)" fragment as
                # one opaque token, disabling the substitution it depends on).
                #
                # Same underlying instructions/skills length problem does not affect Claude Code
                # or Kimi CLI providers -- both already write the system prompt to a temp file and
                # pass a short file-path flag instead of inlining it (see claude_code.py's
                # --append-system-prompt-file, kimi_cli.py's system_prompt_path: YAML field).
                # Codex has no direct equivalent of that "arbitrary absolute path" flag (its only
                # file-loading mechanism, --profile, resolves names relative to $CODEX_HOME, which
                # this provider has no reliable way to resolve per-account from here) -- this
                # command-substitution approach reaches the same practical outcome (a short launch
                # line) without needing that.
                #
                # Deliberate, documented shell-scope trade-off (not an oversight): $(...) command
                # substitution is POSIX and works identically on every shell CAO's own
                # BRACKETED_PASTE_INCOMPATIBLE_SHELLS (constants.py) already tracks as a shell
                # class *except* csh/tcsh, which use `cmd` backticks instead and do not recognize
                # `$(` as substitution syntax at all -- launching codex from a pane whose bare
                # shell is csh/tcsh would break outright with this fragment malformed/rejected by
                # the shell, not merely degrade. bash/zsh/dash/sh/ksh/mksh/ash/fish are all fine.
                # No code here detects or special-cases the pane's shell before writing this
                # fragment (unlike BRACKETED_PASTE_INCOMPATIBLE_SHELLS' own runtime
                # #{pane_current_command} probe) -- csh/tcsh support, if ever needed, is scoped
                # out of this fix rather than silently assumed to already work.
                #
                # Not covering here (disclosed, not silently assumed away): the other -c overrides
                # below (per-MCP-server config, codexConfig) are NOT routed through this same
                # mechanism and remain inlined directly -- they are typically far smaller than
                # developer_instructions, but a profile configuring many MCP servers could in
                # theory still accumulate enough inline -c overrides to hit the same limit. Left
                # as a known, scoped-out follow-up rather than expanding this fix's surface.
                developer_instructions_file = self._developer_instructions_file_path()
                developer_instructions_file.parent.mkdir(parents=True, exist_ok=True)
                # Open with mode 0o600 baked into the O_CREAT call itself (rather than
                # write_text() followed by a separate chmod()) so the file is never
                # briefly world/group-readable between creation and permission-tightening --
                # the permissions are correct from the very first byte written.
                fd = os.open(
                    developer_instructions_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(_toml_scalar(system_prompt))
                developer_instructions_fragment = f'-c "developer_instructions=$(cat {shlex.quote(str(developer_instructions_file))})"'

            # Add MCP servers via -c config overrides (per-session, no global config changes).
            # Each server field is set via dotted path: mcp_servers.<name>.<field>=<value>
            if profile.mcpServers:
                for server_name, server_config in profile.mcpServers.items():
                    # Codex-only validation: the server name becomes part of
                    # the -c override PATH (a TOML dotted path), so it must be
                    # a single bare key — a quote/newline would corrupt the
                    # TOML and a dot would nest the server under the wrong
                    # table. Other providers write JSON configs where any
                    # string key is valid, so they don't need this.
                    _validate_config_key(server_name, source="mcpServers name")
                    prefix = f"mcp_servers.{server_name}"
                    if isinstance(server_config, dict):
                        cfg = dict(server_config)
                    else:
                        cfg = server_config.model_dump(exclude_none=True)
                    # Resolve the bundled cao-mcp-server console script to a
                    # PATH-independent invocation.
                    cfg = bind_mcp_server_identity(resolve_mcp_server_config(cfg), self.terminal_id)
                    if "command" in cfg:
                        command_parts.extend(
                            ["-c", f"{prefix}.command={_toml_scalar(cfg['command'])}"]
                        )
                    if "args" in cfg:
                        args_toml = "[" + ", ".join(_toml_scalar(a) for a in cfg["args"]) + "]"
                        command_parts.extend(["-c", f"{prefix}.args={args_toml}"])
                    if "env" in cfg and cfg["env"]:
                        for env_key, env_val in cfg["env"].items():
                            _validate_config_key(env_key, source="mcpServers env")
                            command_parts.extend(
                                ["-c", f"{prefix}.env.{env_key}={_toml_scalar(str(env_val))}"]
                            )
                    # Forward CAO_TERMINAL_ID so MCP servers (e.g. cao-mcp-server)
                    # can identify the current session for handoff/assign operations.
                    # Codex does not forward env vars to MCP subprocesses by default;
                    # env_vars lists names to inherit from the parent shell environment.
                    env_vars = cfg.get("env_vars", [])
                    if "CAO_TERMINAL_ID" not in env_vars:
                        env_vars = list(env_vars) + ["CAO_TERMINAL_ID"]
                    if "CAO_TERMINAL_TOKEN" not in env_vars:
                        env_vars = list(env_vars) + ["CAO_TERMINAL_TOKEN"]
                    env_vars_toml = "[" + ", ".join(_toml_scalar(v) for v in env_vars) + "]"
                    command_parts.extend(["-c", f"{prefix}.env_vars={env_vars_toml}"])
                    # Set a generous tool timeout for MCP calls like handoff, which
                    # create a new terminal, initialize the provider, send a message,
                    # wait for the agent to complete, and extract the output.
                    # Codex defaults to 60s which is too short for multi-step operations.
                    # Value MUST be a TOML float (600.0, not 600) because Codex
                    # deserializes tool_timeout_sec via Option<f64>; a TOML integer
                    # is silently rejected and falls back to the 60s default.
                    if "tool_timeout_sec" not in cfg:
                        command_parts.extend(["-c", f"{prefix}.tool_timeout_sec=600.0"])

            # Inline Codex config overrides (-c key=value). Lets a profile set
            # per-agent Codex knobs — reasoning effort, service tier, fast mode,
            # etc. — without editing the global ~/.codex/config.toml or
            # maintaining named profile files. Keys may be dotted config paths
            # (e.g. "features.fast_mode"); values are serialized to TOML
            # scalars. Emitted before providers.toml defaults so per-key TOML
            # settings can take precedence while other profile keys remain.
        for key, value in codex_config.items():
            command_parts.extend(["-c", _toml_override(key, value)])

        command_parts.extend(["-c", "features.multi_agent=false"])

        # Suppress the startup update dialog at the source. This follows all
        # profile overrides so it wins, but stays before a fork/resume UUID,
        # which Codex requires as the final positional argument.
        command_parts.extend(["-c", "check_for_update_on_startup=false"])

        if self._fork_context:
            mode = self._fork_context.mode
            command_prefix = ["codex", mode]
            command_rest = command_parts[1:]
            command_rest = [
                "--dangerously-bypass-approvals-and-sandbox" if x == "--yolo" else x
                for x in command_rest
            ]
            command_parts = command_prefix + command_rest + [self._fork_context.session_uuid]
        # Fragment stays AFTER fork/resume argv rewrite so shlex.join does not
        # single-quote the $(cat ...) substitution away (upstream 0e7b70bb).
        command = shlex.join(command_parts)
        if developer_instructions_fragment is not None:
            command = f"{command} {developer_instructions_fragment}"
        return command

    def build_fork_command(
        self, session_uuid: str, new_session_uuid: Optional[str] = None
    ) -> list[str]:
        old = self._fork_context
        self._fork_context = ForkContext(
            mode="fork",
            session_uuid=session_uuid,
            base_name="base",
            provider="codex",
            initial_preamble="",
        )
        try:
            return shlex.split(self._build_codex_command())
        finally:
            self._fork_context = old

    def build_resume_command(self, session_uuid: str) -> list[str]:
        old = self._fork_context
        self._fork_context = ForkContext(
            mode="resume",
            session_uuid=session_uuid,
            base_name="base",
            provider="codex",
            initial_preamble="",
        )
        try:
            return shlex.split(self._build_codex_command())
        finally:
            self._fork_context = old

    def capture_session_uuid(self, pane_pid: int, launch_time: float, cwd: str) -> str:
        from cli_agent_orchestrator.services.fork_context_service import capture_codex_uuid

        return capture_codex_uuid(pane_pid, launch_time, cwd, terminal_id=self.terminal_id)

    def resume_session_uuid(self) -> str | None:
        if self._fork_context is not None and self._fork_context.mode == "resume":
            return self._fork_context.session_uuid
        return None

    def validate_session_artifact(self, session_uuid: str, cwd: str) -> None:
        matches = list(
            (_resolved_codex_home(getattr(self, "terminal_id", None)) / "sessions").glob(
                f"**/rollout-*{session_uuid}*.jsonl"
            )
        )
        if not matches:
            raise RetryableArtifactValidation("session_artifact_missing")
        if len(matches) > 1:
            raise TerminalArtifactValidation("session_artifact_ambiguous")
        with matches[0].open(encoding="utf-8") as stream:
            first = json.loads(stream.readline())
        if (
            first.get("type") != "session_meta"
            or first.get("payload", {}).get("id") != session_uuid
        ):
            raise TerminalArtifactValidation("session_artifact_identity_invalid")

    # ------------------------------------------------------------------
    # F435 r6 — STRUCTURAL rollout confirmation helpers
    # ------------------------------------------------------------------

    def _resolve_rollout_file(self, session_uuid: str | None) -> Path | None:
        """Locate the rollout JSONL for the given session UUID.

        Returns the single matching path, or ``None`` if not (yet) resolvable.

        Resolution strategy:
          1. Exact UUID match (glob ``rollout-*{uuid}*.jsonl``) — unambiguous
             when exactly one file matches.
          2. Ambiguous multi-match: pin by NEWEST mtime (the most recently
             written rollout file for this UUID is the active session).
          3. No UUID (fresh session before capture): fall back to the resume
             seed (``self._fork_context.session_uuid`` when mode == resume), or
             the single newest rollout file in the sessions dir.

        Returns None without touching the filesystem/DB when no UUID is
        available and no resume seed exists (test/DB-free environments).
        """
        # Early exit: no UUID and no resume seed → nothing to resolve.
        if not session_uuid:
            resume_uuid = self.resume_session_uuid()
            if not resume_uuid:
                return None

        try:
            sessions_dir = _resolved_codex_home(getattr(self, "terminal_id", None)) / "sessions"
        except Exception:
            return None
        if not sessions_dir.is_dir():
            return None

        # --- Try explicit UUID first ---
        if session_uuid:
            matches = list(sessions_dir.glob(f"**/rollout-*{session_uuid}*.jsonl"))
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                # B5 r7: identity-validate ambiguous candidates via session_meta.
                # mtime orders candidates, but identity decides.
                validated = self._identity_filter_rollout_candidates(
                    matches, session_uuid
                )
                if validated:
                    return validated
                # Fallback: mtime (best-effort, logged as ambiguous).
                logger.warning(
                    "F435 rollout-pin: %d candidates for UUID %s, none "
                    "passed identity validation; falling back to mtime",
                    len(matches),
                    session_uuid,
                )
                return max(matches, key=lambda p: p.stat().st_mtime)
            # No match — file not yet created; caller will poll.
            return None

        # --- No UUID: try resume seed ---
        resume_uuid = self.resume_session_uuid()
        if resume_uuid:
            matches = list(sessions_dir.glob(f"**/rollout-*{resume_uuid}*.jsonl"))
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                validated = self._identity_filter_rollout_candidates(
                    matches, resume_uuid
                )
                if validated:
                    return validated
                return max(matches, key=lambda p: p.stat().st_mtime)

        # --- Last resort: newest rollout file in sessions dir ---
        all_rollouts = list(sessions_dir.glob("**/rollout-*.jsonl"))
        if len(all_rollouts) == 1:
            return all_rollouts[0]
        if all_rollouts:
            return max(all_rollouts, key=lambda p: p.stat().st_mtime)

        return None

    @staticmethod
    def _identity_filter_rollout_candidates(
        candidates: list[Path], expected_id: str
    ) -> Path | None:
        """B5 r7: validate session_meta.payload.id against expected session id.

        Among multiple rollout files matching a UUID glob, select the one whose
        first-line session_meta record has ``payload.id == expected_id``.  If
        exactly one validates, return it.  If multiple validate (concurrent same
        session?), order by mtime.  If none validate, return None so the caller
        can fall back or log.
        """
        validated: list[Path] = []
        for path in candidates:
            try:
                with path.open("r", encoding="utf-8") as f:
                    first_line = f.readline()
                if not first_line:
                    continue
                record = json.loads(first_line)
                if (
                    record.get("type") == "session_meta"
                    and record.get("payload", {}).get("id") == expected_id
                ):
                    validated.append(path)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        if len(validated) == 1:
            return validated[0]
        if len(validated) > 1:
            return max(validated, key=lambda p: p.stat().st_mtime)
        return None

    @staticmethod
    def _rollout_file_offset(rollout_path: Path | None) -> int:
        """Return current end-of-file byte offset, or 0 if unresolvable."""
        if rollout_path is None:
            return 0
        try:
            return rollout_path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        """Collapse whitespace for content comparison (rollout vs dispatch)."""
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _rollout_has_user_event(
        cls,
        rollout_path: Path | None,
        offset: int,
        message: str,
    ) -> bool:
        """Check if the rollout JSONL has a user-turn record matching ``message``.

        Reads only COMPLETE lines (ending in newline) starting from ``offset``
        to handle partial-line writes safely. Matches by normalized content
        comparison (whitespace-collapsed equality or containment for long
        messages).

        Returns True if a matching user event is found after offset.
        """
        if rollout_path is None or not message:
            return False
        try:
            if not rollout_path.exists():
                return False
            file_size = rollout_path.stat().st_size
            if file_size <= offset:
                return False
            with rollout_path.open("r", encoding="utf-8") as f:
                f.seek(offset)
                raw = f.read()
        except OSError:
            return False

        # Only process complete lines (ending in \n) — a partial last line is
        # an in-progress write and must not be parsed.  If the raw chunk ends
        # with \n, every segment is complete; otherwise discard the trailing
        # fragment.
        lines = raw.split("\n")
        if not raw.endswith("\n"):
            lines = lines[:-1]  # drop incomplete trailing fragment
        norm_message = cls._normalize_for_match(message)
        # B4 r7: distinctive matching — require candidate length >= min(len(msg), 64)
        # AND prefix-equality (not substring containment).  This prevents a
        # 1-char event from confirming and prevents unrelated short candidates.
        min_distinctive_len = min(len(norm_message), 64)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            # Check all known Codex user-turn record formats:
            for candidate in cls._extract_rollout_user_texts(record):
                norm_candidate = cls._normalize_for_match(candidate)
                if norm_candidate == norm_message:
                    return True
                # B4 r7: distinctive prefix match for long messages that may be
                # truncated in the rollout.  Both candidate and message must be
                # of distinctive length, and the match is prefix-equality (not
                # arbitrary substring containment).
                if (
                    len(norm_candidate) >= min_distinctive_len
                    and len(norm_message) > 40
                ):
                    # Prefix equality: the shorter of (candidate[:200], message[:200])
                    # must equal the other's same-length prefix.
                    prefix_len = min(200, len(norm_candidate), len(norm_message))
                    if norm_candidate[:prefix_len] == norm_message[:prefix_len]:
                        return True
        return False

    @classmethod
    def _count_rollout_matches(
        cls,
        rollout_path: "Path | None",
        offset: int,
        message: str,
    ) -> int:
        """B3 r7: count ALL matching user-turn records after offset.

        Used for duplicate-delivery detection. Returns the number of matching
        events (0 = none, 1 = normal, 2+ = duplicate delivery detected).
        Uses the same matching logic as _rollout_has_user_event.
        """
        if rollout_path is None or not message:
            return 0
        try:
            if not rollout_path.exists():
                return 0
            file_size = rollout_path.stat().st_size
            if file_size <= offset:
                return 0
            with rollout_path.open("r", encoding="utf-8") as f:
                f.seek(offset)
                raw = f.read()
        except OSError:
            return 0

        lines = raw.split("\n")
        if not raw.endswith("\n"):
            lines = lines[:-1]
        norm_message = cls._normalize_for_match(message)
        min_distinctive_len = min(len(norm_message), 64)
        count = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            for candidate in cls._extract_rollout_user_texts(record):
                norm_candidate = cls._normalize_for_match(candidate)
                if norm_candidate == norm_message:
                    count += 1
                    break
                if (
                    len(norm_candidate) >= min_distinctive_len
                    and len(norm_message) > 40
                ):
                    prefix_len = min(200, len(norm_candidate), len(norm_message))
                    if norm_candidate[:prefix_len] == norm_message[:prefix_len]:
                        count += 1
                        break
        return count

    @staticmethod
    def _extract_rollout_user_texts(record: dict) -> list[str]:
        """Extract user-message text candidates from a rollout JSONL record.

        Covers the known Codex rollout formats:
          * type=event_msg, payload.type=user_message, payload.message=<str>
          * type=response_item, payload.role=user, payload.content=[...]
          * type=user, message=<str or dict with content>
        """
        texts: list[str] = []
        record_type = record.get("type")
        payload = record.get("payload")

        if record_type == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "user_message":
                msg = payload.get("message")
                if isinstance(msg, str):
                    texts.append(msg)

        elif record_type == "response_item" and isinstance(payload, dict):
            if payload.get("role") == "user":
                content = payload.get("content")
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            text = item.get("text")
                            if isinstance(text, str):
                                texts.append(text)
                        elif isinstance(item, str):
                            texts.append(item)

        elif record_type == "user":
            msg = record.get("message")
            if isinstance(msg, str):
                texts.append(msg)
            elif isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            text = item.get("text")
                            if isinstance(text, str):
                                texts.append(text)
                        elif isinstance(item, str):
                            texts.append(item)

        return texts

    def auth_state_path(self) -> Path | None:
        return _resolved_codex_home(getattr(self, "terminal_id", None)) / "auth.json"

    def provider_process_started_at(self, pane_pid: int) -> float | None:
        from cli_agent_orchestrator.services.fork_context_service import _descendants

        matches = []
        for pid in _descendants(pane_pid):
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
                if b"codex" in cmd:
                    matches.append(pid)
            except OSError:
                pass
        if len(matches) != 1:
            return None
        stat = Path(f"/proc/{matches[0]}/stat").read_text().split()
        btime = next(
            float(x.split()[1])
            for x in Path("/proc/stat").read_text().splitlines()
            if x.startswith("btime ")
        )
        return btime + float(stat[21]) / os.sysconf(os.sysconf_names["SC_CLK_TCK"])

    async def _handle_trust_prompt(self, timeout: float = 20.0) -> None:
        """Dismiss a workspace-trust or update dialog that blocks readiness.

        Workspace trust is accepted with Enter. An update dialog is dismissed
        with '3'+Enter so CAO never selects the default global-install action.
        """
        start_time = time.time()
        # Iteration cap: prevents unbounded spin when asyncio.sleep is mocked
        # (wall-clock guard alone is defeated by instant-return mocks).
        max_iterations = int(timeout * 3)
        iterations = 0
        while time.time() - start_time < timeout and iterations < max_iterations:
            iterations += 1
            output = get_backend().get_history(self.session_name, self.window_name)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Cheap pre-check: skip the expensive regex chain when output is
            # already clean (no ESC, no CR, no C1 CSI opener).  Covers ~100% of
            # mocked-test iterations and many real iterations after startup.
            if "\x1b" not in output and "\r" not in output and "\x9b" not in output:
                clean_output = output
            else:
                clean_output = strip_terminal_escapes(re.sub(ANSI_CODE_PATTERN, "", output))
            bottom_region = "\n".join(clean_output.splitlines()[-STARTUP_PROMPT_BOTTOM_LINES:])

            if re.search(TRUST_PROMPT_PATTERN, clean_output):
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info("Codex workspace trust prompt detected, auto-accepting")
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                return

            if _has_update_dialog_in_bottom(clean_output):
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info(
                    "Codex update-available dialog detected, selecting " "'Skip until next version'"
                )
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_keys(self.session_name, self.window_name, "3", enter_count=0)
                # TUI rendering latency: '3' highlights the menu item, Enter confirms.
                await asyncio.sleep(0.3)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                return

            # Exit when the bottom region shows the idle composer prompt AND no
            # dialog is active. The welcome banner alone is insufficient — it
            # renders as normal startup chrome BEFORE a late update dialog appears.
            has_idle = _has_startup_idle_composer(clean_output)
            # MERGE NOTE (upstream bfc4d71f): upstream tests
            # TRUST_PROMPT_PATTERN and a separate TRUST_PROMPT_PATTERN_V2 +
            # TRUST_PROMPT_FOOTER pair. Neither V2 symbol exists in this fork —
            # our TRUST_PROMPT_PATTERN (:164) already unions BOTH trust texts
            # ("allow Codex to work in this folder" | "Do you trust the contents
            # of this directory"), so the V2 clause is redundant here rather than
            # dropped. Transcribing it verbatim raised NameError in 14 tests.
            has_dialog = re.search(TRUST_PROMPT_PATTERN, bottom_region) or (
                _has_update_dialog_in_bottom(clean_output)
            )
            if has_idle and not has_dialog:
                logger.info("Codex started — idle prompt visible, no blocking dialog")
                return

            await asyncio.sleep(1.0)
        pane_tail = ""
        try:
            output = get_backend().get_history(self.session_name, self.window_name)
            if output:
                pane_tail = "\n".join(output.splitlines()[-10:])
        except Exception:
            pass
        logger.error(
            "Codex startup prompt handler timed out; no prompt or welcome banner detected. "
            "Pane tail:\n%s",
            pane_tail,
        )

    async def initialize(
        self,
        *,
        coordinates: tuple[str, str] | None = None,
        provider_override: Any = None,
        raw_status: bool = False,
    ) -> bool:
        """Initialize Codex provider by starting codex command."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = get_server_settings()["provider_init_timeout"]
        shell_kwargs = {"timeout": init_timeout}
        if coordinates is not None:
            shell_kwargs["coordinates"] = coordinates
        if not await wait_for_shell(self.terminal_id, **shell_kwargs):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")
        self.shell_baseline = self.capture_shell_baseline()
        if not self.shell_baseline:
            raise ProviderError("shell_baseline_unavailable")

        # Send a warm-up command before launching codex.
        # Codex exits immediately in freshly-created tmux sessions where the shell
        # has not yet processed a full interactive command cycle.
        # Arm the StatusMonitor stickiness gate: each send_keys here represents
        # external input that must be allowed to drive PROCESSING transitions
        # past any previously-latched ready state.
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, "echo ready")
        await asyncio.sleep(2.0)

        # Build command with flags and agent profile (developer_instructions).
        # --no-alt-screen: run in inline mode so output stays in normal scrollback,
        #   making tmux capture-pane reliable.
        # --disable shell_snapshot: avoid TTY input conflicts (SIGTTIN) in tmux
        #   caused by the shell_snapshot subprocess inheriting stdin.
        command = self._build_codex_command()
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Handle workspace trust prompt if it appears (new/untrusted directories)
        await self._handle_trust_prompt(timeout=20.0)

        init_timeout = float(get_server_settings()["provider_init_timeout"])
        from cli_agent_orchestrator.services.auto_responder import auto_responder

        async def notify_blocked(rule_name: str) -> None:
            if self.blocked_wait_notifier is not None:
                await self.blocked_wait_notifier(rule_name)

        def probe_blocked() -> tuple[str, str] | None:
            gate = auto_responder.waiting_gate(self.terminal_id)
            return gate if isinstance(gate, tuple) else None

        blocked_policy = BlockedWaitPolicy(
            probe=probe_blocked,
            blocked_cap_s=BLOCKED_WAIT_CAP_S,
            on_first_blocked=notify_blocked,
        )
        # WAITING_USER_ANSWER: first-run login menu is a successful init (upstream
        # 0e7b70bb); blocks_orchestrated_input_while_waiting_user_answer prevents
        # assign/handoff paste into the live menu.
        ready = await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED, TerminalStatus.WAITING_USER_ANSWER},
            timeout=init_timeout,
            polling_interval=1.0,
            provider_override=provider_override,
            raw_status=raw_status,
            blocked_policy=blocked_policy,
        )
        if not ready:
            suffix = (
                f" after blocked wait rule '{blocked_policy.last_blocked_rule}'"
                if blocked_policy.last_blocked_rule
                else ""
            )
            raise TimeoutError(
                f"Codex initialization timed out after {init_timeout:g} seconds{suffix}"
            )

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        # Native status (herdr): trust the backend's agent state when available;
        # on herdr the buffer is never fed, so buffer parsing can't leave UNKNOWN.
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        # This check is intentionally stateful and remains outside the pure
        # screen-local classifier used by Stage-0b evidence publication.
        if self._initialized and self.shell_baseline:
            current_command = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
            if current_command == self.shell_baseline:
                return TerminalStatus.ERROR

        # herdr never pushes a buffer (pipe_pane is a no-op there); read live
        # pane content instead of falling through to "no output" on every call.
        output = self._resolve_buffer(output)
        # Rendered cells do not carry widget ownership. Any assistant/tool frame
        # whose bottom cells (including retained SGR) equal a valid dialog frame
        # is snapshot-indistinguishable from that dialog. Codex owns the update
        # prompt only during startup, so lifecycle state is the external signal:
        # fail closed while INITIALIZING, preserve the same rows as content once
        # RUNNING. The startup handler remains responsible for dismissal.
        if not self._initialized and _has_update_dialog_in_bottom(strip_terminal_escapes(output)):
            return TerminalStatus.WAITING_USER_ANSWER
        return self._get_screen_local_status(output)

    @staticmethod
    def _get_screen_local_status(output: str) -> TerminalStatus:
        """Classify Codex text without consulting backend or mutable provider state."""
        if not output:
            return TerminalStatus.UNKNOWN

        # Strip the RAW pipe-pane escapes (cursor positioning, in-place redraws),
        # not just SGR colour codes — otherwise cursor sequences survive and the
        # idle ``›`` prompt / structural checks below misfire on the raw stream.
        clean_output = strip_terminal_escapes(output)
        tail_output = "\n".join(clean_output.splitlines()[-25:])

        # Search for user messages, excluding the Codex TUI footer when present.
        # The TUI footer (idle prompt hint like "› Summarize recent commits" +
        # status bar "? for shortcuts / context left") can contain › followed by
        # suggestion text, which USER_PREFIX_PATTERN would incorrectly match as
        # user input, preventing COMPLETED detection.
        # Only apply the cutoff when TUI footer indicators are actually present
        # to avoid over-excluding in short outputs or test fixtures.
        all_lines = clean_output.splitlines()
        if _has_tui_footer_in_tail(all_lines) or _has_known_composer_placeholder_at_bottom(
            all_lines
        ):
            cutoff_pos = _compute_tui_footer_cutoff(all_lines)
        else:
            cutoff_pos = len(clean_output)

        last_user = None
        for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
            if match.start() < cutoff_pos:
                last_user = match

        output_after_last_user = clean_output[last_user.start() :] if last_user else clean_output
        # Skip MCP tool-call markers — those mark "model invoked a tool", not
        # "model has replied", and shouldn't gate WAITING/ERROR detection.
        assistant_after_last_user = bool(
            last_user and _find_assistant_marker(output_after_last_user) is not None
        )

        # Check trust prompt early — the trust menu uses › which matches the idle prompt
        # pattern, and PROCESSING_PATTERN matches "running" in "You are running Codex in..."
        trust = re.search(TRUST_PROMPT_PATTERN, clean_output)
        if trust:
            selector = TRUST_SELECTOR_PATTERN.search(clean_output, trust.end())
            if (
                selector is not None
                and clean_output[trust.end() : selector.start()].count("\n") <= 4
            ):
                return TerminalStatus.WAITING_USER_ANSWER

        # First-run login/auth menu (no credentials). Bottom-anchored with footer.
        bottom_region = "\n".join(clean_output.splitlines()[-15:])
        if re.search(LOGIN_MENU_PATTERN, bottom_region) and re.search(
            LOGIN_MENU_FOOTER, bottom_region
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # Boxed command-approval modal ("Command Approval Required" / "[a] Accept"
        # / "[d] Decline"). Reuses the copy that STARTUP_BLOCKING_INPUT_PATTERN
        # already vetoes readiness on at startup — the same modal can appear at
        # RUNTIME under any approval-prompting codexProfile, and only the startup
        # path used to notice it.
        #
        # Bottom-anchored like trust-v2 and the update dialog, and placed BEFORE
        # the idle/COMPLETED classification for the same reason: the TUI composer
        # and status bar keep rendering while the modal is up, so the idle-prompt
        # check below would otherwise report COMPLETED (or PROCESSING when the
        # composer has scrolled off) for a pane that is hard-blocked on a
        # keystroke. A COMPLETED there is the dangerous case — it tells the
        # conductor the agent is free and invites more work into a dead pane.
        #
        # NOT gated on `not assistant_after_last_user` (unlike WAITING_PROMPT_PATTERN
        # below): the modal is raised mid-turn, after the model has already emitted
        # bullets, so that gate would suppress every real occurrence. Prose that
        # merely quotes the copy is excluded structurally instead — see
        # _has_approval_modal_in_bottom.
        if _has_approval_modal_in_bottom(clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        # Runtime approval prompt as codex-cli 0.147.0 actually renders it -- a
        # numbered menu, not the boxed modal above. This is the check that fires on
        # a current-Codex approval; without it a live prompt classified as IDLE
        # (verified against the live capture in
        # test/providers/fixtures/codex_approval_modal_raw.txt), because the
        # prompt's own "› 1. Yes, proceed (y)" cursor line is both the last
        # USER_PREFIX_PATTERN match and an idle-prompt match, so the classification
        # below saw a user message with no reply after it. IDLE is as dangerous as
        # COMPLETED here: both tell the conductor the pane is free.
        #
        # Placed after the legacy modal check and before the idle classification,
        # for the same reason: the composer and status bar keep rendering while the
        # prompt is up, so the idle-prompt check cannot see the block.
        #
        # Structural, not title-driven: see _has_approval_prompt_in_bottom. Both
        # this buffer path and get_status_from_screen's rendered-screen path reach
        # it through this one call, so the two cannot disagree.
        if _has_approval_prompt_in_bottom(clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        # Check bottom of captured output for idle prompt.
        # With --no-alt-screen, scrollback contains history so we can't anchor
        # to end-of-string. Instead, check only the last few lines.
        bottom_lines = clean_output.strip().splitlines()[-IDLE_PROMPT_TAIL_LINES:]
        has_idle_prompt_at_end = any(
            re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line, re.IGNORECASE) for line in bottom_lines
        )

        # Only treat ERROR/WAITING prompts as actionable if they appear after the last user message
        # and are not part of an assistant response.
        if last_user is not None:
            if not assistant_after_last_user:
                if re.search(
                    WAITING_PROMPT_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.WAITING_USER_ANSWER
                if re.search(
                    ERROR_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.ERROR
        else:
            if re.search(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.WAITING_USER_ANSWER
            if re.search(ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.ERROR
        if has_idle_prompt_at_end:
            # Check for TUI progress indicator ("• Working (0s • esc to interrupt)").
            # With --no-alt-screen, the TUI footer (› hint + status bar) is always
            # rendered at the bottom, even during processing. The • in the progress
            # spinner matches ASSISTANT_PREFIX_PATTERN, causing a false COMPLETED.
            # Detect the spinner and return PROCESSING before checking for COMPLETED.
            if re.search(TUI_PROGRESS_PATTERN, tail_output, re.MULTILINE):
                return TerminalStatus.PROCESSING

            # Consider COMPLETED only if we see an assistant marker (skipping
            # MCP tool-call markers) after the last user message. Without the
            # tool-call filter, "• Called <server>.<tool>(...)" emitted before
            # the model has actually replied would trip COMPLETED prematurely.
            if last_user is not None:
                if _find_assistant_marker(clean_output[last_user.start() :]) is not None:
                    return TerminalStatus.COMPLETED

                return TerminalStatus.IDLE

            # No user-message marker in the cleaned buffer. Two cases:
            # - Fresh init: no assistant content either → IDLE.
            # - Long-running response: the › user marker has been evicted from
            #   the rolling state buffer by the time the response settles, but an
            #   assistant bullet is still visible. Without this branch we'd
            #   return IDLE forever and ``wait_for_status(completed)`` in the
            #   e2e tests would time out.
            # Search above the TUI footer cutoff so the › suggestion-hint and
            # status-bar lines aren't confused with a model reply.
            if _find_assistant_marker(clean_output[:cutoff_pos]) is not None:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        # If we're not at an idle prompt and we don't see explicit errors/permission prompts,
        # assume the CLI is still producing output.
        return TerminalStatus.PROCESSING

    # Opt in to pyte rendered-screen detection (gated by CAO_PYTE_STATUS). The
    # existing get_status() regex logic above already works correctly against a
    # composited screen (verified against a live capture-pane snapshot) — the
    # bug is specific to the raw pipe-pane rolling buffer, where an unsent TUI
    # composer draft can evict the user/assistant anchors from the 8KB window.
    # The base class's default get_status_from_screen() (join + delegate to
    # get_status) is sufficient here, so no override is needed — see base.py's
    # ClaudeCodeProvider reference implementation for a provider that DOES need
    # a purpose-built override.
    supports_screen_detection = True

    # F530: rows the codex TUI renders as persistent footer chrome, NOT agent
    # output. dialog_region() drops these before measuring the dialog tail, so a
    # still-active modal (the resume-cwd chooser) is not pushed out of the tail
    # by its own spinner/composer/status bar. Genuine agent output matches none
    # of these, so real scrollback below a dialog still supersedes it (F55).
    _CHROME_ROW_PATTERNS = (
        re.compile(TUI_PROGRESS_PATTERN),  # • Working (…s • esc to interrupt)
        re.compile(r"•\s+Ran\b.*\bctrl \+ t to view transcript"),  # turn footer
        re.compile(r"^\s*" + IDLE_PROMPT_PATTERN + r"\s+Ask Codex to do anything"),
        re.compile(STARTUP_FOOTER_PATTERN),  # status/context bar, "? for shortcuts"
        re.compile(r"\btab\s+to\s+queue\s+message\b", re.IGNORECASE),
        re.compile(r"^\s*" + IDLE_PROMPT_PATTERN + r"\s*$"),  # bare composer prompt
    )

    def chrome_row_patterns(self) -> list["re.Pattern[str]"]:
        return list(self._CHROME_ROW_PATTERNS)

    @property
    def resolved_model(self) -> Optional[str]:
        """Return the effective model resolved during command build."""
        return getattr(self, "_resolved_model", None)

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        """Block orchestrated input while Codex is showing an interactive dialog."""
        return True

    supports_draft_preservation = True
    composer_clear_keys = ["C-a", "C-k"]
    clear_immune_ghosts = True
    # Dim-SGR ghost detection needs escape-preserving capture-pane (-e).
    composer_parse_accepts_escapes = True
    liveness_exclude_patterns = [
        rf"^\s*{IDLE_PROMPT_PATTERN}",
        TUI_FOOTER_PATTERN,
        r"\btab\s+to\s+queue\s+message\b",
    ]

    signal_kinds = frozenset({"waiting", "error", "progress", "completion", "chrome"})

    def emit_screen_signals(self, screen_lines: list[str]) -> tuple[ScreenSignal, ...]:
        """Produce Codex signals while preserving the existing fixture corpus."""
        joined = "\n".join(screen_lines)
        clean = strip_terminal_escapes(joined)
        rows = clean.splitlines()
        legacy_status = self._get_screen_local_status(joined)
        startup_update_dialog = not self._initialized and _has_update_dialog_in_bottom(clean)
        chrome_rows = [
            index
            for index, row in enumerate(rows)
            if re.search(IDLE_PROMPT_SCREEN_PATTERN, row, re.IGNORECASE)
        ]
        progress_rows = [
            index
            for index, row in enumerate(rows)
            if re.search(TUI_PROGRESS_PATTERN, row) is not None
        ]
        terminal_index = next(
            (index for index in range(len(rows) - 1, -1, -1) if rows[index].strip()),
            -1,
        )
        signals: list[ScreenSignal] = []
        if startup_update_dialog:
            dialog_footer_index = next(
                (
                    index
                    for index in range(len(rows) - 1, -1, -1)
                    if _UPDATE_DIALOG_ROW_PATTERNS[-1].fullmatch(rows[index].strip())
                ),
                max(len(rows) - 1, 0),
            )
            signals.append(
                ScreenSignal(
                    "waiting",
                    "DIALOG_ACTION_FOOTER_PATTERN",
                    dialog_footer_index,
                )
            )
        for index, row in enumerate(rows):
            progress = re.search(TUI_PROGRESS_PATTERN, row) is not None
            if progress:
                signals.append(
                    ScreenSignal("progress", "TUI_PROGRESS_PATTERN", index, row, "corroborable")
                )
            if (
                legacy_status == TerminalStatus.WAITING_USER_ANSWER
                and TRUST_SELECTOR_PATTERN.search(row)
            ):
                signals.append(ScreenSignal("waiting", "TRUST_SELECTOR_PATTERN", index))
            if (
                not progress_rows
                and index == terminal_index
                and DIALOG_ACTION_FOOTER_PATTERN.search(row)
            ):
                signals.append(ScreenSignal("waiting", "DIALOG_ACTION_FOOTER_PATTERN", index))
            if RESUME_CWD_CHOOSER_PATTERN.search(row):
                # F516 D2: resume-cwd chooser title → WAITING_USER_ANSWER.
                # F530: emit UNCONDITIONALLY, even when a progress spinner
                # coexists on screen. The chooser is a hard input-blocking modal
                # — codex cannot actually be "working" while blocked on it, so a
                # concurrently-rendered "• Working" footer must not demote it to
                # PROCESSING. The shared law resolves ``waiting`` before
                # ``progress``/``completion``, so this WAITING signal wins and the
                # auto-responder's D6 gates no longer veto the fire (F530 layer 2).
                signals.append(
                    ScreenSignal("waiting", "RESUME_CWD_CHOOSER_PATTERN", index)
                )
            if legacy_status == TerminalStatus.WAITING_USER_ANSWER and re.search(
                WAITING_PROMPT_PATTERN, row, re.IGNORECASE
            ):
                signals.append(ScreenSignal("waiting", "WAITING_PROMPT_PATTERN", index))
            if legacy_status == TerminalStatus.ERROR and re.search(
                ERROR_PATTERN, row, re.IGNORECASE
            ):
                signals.append(ScreenSignal("error", "ERROR_PATTERN", index))
            assistant = re.search(ASSISTANT_PREFIX_PATTERN, row, re.IGNORECASE) is not None
            excluded_assistant = bool(
                re.search(MCP_TOOL_CALL_PATTERN, row, re.IGNORECASE)
                or re.search(SYSTEM_NOTICE_PATTERN, row, re.IGNORECASE)
            )
            if (
                assistant
                and not excluded_assistant
                and (
                    legacy_status == TerminalStatus.COMPLETED
                    or progress
                    or (bool(progress_rows) and index > max(progress_rows) and bool(chrome_rows))
                )
            ):
                signals.append(ScreenSignal("completion", "ASSISTANT_PREFIX_PATTERN", index))
            if index in chrome_rows:
                signals.append(ScreenSignal("chrome", "IDLE_PROMPT_SCREEN_PATTERN", index))

        # Codex historically treats a signal-free screen as PROCESSING. Keep
        # that output byte-identical for the existing corpus with an explicit,
        # named producer fallback; it participates only when no law signal exists.
        if not signals and legacy_status == TerminalStatus.PROCESSING:
            assert SCREEN_FALLBACK_PROCESSING_PATTERN.search(clean)
            signals.append(
                ScreenSignal(
                    "progress",
                    "SCREEN_FALLBACK_PROCESSING_PATTERN",
                    max(len(rows) - 1, 0),
                    clean,
                    "exempt",
                )
            )
        return tuple(signals)

    def transient_error_detected(
        self, rows: list[str], classification: ScreenClassificationResult
    ) -> bool:
        clean_rows = [strip_terminal_escapes(row) for row in rows]
        positive = any(
            re.search(pattern, row) is not None
            for pattern in TRANSIENT_API_ERROR_PATTERNS
            for row in clean_rows
        )
        excluded = any(
            re.search(pattern, row) is not None
            for pattern in TRANSIENT_ERROR_EXCLUSIONS
            for row in clean_rows
        )
        strict_idle = any(
            re.search(IDLE_PROMPT_STRICT_PATTERN, row, re.IGNORECASE) is not None
            for row in clean_rows
        )
        return (
            positive
            and not excluded
            and strict_idle
            and classification.status == TerminalStatus.IDLE
        )

    def classify_idle_reason(
        self, rows: list[str], classification: ScreenClassificationResult
    ) -> str | None:
        clean_rows = [strip_terminal_escapes(row) for row in rows]
        footer_index = _find_tui_footer_index(clean_rows)
        strong_footer_indices = {
            index
            for index, row in enumerate(clean_rows)
            if _tui_footer_candidate_strength(row) == "strong"
            and bool(re.search(r"·\s+[~/]|\?\s+for shortcuts", row, re.IGNORECASE))
        }
        state = "neutral"
        banner_rows: list[str] = []

        for index, row in enumerate(clean_rows):
            if re.search(USER_PREFIX_PATTERN, row):
                state = "user"
                continue
            if re.search(ASSISTANT_PREFIX_PATTERN, row):
                state = "assistant"
                continue
            if (
                index == footer_index
                or index in strong_footer_indices
                or re.search(IDLE_PROMPT_STRICT_PATTERN, row, re.IGNORECASE)
            ):
                state = "neutral"
                continue

            override_banner = (
                row.startswith("⚠")
                or re.search(ERROR_PATTERN, row) is not None
                or re.search(CONTENT_POLICY_REFUSAL_PATTERN, row) is not None
            )
            neutral_http_banner = state == "neutral" and re.search(r"^\d{3} ", row) is not None
            if override_banner or neutral_http_banner:
                state = "banner"
                banner_rows.append(row)
                continue
            if state == "banner":
                banner_rows.append(row)

        if any(re.search(CONTENT_POLICY_REFUSAL_PATTERN, row) for row in banner_rows):
            return "content_policy_refusal"
        if any(
            re.search(pattern, row) is not None
            for pattern in TRANSIENT_ERROR_EXCLUSIONS
            for row in banner_rows
        ):
            return "quota_or_auth"
        if any(
            re.search(pattern, row) is not None
            for pattern in TRANSIENT_API_ERROR_PATTERNS
            for row in banner_rows
        ):
            return "transient_api_error"
        if any(re.search(ERROR_PATTERN, row) is not None for row in banner_rows):
            return "error_banner"
        return None

    def classify_injection_hazard(self, rows: list[str]) -> str | None:
        return (
            "interactive_dialog"
            if self.get_status_from_screen(rows) == TerminalStatus.WAITING_USER_ANSWER
            else None
        )

    def get_status_from_screen(self, screen_lines: list[str]) -> TerminalStatus:
        return self.classify_screen(screen_lines).status

    def read_composer_draft(self, screen_lines: list[str]) -> str | None:
        """Read the visible Codex composer draft from rendered screen lines.

        Codex renders the editable composer at the bottom with a leading ``›``.
        The status footer sits below it. The parser intentionally uses only the
        provider's rendered screen shape; the shared draft guard stays generic.

        When lines retain SGR escapes (``capture-pane -e`` / strip_escapes=False),
        dim-wrapped composer body text (SGR 2) is treated as a ghost suggestion
        and returns ``""`` so it is not stashed/restored as a real draft. Plain
        (escape-stripped) lines still work; placeholder strings remain a fallback.
        """
        if not screen_lines:
            return None

        raw_lines = [line.rstrip("\r") for line in screen_lines]
        # Structural matching uses escape-stripped text; segment join keeps raw
        # widths where useful, then we strip SGR from the final draft.
        plain_lines = [strip_terminal_escapes(line).rstrip() for line in raw_lines]

        last_nonempty = next(
            (i for i in range(len(plain_lines) - 1, -1, -1) if plain_lines[i].strip()),
            -1,
        )
        footer_idx = _find_tui_footer_index(plain_lines)
        if footer_idx is None:
            footer_idx = len(plain_lines)

        search_end = footer_idx
        while search_end > 0 and not plain_lines[search_end - 1].strip():
            search_end -= 1

        prompt_idx: int | None = None
        lower_bound = max(0, search_end - 12)
        for i in range(search_end - 1, lower_bound - 1, -1):
            if "›" in plain_lines[i]:
                prompt_idx = i
                break
        if prompt_idx is None:
            return None

        # Ghost detection needs the raw (possibly dim) body after › on the
        # prompt line plus continuation rows before the footer.
        raw_prompt = raw_lines[prompt_idx]
        # Locate › in raw by walking with CSI skipped, or plain rfind on stripped.
        plain_prompt = plain_lines[prompt_idx]
        prompt_pos = plain_prompt.rfind("›")
        first_plain = plain_prompt[prompt_pos + 1 :]
        if first_plain.startswith(" "):
            first_plain = first_plain[1:]

        raw_body_parts: list[str] = []
        # Extract raw suffix after › (CSI may wrap the glyph).
        raw_after = self._raw_after_prompt_glyph(raw_prompt)
        raw_body_parts.append(raw_after)
        for line in raw_lines[prompt_idx + 1 : search_end]:
            raw_body_parts.append(line)
        raw_body = "\n".join(raw_body_parts)
        if _composer_body_is_dim_ghost(raw_body):
            return ""

        segments = [first_plain]
        draft_region_has_assistant = any(
            re.match(
                ASSISTANT_PREFIX_PATTERN,
                strip_terminal_escapes(candidate_line).strip(),
                re.IGNORECASE,
            )
            for candidate_line in plain_lines[prompt_idx + 1 : last_nonempty]
        )
        for offset, line in enumerate(
            plain_lines[prompt_idx + 1 : search_end], start=prompt_idx + 1
        ):
            text = line.strip()
            # Defense in depth: a status_line row must never become draft text,
            # even if a future footer variant misses the primary detector.
            candidate = _tui_footer_candidate_strength(line)
            if offset == last_nonempty and candidate is not None and not draft_region_has_assistant:
                continue
            if not text:
                segments.append("")
                continue
            if text.startswith(("╭", "╰", "│")) and text.endswith(("╮", "╯", "│")):
                continue
            segments.append(text)

        while segments and segments[-1] == "":
            segments.pop()

        # Join using plain line widths (escape-stripped); matches previous
        # behavior for wrap detection on capture-pane plain or pyte screens.
        draft = self._join_composer_segments(plain_lines, prompt_idx, prompt_pos, segments)
        if draft and any(
            re.match(
                ASSISTANT_PREFIX_PATTERN,
                strip_terminal_escapes(candidate).strip(),
                re.IGNORECASE,
            )
            for candidate in plain_lines[:prompt_idx]
        ):
            # A non-empty composer-shaped row below assistant output has no
            # snapshot-only ownership proof. Returning None makes draft_guard
            # defer injection instead of clearing/restoring uncertain text.
            return None
        if draft.strip() in CODEX_EMPTY_COMPOSER_PLACEHOLDERS:
            return ""
        return draft

    @staticmethod
    def _pane_shows_pasted_chip(captured: str) -> bool:
        """Whether the ACTIVE composer still shows an unsubmitted paste chip.

        The stuck F435 signature is the CURRENT composer row carrying the
        ``[Pasted Content NNNN chars]`` chip. A submitted composer instead shows
        an empty idle placeholder or the Working/Thinking spinner, so the chip's
        presence in the active composer is a durable, idempotent gate for
        re-sending Enter.

        Composer-SCOPED (BLOCKER B1): the chip is matched ONLY within the active
        composer region at the bottom of the rendered pane, never against the
        whole 200-row capture. A HISTORICAL chip that has scrolled up into
        transcript history — with the current composer now empty or showing the
        Working spinner — must NOT be read as stuck, or recovery would blind a
        submitted composer with an extra Enter (a double-submit).

        Scoping mirrors ``read_composer_draft``: locate the TUI footer, then the
        last ``›``/``»`` composer row in the small window just above it, and test
        the chip pattern on that row plus its continuation rows up to the footer.
        Escapes are stripped first so an SGR-wrapped chip still matches.

        Note this deliberately does NOT inherit ``read_composer_draft``'s
        "assistant output above the prompt ⇒ defer (None)" ownership rule: that
        rule protects human-draft stash/restore against ambiguous ownership, but
        the ``[Pasted Content N chars]`` chip is unambiguous CAO-injected chrome
        (never user/assistant prose), and the real stuck pane always has the
        SEED_OK assistant bullet above the composer.
        """
        if not captured:
            return False
        raw_lines = [line.rstrip("\r") for line in captured.splitlines()]
        plain_lines = [strip_terminal_escapes(line).rstrip() for line in raw_lines]

        footer_idx = _find_tui_footer_index(plain_lines)
        if footer_idx is None:
            footer_idx = len(plain_lines)

        # Identify the ACTIVE composer row using the same strict adjacency the
        # status/extraction paths use: walk up from the footer through only
        # blank / "? for shortcuts" rows to a ``›``/``»`` composer row. Any
        # other content (e.g. a ``• Working`` spinner, an assistant bullet)
        # between the footer and a prompt row rejects the anchor — so a
        # HISTORICAL chip that has scrolled up into transcript history is never
        # mistaken for the current composer (BLOCKER B1).
        prompt_idx = _find_composer_anchor_index(plain_lines, footer_idx)
        if prompt_idx is None:
            return False

        # Match the chip only on the active composer region: the anchored prompt
        # row and any continuation rows up to the footer. The chip is single-line
        # in practice; scanning the region is safe and future-proof.
        search_end = footer_idx
        while search_end > 0 and not plain_lines[search_end - 1].strip():
            search_end -= 1
        region = "\n".join(plain_lines[prompt_idx:search_end])
        return CODEX_PASTE_CHIP_PATTERN.search(region) is not None

    @staticmethod
    def _active_composer_chip_count(captured: str) -> int | None:
        """Return the ``[Pasted Content N chars]`` count on the ACTIVE composer.

        ``None`` when no chip is on the active composer (or it cannot be
        anchored). Used to prove OWNERSHIP before a recovery Enter (BLOCKER 3):
        we only re-Enter a stuck chip whose char-count matches the current
        dispatch, so an UNRELATED queued/steering draft that happens to own the
        composer is never blind-Entered mid-run.
        """
        if not captured:
            return None
        raw_lines = [line.rstrip("\r") for line in captured.splitlines()]
        plain_lines = [strip_terminal_escapes(line).rstrip() for line in raw_lines]
        footer_idx = _find_tui_footer_index(plain_lines)
        if footer_idx is None:
            footer_idx = len(plain_lines)
        prompt_idx = _find_composer_anchor_index(plain_lines, footer_idx)
        if prompt_idx is None:
            return None
        search_end = footer_idx
        while search_end > 0 and not plain_lines[search_end - 1].strip():
            search_end -= 1
        region = "\n".join(plain_lines[prompt_idx:search_end])
        match = re.search(r"\[Pasted Content\s+(\d+)\s+chars\]", region)
        return int(match.group(1)) if match is not None else None

    @staticmethod
    def _pane_shows_working(captured: str) -> bool:
        """Whether the pane shows the Working/Thinking progress spinner.

        The spinner (``• Working (0s • esc to interrupt)``) means the agent has
        BEGUN the turn — reachable only after the pasted task submitted. This is
        the single unambiguous positive submission signal that a stale pre-paste
        frame can never exhibit (BLOCKER B1): an empty composer, by contrast, is
        indistinguishable pre-paste vs. post-submit, so absence of the chip is
        NOT evidence of submission.
        """
        if not captured:
            return False
        for line in captured.splitlines():
            if re.search(TUI_PROGRESS_PATTERN, strip_terminal_escapes(line)):
                return True
        return False

    @staticmethod
    def _pane_shows_cleared_composer(captured: str) -> bool:
        """Whether the ACTIVE composer is an empty idle prompt / placeholder.

        Composer-scoped exactly like ``_pane_shows_pasted_chip`` (footer anchor).
        This is NOT positive submission proof on its own — a stale pre-paste
        frame renders the same empty prompt — so the caller only treats a
        cleared composer as ``submitted`` once it has POSITIVELY observed the
        chip first and then seen it cleared (a chip→cleared TRANSITION, BLOCKER
        B1). Used solely to detect that transition.
        """
        if not captured:
            return False
        raw_lines = [line.rstrip("\r") for line in captured.splitlines()]
        plain_lines = [strip_terminal_escapes(line).rstrip() for line in raw_lines]
        footer_idx = _find_tui_footer_index(plain_lines)
        if footer_idx is None:
            footer_idx = len(plain_lines)
        prompt_idx = _find_composer_anchor_index(plain_lines, footer_idx)
        if prompt_idx is None:
            return False
        composer_row = plain_lines[prompt_idx].strip()
        if re.fullmatch(IDLE_PROMPT_STRICT_PATTERN, composer_row, re.IGNORECASE) is not None:
            return True
        return _is_known_composer_placeholder(plain_lines[prompt_idx])

    @staticmethod
    def _extract_submitted_turns(captured: str) -> tuple[list[str], int, list[int]]:
        """Parse submitted user turns from a capture, wrap-joined and normalized.

        Returns ``(fingerprints, prompt_idx_or_-1, chip_counts)`` where:

          * ``fingerprints`` is the normalized, wrap-joined content of every
            submitted ``›``/``»`` user turn STRICTLY ABOVE the active composer
            (history). Each entry strips the ``›`` head glyph and CONCATENATES
            the head body with its wrapped continuation rows (soft-wrap breaks
            mid-character with no inserted separator), then collapses internal
            whitespace — so a turn that soft-wrapped across visual rows produces
            the SAME fingerprint as its unwrapped text (BLOCKER 2: wrapped
            continuations are no longer discarded, and no phantom separator is
            introduced at the wrap column).
          * ``prompt_idx_or_-1`` is the active-composer anchor index, or ``-1``
            when no active composer can be anchored (history-less capture).
          * ``chip_counts`` is the ``[Pasted Content N chars]`` char-count found
            on each submitted turn's head row (for chip-multiset accounting).

        History-scoped by the same footer/composer anchor the rest of the module
        uses, so a chip or task text still sitting on the ACTIVE composer row is
        never mistaken for a submitted turn.
        """
        if not captured:
            return [], -1, []
        raw_lines = [line.rstrip("\r") for line in captured.splitlines()]
        plain_lines = [strip_terminal_escapes(line).rstrip() for line in raw_lines]

        footer_idx = _find_tui_footer_index(plain_lines)
        if footer_idx is None:
            footer_idx = len(plain_lines)
        prompt_idx = _find_composer_anchor_index(plain_lines, footer_idx)
        if prompt_idx is None:
            return [], -1, []

        history = plain_lines[:prompt_idx]
        if not history:
            return [], prompt_idx, []

        fingerprints: list[str] = []
        chip_counts: list[int] = []
        i = 0
        n = len(history)
        while i < n:
            row = history[i]
            if re.match(r"^\s*[›»]\s+\S", row) is None:
                i += 1
                continue
            # Start of a submitted user turn. Absorb wrapped continuation rows:
            # non-empty rows that are NOT themselves a new ``›`` head, an
            # assistant/tool bullet, or an idle prompt. A blank row ends the turn.
            head = row
            # Strip the leading ``›``/``»`` glyph (and one following space) so the
            # head body concatenates cleanly with continuation rows.
            head_body = re.sub(r"^\s*[›»]\s?", "", head)
            block_parts = [head_body]
            j = i + 1
            while j < n:
                nxt = history[j]
                if not nxt.strip():
                    break
                if re.match(r"^\s*[›»]\s", nxt) is not None:
                    break
                if re.match(ASSISTANT_PREFIX_PATTERN, nxt, re.IGNORECASE) is not None:
                    break
                block_parts.append(nxt)
                j += 1
            # Soft-wrap reconstruction: concatenate with NO separator so the
            # wrap column does not introduce a phantom space, then collapse
            # internal whitespace runs for a stable fingerprint.
            block = "".join(block_parts)
            fingerprints.append(CodexProvider._normalize_pane_text(block))
            chip_match = CODEX_PASTE_CHIP_PATTERN.search(head)
            if chip_match is not None:
                num = re.search(r"(\d+)\s+chars", head)
                chip_counts.append(int(num.group(1)) if num else -1)
            i = j
        return fingerprints, prompt_idx, chip_counts

    @staticmethod
    def _build_submission_baseline(captured: str | None) -> CodexSubmitBaseline:
        """Build a dispatch-relative baseline from a PRE-send pane capture.

        A ``None`` capture (failed baseline read) yields ``captured_ok=False`` so
        the post-send verifier treats every subsequent verdict as indeterminate
        (never success) — a missing baseline cannot be used to manufacture a
        NEW-turn confirmation.
        """
        if captured is None:
            return CodexSubmitBaseline()
        fingerprints, _prompt_idx, chip_counts = CodexProvider._extract_submitted_turns(captured)
        chip_counter = Counter(
            f"[Pasted Content {c} chars]" for c in chip_counts if c >= 0
        )
        return CodexSubmitBaseline(
            turn_fingerprints=frozenset(fingerprints),
            chip_counter=tuple(sorted(chip_counter.items())),
            turn_count=len(fingerprints),
            captured_ok=True,
        )

    @staticmethod
    def _pane_shows_new_submitted_task(
        captured: str,
        message: str | None,
        baseline: CodexSubmitBaseline | None,
    ) -> str:
        """Classify a POST-send capture relative to the pre-send ``baseline``.

        This is the r5 dispatch-relative submission boundary. Returns one of:

          * ``CODEX_SUBMIT_STATE_SUBMITTED`` — a submitted user turn is present
            that was ABSENT from the baseline AND is attributable to the current
            dispatch: a chip whose ``[Pasted Content N chars]`` count now exceeds
            its baseline multiset count, OR (for messages long enough to have a
            distinctive signature) a new turn whose text contains the task
            signature, OR — when the task is too short for a signature — simply
            ANY new submitted turn absent from the baseline (a short task still
            produces a NEW turn artifact). Historical collisions are in the
            baseline and excluded by construction (BLOCKER 1).
          * ``CODEX_SUBMIT_STATE_INDETERMINATE`` — the baseline is missing/failed,
            OR the post-send capture shows FEWER submitted turns than the
            baseline watermark (turns evicted from the tail → the evidence window
            is unreliable). Never treated as success; the caller re-observes
            within a bound and, failing that, defers (BLOCKER 3: never a blind
            Enter into a pane whose scrollback advanced past capture).
          * ``""`` (empty) — no new submitted turn yet, but the window is intact
            (watermark not shrunk). The caller consults the active-composer state
            (stuck chip vs. cleared) to decide.

        Wrap is normalized before comparison so a soft-wrapped submitted turn
        matches its baseline/absent fingerprint (BLOCKER 2).
        """
        if baseline is None or not baseline.captured_ok:
            # No dispatch-relative reference ⇒ cannot prove a NEW turn.
            return CODEX_SUBMIT_STATE_INDETERMINATE
        if not captured:
            return CODEX_SUBMIT_STATE_INDETERMINATE

        fingerprints, prompt_idx, chip_counts = CodexProvider._extract_submitted_turns(captured)
        if prompt_idx == -1:
            # No anchorable active composer: we cannot separate history from the
            # live draft this frame. Do not guess; re-observe.
            return CODEX_SUBMIT_STATE_INDETERMINATE

        # BLOCKER 3 watermark: if the current capture shows FEWER submitted turns
        # than the baseline, the tail evicted history — the window is unreliable
        # and absence of the current turn is ambiguous. Indeterminate → bounded
        # → defer; never a blind Enter into a busy/scrolled pane.
        if len(fingerprints) < baseline.turn_count:
            return CODEX_SUBMIT_STATE_INDETERMINATE

        # A submitted turn NEW relative to the baseline (fingerprint set diff).
        new_fingerprints = [fp for fp in fingerprints if fp not in baseline.turn_fingerprints]

        # 1) Chip echo: a new [Pasted Content N chars] occurrence beyond the
        #    baseline multiset for that exact N. A same-N repeat dispatch only
        #    counts once the post-send count exceeds the baseline count, so a
        #    pre-existing identical chip cannot confirm (BLOCKER 1: same-N).
        post_chip_counter = Counter(
            f"[Pasted Content {c} chars]" for c in chip_counts if c >= 0
        )
        for chip, post_count in post_chip_counter.items():
            if post_count > baseline.chip_count(chip):
                return CODEX_SUBMIT_STATE_SUBMITTED

        if not new_fingerprints:
            # No new submitted turn appeared; window intact. Defer to composer
            # state (stuck/cleared) in the caller.
            return ""

        # 2) Raw-text echo: for a message long enough to have a distinctive
        #    signature, require the signature to appear in a NEW turn so an
        #    unrelated new line cannot coincidentally confirm.
        signature = CodexProvider._task_echo_signature(message)
        if signature is not None:
            new_blob = CodexProvider._normalize_pane_text("\n".join(new_fingerprints))
            if signature in new_blob:
                return CODEX_SUBMIT_STATE_SUBMITTED
            # A new turn appeared but it does not carry our signature: it is a
            # DIFFERENT dispatch's turn, not ours. Window intact, ours not yet
            # submitted → defer to composer state.
            return ""

        # 3) Short task (no signature): a short raw task still produces a NEW
        #    submitted ``›`` turn absent from the baseline. Since the window is
        #    intact (watermark not shrunk) and a turn we did not have before now
        #    exists directly above the active composer, that is our submission
        #    (BLOCKER 2: short fast-completions no longer false-defer).
        return CODEX_SUBMIT_STATE_SUBMITTED

    @staticmethod
    def _normalize_pane_text(text: str) -> str:
        """Collapse whitespace so wrapped/re-spaced pane rows compare stably."""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _task_echo_signature(message: str | None) -> str | None:
        """A distinctive normalized prefix of the pasted task for echo matching.

        Returns ``None`` when the message is missing or too short to be a
        reliable, low-false-positive signature — in that case raw-text matching
        is skipped and only the unambiguous chip echo (and secondary spinner)
        can confirm.
        """
        if not message:
            return None
        norm = CodexProvider._normalize_pane_text(message)
        # Require a reasonably distinctive run of characters; a handful of
        # characters could collide with unrelated chrome.
        if len(norm) < CODEX_SUBMIT_TASK_SIGNATURE_MIN_CHARS:
            return None
        return norm[:CODEX_SUBMIT_TASK_SIGNATURE_CHARS]

    def capture_submission_baseline(
        self,
        metadata: dict[str, Any],
        backend: Any,
    ) -> CodexSubmitBaseline:
        """F435 r6: snapshot rollout offset + pane BEFORE the paste.

        The PRIMARY dispatch-relative cursor is the rollout file byte offset —
        any user-turn record written after this offset is from THIS dispatch.
        The pane snapshot is a FAST-PATH HINT only: if the pane clearly shows
        submission (new chip/turn), we skip waiting for the rollout flush.
        Correctness never depends on pane content.

        Called by the send seam immediately before ``backend.send_keys``.
        """
        # --- Structural rollout baseline (PRIMARY) ---
        session_uuid = metadata.get("provider_session_id")
        rollout_path = self._resolve_rollout_file(session_uuid)
        rollout_offset = self._rollout_file_offset(rollout_path)

        # --- Pane baseline (fast-path hint) ---
        session = metadata["tmux_session"]
        window = metadata["tmux_window"]
        try:
            captured = backend.get_history(
                session,
                window,
                tail_lines=CODEX_SUBMIT_BASELINE_TAIL_LINES,
                strip_escapes=False,
            )
            captured = captured if isinstance(captured, str) else None
        except Exception as exc:
            logger.warning(
                "F435 submit-verify: baseline capture failed for terminal %s: %s",
                self.terminal_id,
                exc,
            )
            captured = None

        # Build pane hint portion
        pane_baseline = self._build_submission_baseline(captured)
        # B2 r7: capture pre-paste composer chip count for ambiguity detection
        pre_paste_chip = None
        if captured is not None:
            pre_paste_chip = self._active_composer_chip_count(captured)
        # Merge rollout state into the baseline
        return CodexSubmitBaseline(
            rollout_path=rollout_path,
            rollout_offset=rollout_offset,
            turn_fingerprints=pane_baseline.turn_fingerprints,
            chip_counter=pane_baseline.chip_counter,
            turn_count=pane_baseline.turn_count,
            captured_ok=pane_baseline.captured_ok,
            pre_paste_chip_count=pre_paste_chip,
        )

    def verify_submission_after_send(
        self,
        metadata: dict[str, Any],
        backend: Any,
        message: str | None = None,
        baseline: "CodexSubmitBaseline | None" = None,
    ) -> None:
        """F435 r6: confirm the pasted task SUBMITTED; re-Enter if it stuck.

        DIRECTION CHANGE (r6): the PRIMARY confirmation signal is now STRUCTURAL
        — the Codex session rollout JSONL file gains a user-turn record whose
        content matches the dispatched message. This is content-unambiguous: it
        cannot be spoofed by pane reflow, identical repeats, chip eviction, or
        drafts.

        The pane baseline is retained as a FAST-PATH HINT only: if the pane
        clearly shows submission early, we return immediately without waiting
        for the rollout flush. Correctness NEVER depends on pane content.

        Recovery Enter fires ONLY if:
          1. The rollout file has NO matching user event at the deadline, AND
          2. The pane shows a stuck chip owned by this dispatch, AND
          3. The pre-paste baseline had NO existing draft chip of ambiguous
             length (B2 r7: if it did, ownership is unresolvable → defer), AND
          4. A FINAL rollout re-check immediately before sending confirms no
             match (closes the SHOULD double-send race: if the original submit
             landed between observation and action, the re-check catches it),
          5. Immediately before send_special_key, the pane is re-read and the
             composer must STILL show the matching chip (B3 r7 TOCTOU narrow),
          6. After the Enter, if TWO matching post-cursor events exist, a
             duplicate-delivery warning is logged (detection only — unsend is
             impossible). RESIDUAL WINDOW: between the final rollout re-check
             and the actual key-send, the original submit may land (~50ms on
             local tmux). This is an honest, documented race that detection
             can surface but not prevent.

        This closes all r5 blockers:
          * B1 (partial-baseline false-commit): rollout match is exact content,
            not pane fingerprint diffing.
          * B2 (wrap/reflow): rollout content is never wrapped.
          * B3 (same-length ownership): rollout match is by content, not length.
          * B4 (double-send race): re-check rollout immediately before Enter.
          * Identical raw repeats: each dispatch writes a distinct rollout record;
            even byte-identical messages produce separate events (append, not
            dedup).
        """
        session = metadata["tmux_session"]
        window = metadata["tmux_window"]
        own_chip_count = len(message) if message else None

        # --- Resolve rollout file (handle not-yet-created) ---
        rollout_path = baseline.rollout_path if baseline else None
        rollout_offset = baseline.rollout_offset if baseline else 0
        session_uuid = metadata.get("provider_session_id")

        def _ensure_rollout_path() -> Path | None:
            """Resolve or poll for rollout file creation."""
            nonlocal rollout_path
            if rollout_path is not None:
                return rollout_path
            # No UUID and no resume seed means no rollout file to find.
            if not session_uuid and not self.resume_session_uuid():
                return None
            # File may not exist yet at dispatch (fresh session). Poll for creation.
            deadline = time.monotonic() + CODEX_ROLLOUT_CREATION_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                rollout_path = self._resolve_rollout_file(session_uuid)
                if rollout_path is not None:
                    return rollout_path
                time.sleep(CODEX_ROLLOUT_POLL_INTERVAL_SECONDS)
            return None

        def _rollout_confirms() -> bool:
            """Check if rollout has a matching user event after baseline offset."""
            rp = _ensure_rollout_path()
            return self._rollout_has_user_event(rp, rollout_offset, message or "")

        def _pane_hint_submitted() -> bool:
            """Fast-path pane hint: check if pane clearly shows submission.

            B1 r7 FIX: this hint NO LONGER returns success directly. It is
            used only to short-circuit the WAITING state (skip to rollout
            confirm check immediately). Every success return requires a matching
            post-cursor rollout event — pane state may only accelerate the
            path TO the rollout check, never bypass it.
            """
            try:
                captured = backend.get_history(
                    session, window,
                    tail_lines=PYTE_SCREEN_ROWS,
                    strip_escapes=False,
                )
                if not isinstance(captured, str):
                    return False
            except Exception:
                return False
            # Check pane for new submitted turn (fast path)
            new_task_state = self._pane_shows_new_submitted_task(
                captured, message, baseline
            )
            if new_task_state == CODEX_SUBMIT_STATE_SUBMITTED:
                return True
            # Also check the Working/Thinking spinner
            if self._pane_shows_working(captured):
                return True
            return False

        def _pane_shows_stuck_chip() -> bool:
            """Check if the pane has a stuck chip owned by this dispatch.

            B2 r7: recovery Enter requires:
              (a) no matching rollout event (checked by caller), AND
              (b) the pre-paste baseline had NO existing draft chip of ambiguous
                  length — if the pre-paste composer already held a draft whose
                  count is within ±1 of own_chip_count, ownership is
                  unresolvable and we must defer (raise DeliveryDeferredError).
            """
            try:
                captured = backend.get_history(
                    session, window,
                    tail_lines=PYTE_SCREEN_ROWS,
                    strip_escapes=False,
                )
                if not isinstance(captured, str):
                    return False
            except Exception:
                return False
            if not self._pane_shows_pasted_chip(captured):
                return False
            # Verify ownership
            active_n = self._active_composer_chip_count(captured)
            if active_n is None:
                return False
            if own_chip_count is None:
                return True
            if abs(active_n - own_chip_count) > 1:
                return False
            # B2 r7: check pre-paste ambiguity. If the baseline captured a
            # draft chip whose length is within ±1 of our dispatch, the chip
            # we see NOW might be that pre-existing draft, not ours → ambiguous.
            if baseline and baseline.pre_paste_chip_count is not None:
                if abs(baseline.pre_paste_chip_count - own_chip_count) <= 1:
                    # Ambiguous: pre-paste already had a same-length draft.
                    # Cannot determine ownership → must defer.
                    from cli_agent_orchestrator.services.draft_guard import (
                        DeliveryDeferredError,
                    )
                    raise DeliveryDeferredError(
                        f"F435 r7 B2: pre-paste composer already held a draft "
                        f"(chip {baseline.pre_paste_chip_count} chars) within "
                        f"±1 of dispatch ({own_chip_count} chars); ownership "
                        f"is unresolvable — deferring delivery"
                    )
            return True

        # --- Main verification loop ---
        time.sleep(CODEX_SUBMIT_VERIFY_GRACE_SECONDS)

        # B1 r7: When rollout infrastructure is available (rollout_path or
        # session_uuid exists), pane hint accelerates to rollout check only.
        # When rollout is unavailable (no UUID, no file), pane is the only
        # signal and may conclude success (backward compat with pre-r6 tests).
        _has_rollout_infra = (
            rollout_path is not None
            or session_uuid is not None
            or (self.resume_session_uuid() is not None)
        )

        if _pane_hint_submitted():
            if not _has_rollout_infra:
                # No rollout available — pane is the only signal (pre-r6 compat)
                logger.info(
                    "F435 submit-verify: terminal %s confirmed via pane hint "
                    "(no rollout infrastructure available)",
                    self.terminal_id,
                )
                return
            # Rollout infra exists — pane hint accelerates to rollout check
            if _rollout_confirms():
                logger.info(
                    "F435 submit-verify: terminal %s confirmed via rollout "
                    "after pane fast-path hint",
                    self.terminal_id,
                )
                return

        # Primary signal: poll rollout file for the user event
        poll_deadline = time.monotonic() + CODEX_ROLLOUT_POLL_TIMEOUT_SECONDS
        while time.monotonic() < poll_deadline:
            if _rollout_confirms():
                logger.info(
                    "F435 submit-verify: terminal %s confirmed via rollout "
                    "structural signal",
                    self.terminal_id,
                )
                return
            # B1 r7: pane hint accelerates to rollout recheck (when infra exists)
            # or confirms directly (when no infra)
            if _pane_hint_submitted():
                if not _has_rollout_infra:
                    logger.info(
                        "F435 submit-verify: terminal %s confirmed via pane "
                        "hint during poll (no rollout infrastructure)",
                        self.terminal_id,
                    )
                    return
                if _rollout_confirms():
                    logger.info(
                        "F435 submit-verify: terminal %s confirmed via rollout "
                        "after pane hint during poll",
                        self.terminal_id,
                    )
                    return
            time.sleep(CODEX_ROLLOUT_POLL_INTERVAL_SECONDS)

        # Rollout poll exhausted without confirmation. Check if stuck.
        # Recovery Enter fires ONLY if pane shows a stuck owned chip AND
        # a final rollout re-check still shows no match.
        #
        # F491: Before entering the recovery loop, check if a blocking dialog
        # appeared (e.g. resume-cwd) that the auto-responder can dismiss. If
        # the terminal is in WAITING_USER_ANSWER, the paste never submitted
        # because the dialog absorbed the Enter. Don't retry blindly — let the
        # auto-responder handle it or raise to the deferred init retry.
        try:
            from cli_agent_orchestrator.services.auto_responder import auto_responder
            from cli_agent_orchestrator.services.status_monitor import status_monitor

            _f491_status = status_monitor.get_status(self.terminal_id)
            if _f491_status == TerminalStatus.WAITING_USER_ANSWER:
                # A dialog is blocking. Give auto-responder one more chance to
                # fire by forcing a screen evaluation.
                _f491_lines = status_monitor.get_rendered_screen(self.terminal_id)
                if _f491_lines is not None:
                    _f491_provider = self
                    auto_responder.on_screen(self.terminal_id, _f491_provider, _f491_lines)
                # Brief wait for auto-responder dismiss + TUI redraw
                time.sleep(1.5)
                _f491_recheck = status_monitor.get_status(self.terminal_id)
                if _f491_recheck == TerminalStatus.WAITING_USER_ANSWER:
                    logger.warning(
                        "F435/F491 submit-verify: terminal %s has active dialog "
                        "(status WAITING_USER_ANSWER); cannot recover with Enter",
                        self.terminal_id,
                    )
                    raise CodexSubmitStuckError(
                        f"Codex terminal {self.terminal_id} has an active dialog "
                        f"blocking submission (WAITING_USER_ANSWER); the paste "
                        f"never submitted. Auto-responder dismiss pending."
                    )
                logger.info(
                    "F435/F491 submit-verify: terminal %s dialog cleared by "
                    "auto-responder; rechecking rollout",
                    self.terminal_id,
                )
                if _rollout_confirms():
                    return
        except CodexSubmitStuckError:
            raise
        except Exception:
            logger.debug(
                "F435/F491 dialog pre-check failed for %s; proceeding with retry",
                self.terminal_id,
                exc_info=True,
            )

        for attempt in range(1, CODEX_SUBMIT_VERIFY_MAX_RETRIES + 1):
            if not _pane_shows_stuck_chip():
                # No stuck chip visible — cannot recover with Enter. The task
                # may have submitted but the rollout flush is slow, OR the pane
                # is in an indeterminate state. Re-check rollout one more time.
                logger.warning(
                    "F435 submit-verify: terminal %s no stuck chip visible "
                    "(attempt %d/%d); re-checking rollout",
                    self.terminal_id,
                    attempt,
                    CODEX_SUBMIT_VERIFY_MAX_RETRIES,
                )
                time.sleep(CODEX_SUBMIT_VERIFY_BACKOFF_SECONDS * attempt)
                if _rollout_confirms():
                    logger.info(
                        "F435 submit-verify: terminal %s confirmed via rollout "
                        "after extended wait",
                        self.terminal_id,
                    )
                    return
                continue

            # Stuck chip owned by this dispatch is visible. Before sending
            # recovery Enter, MUST re-check rollout (closes double-send race:
            # if the original submit landed between our last check and now,
            # this re-check catches it and we skip the Enter).
            if _rollout_confirms():
                logger.info(
                    "F435 submit-verify: terminal %s confirmed via rollout "
                    "re-check before recovery Enter (race avoided)",
                    self.terminal_id,
                )
                return

            # Rollout still has no match AND pane shows stuck chip → re-Enter.
            # B3 r7: NARROW THE TOCTOU — immediately before sending, re-read
            # the pane and require the composer STILL shows the matching chip.
            # This narrows the window between the rollout re-check above and
            # the actual keystroke.
            try:
                pre_enter_pane = backend.get_history(
                    session, window,
                    tail_lines=PYTE_SCREEN_ROWS,
                    strip_escapes=False,
                )
                if isinstance(pre_enter_pane, str):
                    pre_enter_chip = self._active_composer_chip_count(pre_enter_pane)
                    if pre_enter_chip is None or (
                        own_chip_count is not None
                        and abs(pre_enter_chip - own_chip_count) > 1
                    ):
                        # Chip gone between re-check and now — the submit may
                        # have landed (TOCTOU race). Skip this Enter, re-poll.
                        logger.info(
                            "F435 submit-verify: terminal %s chip vanished on "
                            "pre-Enter reread (TOCTOU avoided, attempt %d)",
                            self.terminal_id,
                            attempt,
                        )
                        time.sleep(CODEX_SUBMIT_VERIFY_BACKOFF_SECONDS * attempt)
                        if _rollout_confirms():
                            logger.info(
                                "F435 submit-verify: terminal %s confirmed via "
                                "rollout after chip-vanished reread",
                                self.terminal_id,
                            )
                            return
                        continue
            except Exception:
                pass  # If pane read fails, proceed with Enter (best effort)

            logger.warning(
                "F435 submit-verify: paste chip still drafted on terminal %s "
                "(attempt %d/%d, rollout negative); re-sending Enter",
                self.terminal_id,
                attempt,
                CODEX_SUBMIT_VERIFY_MAX_RETRIES,
            )
            try:
                backend.send_special_key(session, window, "Enter")
            except Exception as exc:
                logger.warning(
                    "F435 submit-verify: re-Enter failed for terminal %s: %s",
                    self.terminal_id,
                    exc,
                )
            # Wait for rollout to register the submission after recovery Enter
            time.sleep(CODEX_SUBMIT_VERIFY_BACKOFF_SECONDS * attempt)
            if _rollout_confirms():
                # B3 r7: detect duplicate delivery. Count ALL matching events
                # after baseline offset. If >1, a duplicate occurred (the
                # original submit + our recovery Enter both landed). Log and
                # surface the warning but return success (unsend is impossible).
                # RESIDUAL WINDOW: between the final rollout re-check and the
                # send_special_key call, the original submit may land. This
                # detection catches it post-facto but cannot prevent it. The
                # window is bounded by one pane-read + one key-send latency
                # (typically <50ms on local tmux).
                dup_count = self._count_rollout_matches(
                    _ensure_rollout_path(), rollout_offset, message or ""
                )
                if dup_count > 1:
                    logger.warning(
                        "F435 submit-verify: DUPLICATE DELIVERY detected on "
                        "terminal %s — %d matching post-cursor events "
                        "(residual TOCTOU window); first confirmed, remainder "
                        "is a duplicate that cannot be unsent",
                        self.terminal_id,
                        dup_count,
                    )
                logger.info(
                    "F435 submit-verify: terminal %s submitted after %d "
                    "re-Enter(s) (rollout confirmed)",
                    self.terminal_id,
                    attempt,
                )
                return

        # All retries exhausted. Final rollout check.
        if _rollout_confirms():
            logger.info(
                "F435 submit-verify: terminal %s confirmed via final rollout check",
                self.terminal_id,
            )
            return

        raise CodexSubmitStuckError(
            f"Codex terminal {self.terminal_id} did not confirm submission of the "
            f"pasted task after {CODEX_SUBMIT_VERIFY_MAX_RETRIES} recovery attempts; "
            f"the rollout JSONL has no matching user-turn record after offset "
            f"{rollout_offset}, so delivery is structurally unconfirmed"
        )

    @staticmethod
    def _raw_after_prompt_glyph(raw_line: str) -> str:
        """Return the raw substring after the composer ``›`` glyph (CSI-aware)."""
        plain_chars: list[str] = []
        raw_map: list[int] = []
        j = 0
        while j < len(raw_line):
            if raw_line[j] == "\x1b" and j + 1 < len(raw_line) and raw_line[j + 1] == "[":
                k = j + 2
                while k < len(raw_line) and not (
                    "A" <= raw_line[k] <= "Z" or "a" <= raw_line[k] <= "z"
                ):
                    k += 1
                j = k + 1 if k < len(raw_line) else len(raw_line)
                continue
            plain_chars.append(raw_line[j])
            raw_map.append(j)
            j += 1
        plain = "".join(plain_chars)
        p = plain.rfind("›")
        if p < 0 or p + 1 >= len(raw_map):
            # Glyph at end or missing: body empty / whole line after last char.
            if p >= 0 and p + 1 == len(raw_map):
                return ""
            idx = raw_line.rfind("›")
            return raw_line[idx + 1 :] if idx >= 0 else raw_line
        start = raw_map[p + 1]
        return raw_line[start:]

    @staticmethod
    def _join_composer_segments(
        raw_lines: list[str],
        prompt_idx: int,
        prompt_pos: int,
        segments: list[str],
    ) -> str:
        if not segments:
            return ""

        joined = segments[0]
        for offset, segment in enumerate(segments[1:], start=1):
            prev_raw = raw_lines[prompt_idx + offset - 1]
            width = len(prev_raw)
            prev_visible = prev_raw.rstrip()
            if offset == 1:
                available = max(width - prompt_pos - 2, 0)
                prev_len = len(prev_visible[prompt_pos + 2 :])
            else:
                available = width
                prev_len = len(prev_visible)
            if available >= 20 and prev_len >= available:
                joined += segment
            else:
                joined += "\n" + segment
        return joined

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Codex's final response from terminal output.

        Supports two output formats:
        - Label style: "You ...\\nassistant: response\\n❯" (synthetic/test format)
        - Bullet style: "› user message\\n• response\\n›" (real Codex interactive mode)

        Primary approach: find the last user message and extract everything between
        the end of that line and the next empty idle prompt.
        Fallback: use assistant marker based extraction when no user message is found.
        """
        # Strip ALL terminal escape sequences, not just SGR colour codes. The
        # narrow ANSI_CODE_PATTERN (``\x1b[...m``) leaves cursor-movement (H),
        # erase (K), and scroll CSI sequences in place; codex's TUI emits those
        # heavily, so an SGR-only strip returned raw escape garbage
        # (``[49;2H[K[38;2;...m``) as the "response", failing extraction. Use
        # the shared strip which also normalises \r and column-1 cursor moves to
        # newlines — this is fed a tmux capture-pane render (already laid out),
        # so the line-based extraction below still anchors correctly.
        clean_output = strip_terminal_escapes(script_output)

        # Primary: find last user message, extract response between it and idle prompt.
        # Exclude the Codex TUI footer from user-message matching when detected.
        all_lines = clean_output.splitlines()
        if _has_tui_footer_in_tail(all_lines) or _has_known_composer_placeholder_at_bottom(
            all_lines
        ):
            cutoff_pos = _compute_tui_footer_cutoff(all_lines)
        else:
            cutoff_pos = len(clean_output)
        tui_chrome_detected = cutoff_pos < len(clean_output)

        user_matches = [
            m
            for m in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
            if m.start() < cutoff_pos
        ]

        # Preserve ambiguous composer/footer-shaped content only when it belongs
        # to the final turn. An older pair followed by a later user boundary must
        # not preempt the normal last-user extraction path.
        minimum_prompt_index = (
            clean_output.count("\n", 0, user_matches[-1].start()) if user_matches else 0
        )
        ambiguous_footer = _find_ambiguous_footer_region(
            all_lines, minimum_prompt_index=minimum_prompt_index
        )
        if ambiguous_footer is not None:
            prompt_idx, _footer_idx = ambiguous_footer
            prompt_pos = len("\n".join(all_lines[:prompt_idx]))
            if prompt_idx:
                prompt_pos += 1
            prior_users = [match for match in user_matches if match.start() < prompt_pos]
            if prior_users:
                last_user = prior_users[-1]
                assistant = _find_assistant_marker(clean_output[last_user.start() : prompt_pos])
                if assistant is not None:
                    response_start = last_user.start() + assistant.start()
                    response_text = clean_output[response_start:].strip()
                    response_text = re.sub(
                        r"^(?:assistant|codex|agent)\s*:\s*",
                        "",
                        response_text,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                    return response_text.strip()

        if user_matches:
            last_user = user_matches[-1]

            # Extraction uses a stricter anchor than status detection: skip MCP
            # calls and at least two complete native activity cells before the
            # model's actual reply, while preserving ambiguous compact groups.
            asst_after_user = _find_response_marker(clean_output[last_user.start() :])

            if asst_after_user:
                response_start = last_user.start() + asst_after_user.start()
            else:
                # No assistant marker found; fall back to skipping one line
                user_line_end = clean_output.find("\n", last_user.start())
                if user_line_end == -1:
                    user_line_end = len(clean_output)
                response_start = user_line_end + 1

            # Find extraction boundary: empty idle prompt or TUI footer area.
            # With --no-alt-screen, the TUI footer (› hint + status bar) has no
            # empty idle prompt. Use cutoff_pos as the boundary when TUI is present.
            idle_after = re.search(
                IDLE_PROMPT_STRICT_PATTERN,
                clean_output[response_start:],
                re.MULTILINE,
            )
            if idle_after:
                end_pos = response_start + idle_after.start()
            elif tui_chrome_detected:
                end_pos = cutoff_pos
            else:
                end_pos = len(clean_output)

            response_text = clean_output[response_start:end_pos].strip()

            if response_text:
                # Strip "assistant:" prefix if present (label format)
                response_text = re.sub(
                    r"^(?:assistant|codex|agent)\s*:\s*",
                    "",
                    response_text,
                    count=1,
                    flags=re.IGNORECASE,
                )
                return response_text.strip()

        # Fallback: assistant marker based extraction (no user message found).
        # Filter out "• Called <tool>(...)" MCP tool call markers so we anchor
        # on the model's actual reply, not tool output.
        all_matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )
        matches = []
        for m in all_matches:
            line_end = clean_output.find("\n", m.start())
            if line_end == -1:
                line_end = len(clean_output)
            line = clean_output[m.start() : line_end]
            if re.match(MCP_TOOL_CALL_PATTERN, line):
                continue
            if re.match(SYSTEM_NOTICE_PATTERN, line):
                continue
            matches.append(m)

        if not matches:
            raise ValueError("No Codex response found - no assistant marker detected")

        last_match = matches[-1]
        start_pos = last_match.end()

        idle_after = re.search(
            IDLE_PROMPT_STRICT_PATTERN,
            clean_output[start_pos:],
            re.MULTILINE,
        )
        end_pos = start_pos + idle_after.start() if idle_after else len(clean_output)

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Codex response - no content found")

        return final_answer

    def exit_cli(self) -> str:
        """Get the command to exit Codex CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Codex CLI provider."""
        self._initialized = False
        # Remove the developer_instructions temp file written by _build_codex_command, if any --
        # same convention claude_code.py's own cleanup() uses for its analogous .prompt file.
        # Path comes from _developer_instructions_file_path() (single source of truth shared
        # with _build_codex_command) so the write site and the cleanup site can't drift apart.
        try:
            self._developer_instructions_file_path().unlink(missing_ok=True)
        except OSError:
            pass
