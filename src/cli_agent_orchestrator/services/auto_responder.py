"""Auto-answers blocking TUI dialogs (whitelist-only) from the composited screen.

Consumer: status_monitor's screen-detection path (``_detect_screen``), called
once per detection tick (rising edge + quiescence) for providers that opt in
via ``supports_screen_detection``. See blueprints/auto-responder.md in the
outer cli-subagents repo for the full design.

Scope is intentionally narrow: only dialogs matching a rule the supervisor
(or a human) has authored in ``~/.aws/cli-agent-orchestrator/auto-answers/
<provider>.yaml`` are ever auto-answered. An unmatched screen is suspect only
when dialog markers are close together. The unknown-dialog gate fires only on
WAITING_USER_ANSWER / UNKNOWN / ERROR. This deliberately errs toward silence
for novel dialogs that parse otherwise; the stalled-callback watchdog remains
the fallback.
Usage-reset prompts are dismiss-only by design — no rule may consume ``/usage``
on the user's behalf.

THE line-break trap: terminal width changes where TUI lines wrap, but a TUI
never splits a word mid-token, so the word sequence is stable while the
newlines are not. All matching therefore runs against a CANONICAL form of the
composited screen (see ``canonicalize``): NFKC-normalized, lowercased, every
non-``[a-z0-9]`` character mapped to a space, then whitespace collapsed. This
drops box-drawing walls, block glyphs, arrows, bullets/markers (``> › • ▸``),
numbering dots, and quotes/apostrophes, and makes wrap points irrelevant, so a
rule author writes plain prose and never needs ``\\W+`` in a rule. Rules are
canonicalized the same way on load, so both sides of every match share one
domain. Matching never runs against raw lines.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

# F516 commit 1: injectable monotonic clock seam (precedent: question_state.py
# ``_clock``). Deterministic tests patch ``auto_responder._clock``; production
# reads the real monotonic clock. New D4/D5 code paths read through this seam so
# the 1/2/4/8s backoff is observable without wall-clock sleeps.
_clock: Callable[[], float] = time.monotonic
# F597 #454 pt2: injectable sleep seam so settle/re-arm delays are observable in
# tests without real wall-clock waits (mirrors the ``_clock`` monotonic seam).
_clock_sleep: Callable[[float], None] = time.sleep


def _ar_display_name(terminal_id: str, metadata: Dict[str, Any]) -> str:
    """F172: Return display form for auto-responder messages."""
    profile = metadata.get("agent_profile") or metadata.get("profile")
    if profile:
        return f"{profile}-{terminal_id}"
    return terminal_id


AUTO_ANSWER_DIR = CAO_HOME_DIR / "auto-answers"
AUTO_ANSWER_LOG_DIR = CAO_HOME_DIR / "logs" / "auto-answers"

RETRY_MAX = 3
RETRY_DELAY_S = 1.0
COOLDOWN_S = 5.0
KEY_DELAY_S = 0.1
# F597 #454 pt2 (a) SETTLE: before the FIRST send of an episode, require the
# matched frame to be byte-stable across two captures this far apart, so the
# dialog is fully painted and codex's TUI input handler is armed before keys are
# sent. The 5x-in-1.6s no-op stall was Enters landing before the handler existed.
SETTLE_INTERVAL_S = 0.5
# F597 #454 pt2 (c) RE-ARM: retry-exhaustion must NOT latch off forever. While
# the same dialog signature persists, re-fire on this backoff schedule (seconds),
# then every REARM_STEADY_S, capped at REARM_CAP_S total since exhaustion.
REARM_BACKOFF_S = (5.0, 15.0, 45.0)
REARM_STEADY_S = 60.0
REARM_CAP_S = 600.0
UNKNOWN_DIALOG_PUSH_FLOOR_S = 300.0
UNKNOWN_DIALOG_PAYLOAD_CHARS = 600
DIALOG_PROXIMITY_CHARS = 200
# F354: geometry-derived — grok trust dialog question renders 16–17 rows above
# pane bottom (above ASCII banner); must reach it (≥17) while staying below the
# M2/F55 quoted-prose suppression boundary (≤23).
DIALOG_REGION_LINES = 20

# F86: Known permission/AskUserQuestion patterns — these are interactive prompts
# from the host CLI (claude_code, kiro) that should NOT trigger unknown-dialog
# escalation. The auto-responder returns WAITING_USER_ANSWER without pushing.
_PERMISSION_PROMPT_PATTERNS = (
    re.compile(r"Select an option", re.IGNORECASE),
    re.compile(r"Use arrow keys", re.IGNORECASE),
    re.compile(r"[↑↓].*to navigate.*[↵⏎].*to select", re.IGNORECASE),
    re.compile(r"Yes, I trust this folder"),
    re.compile(r"Yes, I accept"),
    re.compile(r"Allow (?:once|always)", re.IGNORECASE),
    re.compile(r"\[y/n(?:/t)?\]"),
)

# Seed rule files, created only if absent -- never overwritten. Keys are the
# provider filename (``<provider>.yaml``); values are the verbatim YAML from
# the blueprint.
SEED_RULES: Dict[str, str] = {
    "codex.yaml": """\
# F597 #454: rules match against a CANONICAL form of the screen, in TWO domains.
#   * match_mode: contains → FULL canonical (NFKC, lowercased, every
#     non-[a-z0-9] char -> space, whitespace collapsed). Write plain prose;
#     glyphs, box walls, bullets, quotes, apostrophes and wrap points are all
#     folded away, so no rule needs \\W+.
#   * match_mode: regex → LIGHT canonical (NFKC, lowercased, whitespace
#     collapsed ONLY — PUNCTUATION PRESERVED), matched case-insensitively. Write
#     lowercase-friendly patterns; punctuation your regex needs (?, !, version
#     dots, ->, glyph anchors) survives, and \\d+ / word tokens still match.
- name: codex-usage-resets
  enabled: true
  match_mode: regex
  question: 'You have \\d+ usage limit resets available'
  options: ["Yes, continue", "No, quit"]   # all must appear (normalized)
  answer: wait                              # human-gated: never auto-spend usage-limit resets
- name: codex-trust-dir
  enabled: true
  match_mode: contains
  question: "Do you trust the contents of this directory?"
  options: ["Yes, continue", "No, quit"]
  answer: ["Enter"]
- name: codex-trust-dir-subdir
  enabled: true
  match_mode: contains
  question: "subdirectory of a Git project. Trusting will apply to the repository root"
  options: ["Press enter to continue"]
  answer: ["Enter"]   # worktree seats: option 1 (Yes, continue) is pre-selected
- name: codex-resume-working-directory
  enabled: true
  match_mode: contains
  question: "Choose working directory to resume this session"
  options: ["Press enter"]
  answer: ["Enter"]
""",
}

# Generic unknown-dialog heuristic (any provider): numbered options like
# "1. Yes, continue" plus a "press enter to continue"-style footer. F597 #454:
# these run against the CANONICAL string, where "1." has been folded to "1 "
# (the dot became a space), so the numbered-option pattern is "<digit> <word>".
_NUMBERED_OPTION_PATTERN = re.compile(r"\b[1-3]\s+\S")
_PRESS_ENTER_PATTERN = re.compile(r"press enter", re.IGNORECASE)
# F597 #454: canonical-domain equivalent of codex's WAITING_PROMPT_PATTERN
# (providers/codex.py). The screen is already lowercased and punctuation-folded
# here, so "Approve command? y/n" reads "approve command y n": an approve/allow
# lead, then a yes/no affordance ("y n"/"yes no"/"yes"/"no") later in the region.
_CANONICAL_APPROVAL_PATTERN = re.compile(r"^(?:approve|allow)\b.*\b(?:y n|yes no|yes|no)\b")


def canonicalize(text: str) -> str:
    """Fold ``text`` into the canonical match domain (F597 #454).

    Steps: NFKC-normalize → lowercase → map every character that is not
    ``[a-z0-9]`` to a space → collapse whitespace runs → strip. The effect is
    that all TUI chrome (box-drawing walls, block elements, arrows, bullets and
    prompt markers like ``> › • ▸``, numbering dots, quotes and apostrophes) is
    reduced to word boundaries, and where a line wraps no longer matters. A
    58-col bordered card whose question wraps across a ``│`` wall canonicalizes
    to the same string as the same prompt rendered as plain unwalled text, so a
    plain-prose ``contains`` rule matches both. Word/digit tokens survive intact,
    so ``regex`` rules like ``you have \\d+ usage limit resets`` still match.

    This is applied to BOTH the composited screen (via ``normalize_screen``) and
    every rule's ``question``/``options`` (on load), so both sides of a match
    share one domain. Deliberately NOT fuzzy/semantic: it is a pure, reversible
    character fold, nothing more.
    """
    folded = unicodedata.normalize("NFKC", text).lower()
    folded = "".join(ch if ("a" <= ch <= "z" or "0" <= ch <= "9") else " " for ch in folded)
    return " ".join(folded.split())


def canonicalize_light(text: str) -> str:
    """Light canonical fold for ``regex`` rules (F597 #454 B2).

    Steps: NFKC-normalize → lowercase → collapse whitespace runs → strip.
    Crucially, PUNCTUATION AND GLYPHS ARE PRESERVED (unlike the full
    ``canonicalize``, which maps every non-``[a-z0-9]`` char to a space). This is
    the domain ``regex`` rules match against: their patterns legitimately depend
    on punctuation the full fold would erase — e.g. ``askuserquestion`` glyph
    anchors, ``codex-ratelimit-model-switch``'s ``->``, ``codex-update-available``'s
    version dots and ``!``. NFKC still normalizes fullwidth/compatibility forms
    and lowercasing + IGNORECASE keep case-insensitivity, while wrap/whitespace
    variance is collapsed so a wrapped card still matches. Applied to BOTH the
    screen (``normalize_screen_light``) and each regex rule's pattern source at
    load, so both sides share this domain.
    """
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def normalize_screen(lines: List[str]) -> str:
    """Flatten composited screen lines into the FULL canonical match domain.

    Joins ``lines`` and returns their ``canonicalize`` fold — the domain
    ``contains`` rules match against, plus the domain digests / diagnostics use.
    Never match against raw ``lines``; the raw text (for diagnostics) is
    recoverable by joining ``DialogRegion.rows``.
    """
    return canonicalize(" ".join(lines))


def normalize_screen_light(lines: List[str]) -> str:
    """Flatten composited screen lines into the LIGHT canonical domain.

    The punctuation-preserving companion to ``normalize_screen`` that ``regex``
    rules match against (F597 #454 B2).
    """
    return canonicalize_light(" ".join(lines))


@dataclass(frozen=True)
class DialogRegion:
    rows: tuple[str, ...]
    normalized: str
    # F516 D2(i)/D5: pending-fire digests, both normalized-domain (r5-B1) and
    # BOTH defaulted so the existing two-positional construction keeps working
    # (r5-S1, r4-B2 split). ``settle_digest`` is what D2(i)'s settle compares and
    # is re-seeded each _verify_and_retry iteration; ``consume_digest`` is set
    # once at match time, never re-seeded, and is what D5's consume gate reads.
    settle_digest: str = ""
    consume_digest: str = ""
    # F597 #454 B2: the LIGHT canonical form of the same rows (punctuation
    # preserved). ``regex`` rules match against this; ``contains`` rules and all
    # digests/diagnostics use ``normalized`` (full). Defaulted so the existing
    # two-positional ``DialogRegion(rows, normalized)`` construction keeps
    # working; ``dialog_region`` populates it from the same rows.
    normalized_light: str = ""

    def with_digests(self, settle: str, consume: str) -> "DialogRegion":
        """Return a copy carrying the two pending-fire digests."""
        return DialogRegion(
            rows=self.rows,
            normalized=self.normalized,
            settle_digest=settle,
            consume_digest=consume,
            normalized_light=self.normalized_light,
        )


def _digest_normalized(normalized: str) -> str:
    """Stable digest over the whitespace-collapsed region string.

    F516 D2(i) digest-domain rule (r5-B1): digests are ALWAYS computed over
    ``DialogRegion.normalized`` (the flattened string), never over ``rows`` —
    the pyte-composite and tmux-viewport capture paths pad rows differently, so
    a rows-domain digest never matches on a static pane.
    """
    import hashlib

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RuleMatchVerdict:
    """F516 D3p: result of ``AutoResponder.match_verdict`` for a consult caller.

    Metadata-free and privates-free so it can cross the draft_guard / wait-site
    boundary without leaking responder internals.
    """

    rule_name: str
    region_digest: str  # normalized-domain digest (D2(i) rule)
    matched_region_rows: tuple[str, ...]


def _drop_chrome_rows(
    rows: List[str], chrome_patterns: Optional[List["re.Pattern[str]"]]
) -> List[str]:
    """F530: drop provider footer/chrome rows so the dialog tail is measured in
    CONTENT rows only. A blocking modal is superseded only by rows the agent
    emitted after it, never by its own spinner/composer/status bar. Genuine
    output matches none of these patterns, so real scrollback below a dialog
    still pushes it out (F55 suppression preserved)."""
    if not chrome_patterns:
        return rows
    return [row for row in rows if not any(pat.search(row) for pat in chrome_patterns)]


def dialog_region(
    screen: List[str], chrome_patterns: Optional[List["re.Pattern[str]"]] = None
) -> DialogRegion:
    """Return the rendered dialog-bearing tail without normalizing provider input.

    F530: when ``chrome_patterns`` is supplied, provider footer/chrome rows are
    dropped BEFORE the ``DIALOG_REGION_LINES`` tail is sliced, so a still-active
    modal is not pushed out of the tail by its own footer. The tail
    ``rows``/``normalized`` are otherwise unchanged, so all 6 ``.matches()`` call
    sites keep matching against ``.normalized`` (the F55 AST invariant holds).
    """
    filtered = _drop_chrome_rows(screen, chrome_patterns)
    end = len(filtered)
    while end and not filtered[end - 1].strip():
        end -= 1
    rows = tuple(filtered[max(0, end - DIALOG_REGION_LINES) : end])
    row_list = list(rows)
    return DialogRegion(
        rows=rows,
        normalized=normalize_screen(row_list),
        normalized_light=normalize_screen_light(row_list),
    )


TerminalIncarnation = tuple[int, int, str, str]


def _rules_path(provider: str) -> Path:
    """Return the rule file path for ``provider``, seeding it if absent.

    Never overwrites an existing file, even an empty or malformed one.
    """
    AUTO_ANSWER_DIR.mkdir(parents=True, exist_ok=True)
    path = AUTO_ANSWER_DIR / f"{provider}.yaml"
    seed = SEED_RULES.get(f"{provider}.yaml")
    if seed and not path.exists():
        path.write_text(seed, encoding="utf-8")
    return path


def _chrome_patterns_for_class(
    provider_cls: type,
) -> Optional[List["re.Pattern[str]"]]:
    """F530: obtain a provider's chrome_row_patterns without constructing a live
    terminal provider (used by the diagnostic CLI). Reads the class-level
    ``_CHROME_ROW_PATTERNS`` when present; falls back to None."""
    patterns = getattr(provider_cls, "_CHROME_ROW_PATTERNS", None)
    if patterns:
        return list(patterns)
    return None


def diagnose_rules(
    provider_name: str, lines: List[str], provider_cls: type | None = None
) -> Dict[str, Any]:
    """F530 diagnosability: compute the dialog region (unfiltered + chrome-
    filtered match region) for ``lines`` and each rule's verdict, WITHOUT sending
    any keys or mutating state. Powers ``cao auto-answers test``.

    Returns a plain dict: ``region`` (tail rows + normalized), ``match_region``
    (chrome-filtered normalized), and ``rules`` (name, enabled, match_mode,
    matched bool, reject reason)."""
    chrome_patterns = _chrome_patterns_for_class(provider_cls) if provider_cls else None
    region = dialog_region(lines)
    match_region = dialog_region(lines, chrome_patterns) if chrome_patterns else region
    rule_reports = []
    for rule in _store.get_rules(provider_name):
        reason = rule.reject_reason(match_region)
        rule_reports.append(
            {
                "name": rule.name,
                "enabled": rule.enabled,
                "match_mode": rule.match_mode,
                "answer": rule.answer,
                "matched": reason is None,
                "reject_reason": reason,
            }
        )
    return {
        "provider": provider_name,
        "chrome_filtered": chrome_patterns is not None,
        "region_rows": list(region.rows),
        "region_normalized": region.normalized,
        "match_normalized": match_region.normalized,
        "rules": rule_reports,
    }


@dataclass
class Rule:
    name: str
    enabled: bool
    match_mode: str
    question: str
    options: List[str]
    answer: Any  # list[str] of tmux special-key names, or the literal "wait"
    # F597 #454: canonical match forms, computed in __post_init__ (not init args).
    _canon_question: str = field(init=False, repr=False, compare=False, default="")
    _canon_options: tuple[str, ...] = field(init=False, repr=False, compare=False, default=())
    _regex: "re.Pattern[str] | None" = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        # F597 #454 B2: precompute the canonical match forms once on load so both
        # sides of every match share ONE domain. TWO domains (gate decision):
        #   * ``contains`` → the FULL canonical domain (``canonicalize``): NFKC →
        #     lower → non-[a-z0-9]→space → collapse. Glyphs/punctuation/wrap are
        #     folded away so plain-prose rules match a bordered card. The question
        #     and options are canonicalized here.
        #   * ``regex`` → the LIGHT canonical domain (``canonicalize_light``): NFKC
        #     → lower → whitespace-collapse ONLY, PUNCTUATION PRESERVED, compiled
        #     IGNORECASE. Shipped regex rules legitimately depend on punctuation
        #     the full fold erases (askuserquestion glyphs, ``->``, version dots,
        #     ``!``), so they must NOT be matched against the full domain. The
        #     pattern is applied verbatim to the light screen string. Options are
        #     still canonicalized against the FULL domain (plain-prose substrings).
        # ``question``/``options`` keep their authored text for reject reasons.
        if self.match_mode == "regex":
            self._canon_question = self.question
            try:
                self._regex = re.compile(self.question, re.IGNORECASE)
            except re.error:
                logger.warning(
                    "auto-responder: rule %r has an invalid regex question; disabling",
                    self.name,
                )
                self._regex = None
        else:
            self._canon_question = canonicalize(self.question)
            self._regex = None
        self._canon_options = tuple(canonicalize(opt) for opt in self.options)

    @property
    def is_wait(self) -> bool:
        return self.answer == "wait"

    @property
    def body_hash(self) -> str:
        """F516 D5: stable hash over question+options+answer+match_mode (NOT
        ``enabled``). Keys the consume-digest and cooldown state so a changed
        rule body resets both; toggling ``enabled`` alone does not."""
        import hashlib
        import json

        payload = json.dumps(
            [self.match_mode, self.question, list(self.options), self.answer],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _domains(region: "DialogRegion | str") -> tuple[str, str]:
        """Resolve (full, light) canonical strings from a region or bare string.

        F597 #454 B2: rule matching needs BOTH domains. Callers pass a
        ``DialogRegion`` (carrying both); a bare ``str`` is treated as the FULL
        canonical form with light defaulting to it (back-compat for the few
        diagnostic/string callers — only matters for regex rules, which the
        region-passing callers always feed correctly)."""
        if isinstance(region, DialogRegion):
            return region.normalized, (region.normalized_light or region.normalized)
        return region, region

    def matches(self, region: "DialogRegion | str") -> bool:
        if not self.enabled:
            return False
        full, light = self._domains(region)
        if self.match_mode == "regex":
            if self._regex is None or not self._regex.search(light):
                return False
        else:
            if self._canon_question not in full:
                return False
        return all(opt in full for opt in self._canon_options)

    def reject_reason(self, region: "DialogRegion | str") -> Optional[str]:
        """F530 diagnosability: return WHY this rule does NOT match, or None when
        it matches. Names the failing field so a stalled dialog is diagnosable
        from the decisions log / CLI without a supervisor.

        F597 #454 B2: ``contains`` is checked against the FULL canonical domain,
        ``regex`` against the LIGHT (punctuation-preserving) domain. Reasons name
        the AUTHORED text so the log is human-readable: ``disabled``,
        ``question(regex)``, ``question(contains)``, ``option[<opt>]``.
        """
        if not self.enabled:
            return "disabled"
        full, light = self._domains(region)
        if self.match_mode == "regex":
            if self._regex is None or not self._regex.search(light):
                return "question(regex)"
        elif self._canon_question not in full:
            return "question(contains)"
        for authored, canon in zip(self.options, self._canon_options):
            if canon not in full:
                return f"option[{authored}]"
        return None


@dataclass(frozen=True)
class AutoResponderDecision:
    normalized: str
    lines: tuple[str, ...]
    status: TerminalStatus
    raw_classification: object | None
    fresh_token: tuple[str, float]


class _RuleStore:
    """Per-provider rule file, hot-reloaded on mtime change."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[str, tuple] = {}  # provider -> (mtime, rules)

    def get_rules(self, provider: str) -> List[Rule]:
        path = _rules_path(provider)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        with self._lock:
            cached = self._cache.get(provider)
            if cached is not None and cached[0] == mtime:
                return cached[1]
        rules = self._load(path)
        with self._lock:
            self._cache[provider] = (mtime, rules)
        return rules

    @staticmethod
    def _load(path: Path) -> List[Rule]:
        import yaml

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except Exception:
            logger.exception("auto-responder: failed to parse rules at %s", path)
            return []
        if not isinstance(raw, list):
            logger.warning("auto-responder: rules file %s is not a list; ignoring", path)
            return []

        rules: List[Rule] = []
        for item in raw:
            try:
                rules.append(
                    Rule(
                        name=item["name"],
                        enabled=item.get("enabled", True),
                        match_mode=item.get("match_mode", "contains"),
                        question=item["question"],
                        options=list(item.get("options", []) or []),
                        answer=item.get("answer", "wait"),
                    )
                )
            except (KeyError, TypeError):
                logger.warning("auto-responder: skipping malformed rule in %s: %r", path, item)
        return rules


_store = _RuleStore()


@dataclass
class _RuleState:
    cooldown_until: float = field(default=0.0)


@dataclass
class _UnknownDialogState:
    episode_open: bool = False
    non_dialog_ticks: int = 0
    last_push_at: float = field(default=-UNKNOWN_DIALOG_PUSH_FLOOR_S)


VETO_STREAK_THRESHOLD = 5
VETO_STREAK_PUSH_FLOOR_S = 300.0


@dataclass
class _VetoStreakState:
    """F516 D6: consecutive match-but-vetoed eval counter with its own push
    floor. Reset edges: fire, pane output, episode close."""

    count: int = 0
    episode_open: bool = False
    last_push_at: float = field(default=-VETO_STREAK_PUSH_FLOOR_S)


@dataclass
class _RearmState:
    """F597 #454 pt2 (c): tracks a retry-exhausted dialog so the responder can
    re-fire on a bounded backoff instead of latching off forever.

    ``signature`` is the region consume-digest that exhausted; ``exhausted_at``
    is the monotonic time the latch was set; ``next_at`` is when the next re-fire
    is due; ``attempts`` counts re-arm fires so far (indexes REARM_BACKOFF_S,
    then REARM_STEADY_S). A changed signature or an eval that no longer matches
    clears this."""

    signature: str
    exhausted_at: float
    next_at: float
    attempts: int = 0


class AutoResponder:
    """Whitelist-only engine: fires ``answer`` keys for matched rules,
    surfaces everything else as WAITING_USER_ANSWER.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rule_state: Dict[tuple, _RuleState] = {}
        self._unknown_state: Dict[str, _UnknownDialogState] = {}
        self._wait_rule_active: Dict[str, tuple[str, float]] = {}
        self._retry_exhausted: set[str] = set()
        self._terminal_generation: Dict[str, int] = {}
        self._exit_suppressed: set[str] = set()
        # F516 D5: consumed pre-fire region digests, keyed
        # (terminal, rule-name, rule-body-hash). Recorded AFTER _verify_and_retry
        # confirms the dialog cleared; the barrier refuses to re-fire a digest
        # already consumed. Purged in clear_terminal by terminal prefix.
        self._consumed_digests: Dict[tuple, str] = {}
        # F516 D6: last-2 DialogRegion captures per terminal (scroll-exclusion
        # history), the per-terminal cached banner verdict for the current eval,
        # and the veto-streak state. All purged in clear_terminal.
        self._region_history: Dict[str, list[DialogRegion]] = {}
        self._prefilter_verdict: Dict[str, bool] = {}
        self._veto_streak: Dict[str, _VetoStreakState] = {}
        # F597 #454: per-terminal set of canonical-region hashes already dumped
        # in full to the decisions log. The first no_match on a distinct region
        # logs the FULL canonical region (2 KB cap) so the next no-fire class is
        # diagnosable without a live capture; repeats are deduped to one line.
        # Purged in clear_terminal.
        # F597 #454 pt2: per-terminal settle + re-arm state. ``_settled_signatures``
        # records the region signatures whose FIRST send this episode already
        # passed the two-capture settle gate (so we settle once, not every fire).
        # ``_rearm_state`` tracks a latched retry-exhausted dialog so it can be
        # re-fired on a bounded backoff instead of latching off forever. Both are
        # purged in clear_terminal and reset when the dialog signature changes.
        self._settled_signatures: Dict[str, set[str]] = {}
        self._rearm_state: Dict[str, _RearmState] = {}
        self._logged_region_hashes: Dict[str, set[str]] = {}

    def _waiting_gate_locked(self, terminal_id: str) -> str | tuple[str, str] | None:
        state = self._unknown_state.get(terminal_id)
        if state is not None and state.episode_open:
            return "unknown_dialog"
        wait_state = self._wait_rule_active.get(terminal_id)
        if wait_state is not None:
            return ("wait_rule", wait_state[0])
        if terminal_id in self._retry_exhausted:
            return "retry_exhausted"
        return None

    @staticmethod
    def _gate_transition(
        terminal_id: str,
        before: str | tuple[str, str] | None,
        after: str | tuple[str, str] | None,
    ) -> None:
        if before is None or after is not None:
            return
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        inbox_service.schedule_delivery_wake(terminal_id)

    def waiting_gate(self, terminal_id: str) -> str | tuple[str, str] | None:
        with self._lock:
            return self._waiting_gate_locked(terminal_id)

    @staticmethod
    def _chrome_patterns(provider: Any) -> Optional[List["re.Pattern[str]"]]:
        """F530: the provider's footer/chrome row patterns, or None. Never
        raises — a provider without the hook (or a raising one) yields None so
        ``dialog_region`` behaves exactly as before for that provider."""
        try:
            patterns = provider.chrome_row_patterns()
            return patterns or None
        except Exception:
            return None

    @staticmethod
    def _reject_summary(provider_name: str, region: "DialogRegion") -> str:
        """F530 diagnosability: for a 'no_rule_matched' eval, name WHY each rule
        rejected (rule + failing field) plus the first 80 chars of the CANONICAL
        window that was matched against. Written into the decisions log ``extra``
        field so this whole class of no-fire is diagnosable without a supervisor.
        The full canonical region is dumped once per distinct region by
        ``_log_no_match_region`` (F597 #454). Takes the region so each rule sees
        the correct domain (F597 #454 B2: contains→full, regex→light)."""
        parts: List[str] = []
        for rule in _store.get_rules(provider_name):
            reason = rule.reject_reason(region)
            if reason is not None:
                parts.append(f"{rule.name}:{reason}")
        window = region.normalized[:80]
        rejects = " ".join(parts) if parts else "no-rules"
        return f"rejects=[{rejects}] window={window!r}"

    def _log_no_match_region(self, terminal_id: str, canonical: str) -> None:
        """F597 #454: on the FIRST no_match for a distinct canonical region,
        dump the FULL canonical region (2 KB cap) to the decisions log so the
        next no-fire is diagnosable from the log alone — no live capture needed.

        Deduped per (terminal, region-hash): a static stalled pane re-evaluates
        the same region every tick, so only the first sighting is dumped in full.
        Best-effort; never raises into the detection tick."""
        if not canonical:
            return
        import hashlib

        region_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        with self._lock:
            seen = self._logged_region_hashes.setdefault(terminal_id, set())
            if region_hash in seen:
                return
            seen.add(region_hash)
        payload = canonical[:2048]
        self._log_decision(
            terminal_id,
            "no_match",
            "region_dump",
            extra=f"region_hash={region_hash} canonical={payload!r}",
        )

    def match_verdict(
        self, provider_name: str, lines: List[str], terminal_id: str | None = None
    ) -> "RuleMatchVerdict | None":
        """F516 D3p: whitelist text-match verdict for a consult caller.

        Handed pre-captured ``lines`` (this never captures itself). Computes the
        dialog region and the normalized whitelist match against the provider's
        rules and returns a metadata-free ``RuleMatchVerdict`` for the first
        matching non-wait rule, or ``None`` when nothing matches. Provider dialog
        classification is a SEPARATE consult (consult (a)) — this never calls
        ``_classify_region``.

        ``terminal_id`` (optional) enables the D6 banner path: the cached
        scroll-exclusion mark is honored ONLY while this consult's fresh region
        still differs from the newest history entry (a still-scrolling banner),
        in which case ``None`` is returned so the consult does NOT defer (C4).
        """
        terminal_id_key = terminal_id
        region = dialog_region(lines)
        if not region.normalized:
            return None
        for rule in _store.get_rules(provider_name):
            if not rule.matches(region):
                continue
            # D6 banner path (r8-B/r9-S1): the cached banner-mark is HONORED ONLY
            # WHILE THE REGION IS STILL MOVING — this consult's fresh region
            # still differs from the newest history entry, compared in the
            # NORMALIZED domain. A STATIC region is dialog-eligible whatever the
            # mark → return the verdict and let the consult DEFER.
            #   static + match            → defer (return verdict)
            #   moving + banner-mark      → None  (C4: don't stall on a banner)
            #   moving + no-mark/no-cache → defer (return verdict)
            with self._lock:
                banner_mark = self._prefilter_verdict.get(terminal_id_key)
                history = self._region_history.get(terminal_id_key, []) if terminal_id_key else []
                newest = history[-1] if history else None
            still_moving = newest is not None and _digest_normalized(
                newest.normalized
            ) != _digest_normalized(region.normalized)
            if banner_mark and still_moving:
                return None
            return RuleMatchVerdict(
                rule_name=rule.name,
                region_digest=_digest_normalized(region.normalized),
                matched_region_rows=region.rows,
            )
        return None

    def record_published_status(self, terminal_id: str, status: TerminalStatus) -> None:
        try:
            with self._lock:
                before = self._waiting_gate_locked(terminal_id)
                if status != TerminalStatus.WAITING_USER_ANSWER:
                    self._retry_exhausted.discard(terminal_id)
                    # F597 #454 pt2 (c): the terminal left WAITING (real progress
                    # or a fire took) — drop the re-arm latch/settle state too.
                    self._rearm_state.pop(terminal_id, None)
                    self._settled_signatures.pop(terminal_id, None)
                after = self._waiting_gate_locked(terminal_id)
            self._gate_transition(terminal_id, before, after)
        except BaseException:
            try:
                logger.warning(
                    "auto-responder: failed to record published status for %s",
                    terminal_id,
                    exc_info=True,
                )
            except BaseException:
                pass

    def clear_terminal(self, terminal_id: str) -> None:
        """Clear engine state without acquiring the non-reentrant delivery lock."""
        with self._lock:
            self._terminal_generation[terminal_id] = (
                self._terminal_generation.get(terminal_id, 0) + 1
            )
            self._wait_rule_active.pop(terminal_id, None)
            self._retry_exhausted.discard(terminal_id)
            self._unknown_state.pop(terminal_id, None)
            self._exit_suppressed.discard(terminal_id)
            self._region_history.pop(terminal_id, None)
            self._prefilter_verdict.pop(terminal_id, None)
            self._veto_streak.pop(terminal_id, None)
            self._logged_region_hashes.pop(terminal_id, None)
            self._settled_signatures.pop(terminal_id, None)
            self._rearm_state.pop(terminal_id, None)
            for key in [key for key in self._rule_state if key[0] == terminal_id]:
                self._rule_state.pop(key, None)
            for key in [key for key in self._consumed_digests if key[0] == terminal_id]:
                self._consumed_digests.pop(key, None)

    def mark_exit_suppress(self, terminal_id: str) -> None:
        """Suppress all on_screen effects for a terminal that is exiting (Layer B).

        Called from exit_terminal_cli after successfully sending the exit command.
        Cleared on re-register (rebind) or clear_terminal (delete).
        """
        with self._lock:
            self._exit_suppressed.add(terminal_id)

    def unmark_exit_suppress(self, terminal_id: str) -> None:
        """Remove exit suppression, e.g. after rebind re-register.

        Allows a rebound terminal to resume receiving auto-responder scans.
        """
        with self._lock:
            self._exit_suppressed.discard(terminal_id)

    def is_exit_suppressed(self, terminal_id: str) -> bool:
        """Check if a terminal is exit-suppressed (for testing)."""
        with self._lock:
            return terminal_id in self._exit_suppressed

    def _clear_wait_rule(self, terminal_id: str) -> None:
        with self._lock:
            before = self._waiting_gate_locked(terminal_id)
            self._wait_rule_active.pop(terminal_id, None)
            after = self._waiting_gate_locked(terminal_id)
        self._gate_transition(terminal_id, before, after)

    def on_screen(
        self, terminal_id: str, provider: Any, lines: List[str]
    ) -> Optional[TerminalStatus]:
        """Inspect the composited screen; return a status override or None.

        None means "no opinion" -- the caller should fall through to normal
        provider detection. Never raises.
        """
        if os.environ.get("CAO_AUTO_ANSWER", "true").lower() == "false":
            self._clear_wait_rule(terminal_id)
            return None
        capabilities = provider.capabilities
        if not capabilities.supports_screen_detection:
            self._clear_wait_rule(terminal_id)
            return None
        try:
            return self._on_screen(terminal_id, provider, lines)
        except Exception:
            self._clear_wait_rule(terminal_id)
            logger.exception("auto-responder: error handling terminal %s", terminal_id)
            return None

    def _on_screen(
        self, terminal_id: str, provider: Any, lines: List[str]
    ) -> Optional[TerminalStatus]:
        from cli_agent_orchestrator.clients.database import get_terminal_metadata
        from cli_agent_orchestrator.services.session_env import get_session_env

        # Layer B (F115): suppress all on_screen effects after exit.
        with self._lock:
            if terminal_id in self._exit_suppressed:
                self._log_decision(terminal_id, "not_running", "exit_suppressed")
                return None

        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            self._clear_wait_rule(terminal_id)
            self._log_decision(terminal_id, "not_running", "no_metadata")
            return None

        # Per-terminal opt-out via ``cao launch --env CAO_AUTO_ANSWER=false``.
        # Plumbing is session-scoped (env_vars are persisted per tmux session,
        # not per terminal -- see services/session_env.py), which is an exact
        # match for the common case of one worker per session but degrades to
        # session-wide for multi-window sessions.
        session_env = get_session_env(metadata["tmux_session"])
        if session_env.get("CAO_AUTO_ANSWER", "true").lower() == "false":
            self._clear_wait_rule(terminal_id)
            self._log_decision(terminal_id, "not_running", "auto_answer_disabled")
            return None

        if self._find_supervisor(metadata["tmux_session"]) == terminal_id:
            self._clear_wait_rule(terminal_id)
            logger.debug("auto-responder: skipping supervisor terminal %s", terminal_id)
            self._log_decision(terminal_id, "not_running", "is_supervisor")
            return None

        provider_name = metadata["provider"]
        chrome_patterns = self._chrome_patterns(provider)
        # Classifier, D6 history/banner, and the unknown-dialog shape heuristic
        # all read the UNFILTERED bottom-anchored tail (unchanged behavior). Only
        # whitelist RULE MATCHING uses the chrome-filtered tail (F530 layer 1),
        # so a modal is not pushed out of the tail by its own footer chrome while
        # the classifier still sees the true bottom of the pane.
        region = dialog_region(lines)
        match_region = dialog_region(lines, chrome_patterns) if chrome_patterns else region
        if not region.normalized:
            self._clear_wait_rule(terminal_id)
            self._log_decision(terminal_id, "not_running", "empty_region")
            return None
        supplied_status = self._classify_region(terminal_id, provider, region)
        incarnation = self._snapshot_incarnation(terminal_id, metadata)
        # D6 (r4-B1): push the eval-entry region into the last-2 history EXACTLY
        # ONCE per eval and compute this eval's scroll-exclusion (banner) verdict,
        # cached per-terminal for D3's match_verdict. had_history reflects the
        # state BEFORE this push (the no-history HOLD needs the prior state).
        with self._lock:
            had_history = bool(self._region_history.get(terminal_id))
        banner_marked = self._push_region_history(terminal_id, region)

        for rule in _store.get_rules(provider_name):
            if not rule.matches(match_region):
                continue
            if rule.is_wait:
                fresh = self._capture_for_analysis(metadata, lines, terminal_id, provider)
                if fresh is None:
                    self._log_decision(
                        terminal_id, "no_match", "wait_rule_capture_failed", rule.name
                    )
                    return None
                fresh_region = self._region_from_capture(fresh, self._chrome_patterns(provider))
                if not rule.matches(fresh_region):
                    self._log_decision(
                        terminal_id, "no_match", "wait_rule_fresh_mismatch", rule.name
                    )
                    return None
                with self._lock:
                    self._wait_rule_active[terminal_id] = (rule.name, time.monotonic())
                self._log_decision(terminal_id, "matched", "wait_rule_active", rule.name)
                # D4: a human-gated wait rule holds the terminal; re-arm a tick.
                self._request_detection_retry(terminal_id)
                return TerminalStatus.WAITING_USER_ANSWER
            if self._busy_veto(supplied_status):
                self._log_decision(terminal_id, "no_match", "busy_veto", rule.name)
                # D4: a busy-veto on a MATCHED rule is a vetoed eval — request a
                # retry (r3-B4). D6: it also counts toward the veto streak.
                self._note_veto_streak(terminal_id, metadata, incarnation)
                self._request_detection_retry(terminal_id)
                continue
            # D6 fire-path decision for an UNCORROBORATED match (classifier not
            # WAITING). The D2 classifier fast-path DISCHARGES all of this: a
            # WAITING classification makes the match eligible on the FIRST eval.
            #   - no history (first eval of a fresh region): HOLD, request one
            #     D4 retry — the second capture decides (r3-B5).
            #   - has history + banner-marked (region still moving = a scrolled
            #     banner): SUPPRESS (C4 structural fix) — no keys, count a veto.
            #   - has history + not banner-marked (static real dialog): eligible
            #     → fall through and fire.
            if supplied_status != TerminalStatus.WAITING_USER_ANSWER:
                if not had_history:
                    self._log_decision(terminal_id, "no_match", "no_history_hold", rule.name)
                    self._clear_wait_rule(terminal_id)
                    self._note_veto_streak(terminal_id, metadata, incarnation)
                    self._request_detection_retry(terminal_id)
                    return None
                if banner_marked:
                    self._log_decision(terminal_id, "no_match", "scroll_excluded", rule.name)
                    self._clear_wait_rule(terminal_id)
                    self._note_veto_streak(terminal_id, metadata, incarnation)
                    self._request_detection_retry(terminal_id)
                    return None
            self._clear_wait_rule(terminal_id)
            match_digest = _digest_normalized(match_region.normalized)
            # F597 #454 pt2 (c): if this terminal previously EXHAUSTED its retry
            # budget on this same dialog, it is latched at WAITING. Do NOT latch
            # forever — re-fire on a bounded backoff while the signature persists.
            rearm = self._rearm_gate(terminal_id, match_digest, rule)
            if rearm == "hold":
                # Latched and not yet due for a re-arm fire — keep a tick armed so
                # a silent pane still re-evaluates when the backoff elapses.
                self._request_detection_retry(terminal_id)
                return TerminalStatus.WAITING_USER_ANSWER
            state = self._state_for(terminal_id, rule.name, rule.body_hash)
            if rearm != "fire" and time.monotonic() < state.cooldown_until:
                self._log_decision(terminal_id, "no_match", "cooldown_active", rule.name)
                return None  # redraw double-fire guard
            self._log_decision(
                terminal_id,
                "matched",
                "rearm_fire" if rearm == "fire" else "firing",
                rule.name,
            )
            # A real fire resets the veto streak for this terminal (r2-S5 edges).
            self._reset_veto_streak(terminal_id)
            # D2(i)/D5: seed the pending-fire digests over the CHROME-FILTERED
            # match region (F530) so the barrier's settle/consume compares the
            # same domain the rule matched — the classifier still saw ``region``.
            pending_region = match_region.with_digests(settle=match_digest, consume=match_digest)
            self._fire(
                terminal_id,
                metadata,
                provider,
                rule,
                pending_region,
                state,
                incarnation,
            )
            return None

        self._clear_wait_rule(terminal_id)
        # F597 #454 pt2 (c): no rule matched → the dialog episode is over. Drop
        # the retry-exhausted latch and its re-arm/settle state so a later,
        # unrelated dialog starts fresh (and a persistent latch cannot survive a
        # cleared screen).
        with self._lock:
            self._retry_exhausted.discard(terminal_id)
            self._rearm_state.pop(terminal_id, None)
            self._settled_signatures.pop(terminal_id, None)
        self._log_decision(
            terminal_id,
            "no_match",
            "no_rule_matched",
            extra=self._reject_summary(provider_name, match_region),
        )
        self._log_no_match_region(terminal_id, match_region.normalized)
        unknown_result = self._check_unknown(
            terminal_id,
            metadata,
            provider_name,
            provider,
            lines,
            region,
            supplied_status,
            incarnation,
        )
        # D4: an open unknown-dialog episode holds the terminal at WAITING; on a
        # silent pane no chunk re-triggers detection (C3), so re-arm a tick.
        if unknown_result == TerminalStatus.WAITING_USER_ANSWER:
            self._request_detection_retry(terminal_id)
        return unknown_result

    # ----- rule firing ---------------------------------------------------

    def _state_for(self, terminal_id: str, rule_name: str, body_hash: str = "") -> _RuleState:
        with self._lock:
            # D5: the cooldown key gains the rule body-hash so a changed rule
            # resets BOTH the cooldown and the consume digest (aligned keying).
            key = (terminal_id, rule_name, body_hash)
            state = self._rule_state.get(key)
            if state is None:
                state = _RuleState()
                self._rule_state[key] = state
            return state

    def _rearm_gate(self, terminal_id: str, signature: str, rule: Rule) -> Optional[str]:
        """F597 #454 pt2 (c): decide the re-fire disposition for a matched rule
        when the terminal may be in the retry-exhausted latch.

        Returns:
          * ``None``  — not latched; take the normal fire path (cooldown applies).
          * ``"hold"``— latched on THIS signature but the backoff has not elapsed;
            caller holds at WAITING and re-arms a tick.
          * ``"fire"``— latched and the backoff HAS elapsed (or the signature
            changed, resetting the latch): re-fire now, advancing the schedule.

        A signature change clears the latch so a NEW dialog is handled fresh. The
        schedule is REARM_BACKOFF_S then every REARM_STEADY_S, capped at
        REARM_CAP_S since exhaustion; past the cap it holds (human already pinged).
        """
        now = _clock()
        with self._lock:
            latched = terminal_id in self._retry_exhausted
            rearm = self._rearm_state.get(terminal_id)
            if not latched:
                return None
            if rearm is None:
                # Latched with no schedule (defensive) — treat as due.
                return "fire"
            if rearm.signature and signature and rearm.signature != signature:
                # A different dialog is up now — drop the stale latch and let the
                # normal fire path (with settle) handle the new one.
                self._retry_exhausted.discard(terminal_id)
                self._rearm_state.pop(terminal_id, None)
                self._settled_signatures.pop(terminal_id, None)
                return None
            if now - rearm.exhausted_at >= REARM_CAP_S:
                return "hold"  # capped — stay surfaced for the human
            if now < rearm.next_at:
                return "hold"
            # Due: advance the schedule and authorize a re-fire.
            attempts = rearm.attempts + 1
            if attempts < len(REARM_BACKOFF_S):
                delay = REARM_BACKOFF_S[attempts]
            else:
                delay = REARM_STEADY_S
            rearm.attempts = attempts
            rearm.next_at = now + delay
            return "fire"

    def _settle_capture(
        self, terminal_id: str, chrome_patterns: Optional[List["re.Pattern[str]"]]
    ) -> Optional[DialogRegion]:
        """F597 #454 pt2 (a): the capture seam the settle gate samples twice.

        Delegates to the same real screen capture the retry loop uses; kept as a
        SEPARATE method purely so tests can control the settle samples (dialog
        still present) independently from the retry-loop's ``_current_normalized``
        (which a test may stub to 'cleared') — in production both read the same
        live pane, so the two are identical."""
        return self._current_normalized_filtered(terminal_id, chrome_patterns)

    def _settle_before_first_send(
        self,
        terminal_id: str,
        provider: Any,
        region: DialogRegion,
        rule: Rule,
    ) -> bool:
        """F597 #454 pt2 (a): gate the FIRST send of an episode on frame stability.

        The 5x-in-1.6s no-op stall was the responder firing Enter before codex's
        TUI input handler was armed: the dialog was matched from a mid-paint
        frame, the keys were swallowed, the retry budget burned in ~2 s, then the
        terminal latched off. Requiring the matched region to be BYTE-STABLE
        across two captures ``SETTLE_INTERVAL_S`` apart proves the dialog is fully
        painted (and, empirically, that input is armed) before any key is sent.

        Runs at most ONCE per episode per region signature (``consume_digest``):
        once settled, subsequent fires/retries for the same signature skip it.
        Returns True if already-settled or settle passed; False if the frame kept
        changing or could not be captured (caller must NOT send this tick — a
        re-arm/retry will try again). A signature change resets the settle set.
        """
        signature = region.consume_digest
        with self._lock:
            settled = self._settled_signatures.setdefault(terminal_id, set())
            if signature and signature in settled:
                return True
        chrome_patterns = self._chrome_patterns(provider)
        first = self._settle_capture(terminal_id, chrome_patterns)
        if first is None or not rule.matches(first):
            self._log_decision(terminal_id, "no_match", "settle_capture_failed", rule.name)
            return False
        _clock_sleep(SETTLE_INTERVAL_S)
        second = self._settle_capture(terminal_id, chrome_patterns)
        if second is None or not rule.matches(second):
            self._log_decision(terminal_id, "no_match", "settle_lost_frame", rule.name)
            return False
        if _digest_normalized(first.normalized) != _digest_normalized(second.normalized):
            # Still painting/moving — do not send into an unarmed handler.
            self._log_decision(terminal_id, "no_match", "settle_unstable", rule.name)
            return False
        with self._lock:
            self._settled_signatures.setdefault(terminal_id, set()).add(signature)
        self._log_decision(terminal_id, "matched", "settled", rule.name)
        return True

    def _fire(
        self,
        terminal_id: str,
        metadata: Dict[str, Any],
        provider: Any,
        rule: Rule,
        region: DialogRegion,
        state: _RuleState,
        incarnation: TerminalIncarnation,
    ) -> bool:
        # F597 #454 pt2 (a): settle the FIRST send of an episode before touching
        # the barrier/keys. A False here means "not stable yet" — request another
        # detection tick and withhold the send rather than firing blind.
        if not self._settle_before_first_send(terminal_id, provider, region, rule):
            self._request_detection_retry(terminal_id)
            return False
        if not self._effect_barrier(terminal_id, metadata, provider, rule, region):
            return False
        if not self._send_answer(terminal_id, metadata, rule, incarnation):
            return False
        self._log(terminal_id, rule, "fired", region.normalized)
        state.cooldown_until = time.monotonic() + COOLDOWN_S
        threading.Thread(
            target=self._verify_and_retry,
            args=(terminal_id, metadata, provider, rule, state, incarnation, region),
            daemon=True,
        ).start()
        return True

    def _verify_and_retry(
        self,
        terminal_id: str,
        metadata: Dict[str, Any],
        provider: Any,
        rule: Rule,
        state: _RuleState,
        incarnation: TerminalIncarnation,
        region: DialogRegion,
    ) -> None:
        """Runs off the event-loop thread: 1s-later recheck, retry <=3 total fires.

        D2(i): each attempt RE-SEEDS ``settle_digest`` from its own capture
        (r3-S2) — otherwise the first keystroke's redraw fails settle and
        disables retries. ``consume_digest`` is carried forward unchanged.
        """
        chrome_patterns = self._chrome_patterns(provider)
        for attempt in range(2, RETRY_MAX + 1):
            time.sleep(RETRY_DELAY_S)
            if not self._incarnation_is_current(terminal_id, incarnation):
                return
            attempt_region = self._current_normalized_filtered(terminal_id, chrome_patterns)
            if attempt_region is None or not rule.matches(attempt_region):
                # D5: the dialog cleared — record the FROZEN consume_digest so a
                # later identical redraw of the same region is not re-fired.
                if attempt_region is not None:
                    self._record_consumed(terminal_id, rule, region.consume_digest)
                return
            attempt_region = attempt_region.with_digests(
                settle=_digest_normalized(attempt_region.normalized),
                consume=region.consume_digest,
            )
            if not self._effect_barrier(terminal_id, metadata, provider, rule, attempt_region):
                return
            if not self._send_answer(terminal_id, metadata, rule, incarnation):
                return
            self._log(terminal_id, rule, f"retry-{attempt}", attempt_region.normalized)
            state.cooldown_until = time.monotonic() + COOLDOWN_S

        time.sleep(RETRY_DELAY_S)
        if not self._incarnation_is_current(terminal_id, incarnation):
            return
        final_region = self._current_normalized_filtered(terminal_id, chrome_patterns)
        if final_region is not None and rule.matches(final_region):
            self._surface_retry_exhausted(
                terminal_id,
                metadata,
                rule,
                provider,
                incarnation,
                signature=region.consume_digest,
            )
        elif final_region is not None:
            # D5: cleared after the retry budget — record the consume digest.
            self._record_consumed(terminal_id, rule, region.consume_digest)

    def _record_consumed(self, terminal_id: str, rule: Rule, consume_digest: str) -> None:
        if not consume_digest:
            return
        with self._lock:
            self._consumed_digests[(terminal_id, rule.name, rule.body_hash)] = consume_digest

    @staticmethod
    def _current_normalized(
        terminal_id: str, chrome_patterns: Optional[List["re.Pattern[str]"]] = None
    ) -> Optional[DialogRegion]:
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        lines = status_monitor.get_rendered_screen(terminal_id)
        if lines is None:
            return None
        return dialog_region(lines, chrome_patterns)

    def _current_normalized_filtered(
        self, terminal_id: str, chrome_patterns: Optional[List["re.Pattern[str]"]]
    ) -> Optional[DialogRegion]:
        """F530: chrome-aware wrapper around ``_current_normalized`` used by the
        retry loop. Kept separate so tests that patch the single-arg
        ``_current_normalized`` (F55/M5, retry-cap) keep working: they patch the
        base method, and this wrapper re-applies chrome filtering to the rows it
        returns without changing that method's call signature."""
        region = self._current_normalized(terminal_id)
        if region is None or not chrome_patterns:
            return region
        # Re-slice the returned rows with chrome dropped (idempotent for callers
        # that already filtered; correct for patched single-arg stand-ins).
        return dialog_region(list(region.rows), chrome_patterns)

    @staticmethod
    def _barrier_composite_region(
        terminal_id: str, chrome_patterns: Optional[List["re.Pattern[str]"]]
    ) -> Optional[DialogRegion]:
        """F635 #490: the barrier's match/settle capture, in the SAME domain the
        settle-digest is seeded in.

        ``_effect_barrier``'s ``fresh`` capture comes from ``capture_viewport``
        (the RAW tmux viewport), but the settle-digest it compares against is
        seeded (in ``_on_screen`` / ``_verify_and_retry``) from the pyte-COMPOSITE
        screen (``status_monitor.get_rendered_screen``). Those two capture paths
        diverge for a dialog wider than the pane: the composite retains the full
        line while the viewport truncates it at the terminal width. For the codex
        resume-cwd chooser (long absolute worktree paths in its options) the
        normalized strings — and thus ``_digest_normalized`` — legitimately DIFFER
        though it is the SAME static dialog, so the cross-domain ``settle_ok``
        equality NEVER agreed and the send was withheld on every eval (#490's
        matched-but-no-fire deadlock).

        This reads the composite screen DIRECTLY (not via ``_current_normalized``)
        so the barrier stays in the seed's domain WITHOUT colliding with the
        retry-loop tests that stub ``_current_normalized`` to simulate a cleared
        dialog. Returns None when no composite is available (the barrier then
        keeps its prior viewport-region behaviour)."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        lines = status_monitor.get_rendered_screen(terminal_id)
        if lines is None:
            return None
        return dialog_region(lines, chrome_patterns)

    def _send_answer(
        self,
        terminal_id: str,
        metadata: Dict[str, Any],
        rule: Rule,
        incarnation: TerminalIncarnation,
    ) -> bool:
        from cli_agent_orchestrator.backends.registry import get_backend

        backend = get_backend()
        for i, key in enumerate(rule.answer):
            if i > 0:
                time.sleep(KEY_DELAY_S)

            def send_key(key: str = key) -> None:
                backend.send_special_key(metadata["tmux_session"], metadata["tmux_window"], key)

            if not self._run_fenced_effect(
                terminal_id,
                incarnation,
                send_key,
            ):
                return False
        return True

    @staticmethod
    def _log(terminal_id: str, rule: Rule, event: str, normalized: str) -> None:
        AUTO_ANSWER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = AUTO_ANSWER_LOG_DIR / f"{terminal_id}.log"
        ts = datetime.now(timezone.utc).isoformat()
        excerpt = normalized[:200]
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{ts} rule={rule.name} event={event} dialog={excerpt!r}\n")
        except OSError:
            logger.exception("auto-responder: failed to write log for %s", terminal_id)

    @staticmethod
    def _log_decision(
        terminal_id: str,
        outcome: str,
        reason: str,
        rule_name: str | None = None,
        *,
        extra: str | None = None,
    ) -> None:
        """F491: Log every on_screen evaluation decision for diagnosability.

        Outcomes: 'matched' (rule fired or wait activated), 'no_match' (rule
        present but conditions not met), 'not_running' (early exit before rule eval).
        """
        AUTO_ANSWER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = AUTO_ANSWER_LOG_DIR / f"{terminal_id}.decisions.log"
        ts = datetime.now(timezone.utc).isoformat()
        parts = [f"{ts} outcome={outcome} reason={reason}"]
        if rule_name:
            parts.append(f"rule={rule_name}")
        if extra:
            parts.append(extra)
        line = " ".join(parts) + "\n"
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # best-effort — never crash on decision logging

    def _surface_retry_exhausted(
        self,
        terminal_id: str,
        metadata: Dict[str, Any],
        rule: Rule,
        provider: Any = None,
        incarnation: TerminalIncarnation | None = None,
        signature: str = "",
    ) -> None:
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        incarnation = incarnation or self._snapshot_incarnation(terminal_id, metadata)
        # Retry-exhausted: dialog confirmed still-matching. Settle not applicable
        # (no rule-loop sample) → empty-digest region; barrier still enforces
        # match + busy-veto + incarnation.
        empty_region = DialogRegion(rows=(), normalized="")
        if not self._effect_barrier(terminal_id, metadata, provider, rule, empty_region):
            return

        def surface() -> None:
            status_monitor.force_status(terminal_id, TerminalStatus.WAITING_USER_ANSWER)
            with self._lock:
                self._retry_exhausted.add(terminal_id)
                # F597 #454 pt2 (c): seed the re-arm clock so the latch is NOT
                # permanent — while this same dialog signature persists it will
                # be re-fired on REARM_BACKOFF_S/REARM_STEADY_S until REARM_CAP_S.
                now = _clock()
                self._rearm_state[terminal_id] = _RearmState(
                    signature=signature,
                    exhausted_at=now,
                    next_at=now + REARM_BACKOFF_S[0],
                    attempts=0,
                )

        if not self._run_fenced_effect(terminal_id, incarnation, surface):
            return
        self._push(
            terminal_id,
            metadata,
            f"[auto-responder] rule '{rule.name}' fired {RETRY_MAX}x on "
            f"{_ar_display_name(terminal_id, metadata)} but the dialog persists. Manual attention needed.",
            incarnation,
        )

    # ----- unknown-dialog heuristic ---------------------------------------

    def _check_unknown(
        self,
        terminal_id: str,
        metadata: Dict[str, Any],
        provider_name: str,
        provider: Any,
        lines: List[str],
        region: DialogRegion,
        supplied_status: TerminalStatus | None,
        incarnation: TerminalIncarnation,
    ) -> Optional[TerminalStatus]:
        # F86: exempt known permission/AskUserQuestion prompts — return
        # WAITING_USER_ANSWER without escalating to the supervisor.
        if any(pat.search(region.normalized) for pat in _PERMISSION_PROMPT_PATTERNS):
            return TerminalStatus.WAITING_USER_ANSWER

        shape_suspect = self._looks_like_dialog(region.normalized, provider_name)
        is_suspect = supplied_status == TerminalStatus.WAITING_USER_ANSWER or (
            shape_suspect
            and (
                supplied_status is None
                or supplied_status
                in (
                    TerminalStatus.WAITING_USER_ANSWER,
                    TerminalStatus.UNKNOWN,
                    TerminalStatus.ERROR,
                )
            )
        )

        if not is_suspect:
            if shape_suspect:
                return self._record_unknown_nonclean_tick(terminal_id)
            return self._record_unknown_clean_tick(terminal_id)

        fresh = self._capture_for_analysis(metadata, lines, terminal_id, provider)
        if fresh is None:
            return None
        fresh_region = self._region_from_capture(fresh)
        fresh_status = self._classify_region(terminal_id, provider, fresh_region)
        fresh_shape_suspect = self._looks_like_dialog(fresh_region.normalized, provider_name)
        is_suspect = fresh_status == TerminalStatus.WAITING_USER_ANSWER or (
            fresh_shape_suspect
            and fresh_status in (TerminalStatus.WAITING_USER_ANSWER, TerminalStatus.ERROR)
        )
        if not is_suspect:
            if fresh_shape_suspect:
                return self._record_unknown_nonclean_tick(terminal_id)
            return self._record_unknown_clean_tick(terminal_id)
        normalized = fresh_region.normalized

        now = time.monotonic()
        with self._lock:
            state = self._unknown_state.get(terminal_id)
            if state is None:
                state = _UnknownDialogState()
                self._unknown_state[terminal_id] = state
            new_episode = not state.episode_open
            state.episode_open = True
            state.non_dialog_ticks = 0
            should_push = new_episode and now - state.last_push_at >= UNKNOWN_DIALOG_PUSH_FLOOR_S
            if should_push:
                state.last_push_at = now

        if should_push:
            # Layer A (F115): last-line recheck before push — validate metadata
            # still present, terminal not suppressed, and incarnation current.
            from cli_agent_orchestrator.clients.database import get_terminal_metadata as _get_meta

            recheck_meta = _get_meta(terminal_id)
            if recheck_meta is None:
                return TerminalStatus.WAITING_USER_ANSWER
            with self._lock:
                if terminal_id in self._exit_suppressed:
                    return TerminalStatus.WAITING_USER_ANSWER
            recheck_incarnation = self._snapshot_incarnation(terminal_id, recheck_meta)
            if recheck_incarnation != incarnation:
                return TerminalStatus.WAITING_USER_ANSWER

            dialog_text = self._payload_excerpt(normalized)
            self._push(
                terminal_id,
                metadata,
                f"[auto-responder] unknown blocking dialog on "
                f"{_ar_display_name(terminal_id, metadata)} (provider={provider_name}); no rule matched, the "
                "worker is stalled. Ask the user how to answer it (auto-answer "
                "default / other keys / always wait), then append a rule to "
                f"~/.aws/cli-agent-orchestrator/auto-answers/{provider_name}.yaml.\n\n"
                f"Dialog text (normalized): {dialog_text}",
                incarnation,
            )
        return TerminalStatus.WAITING_USER_ANSWER

    def _record_unknown_nonclean_tick(self, terminal_id: str) -> Optional[TerminalStatus]:
        """Suppress a non-WAITING shaped frame without counting it as clean."""
        with self._lock:
            state = self._unknown_state.get(terminal_id)
            if state and state.episode_open:
                return TerminalStatus.WAITING_USER_ANSWER
        return None

    def _record_unknown_clean_tick(self, terminal_id: str) -> Optional[TerminalStatus]:
        """Apply the existing two-clean-tick close rule to a confirmed clean frame."""
        close_episode = False
        with self._lock:
            before = self._waiting_gate_locked(terminal_id)
            state = self._unknown_state.get(terminal_id)
            if state and state.episode_open:
                state.non_dialog_ticks += 1
                if state.non_dialog_ticks >= 2:
                    state.episode_open = False
                    state.non_dialog_ticks = 0
                    close_episode = True
            after = self._waiting_gate_locked(terminal_id)
        self._gate_transition(terminal_id, before, after)
        if state and state.episode_open and not close_episode:
            return TerminalStatus.WAITING_USER_ANSWER
        return None

    @staticmethod
    def _capture_fresh(
        metadata: Dict[str, Any],
        _suspect_lines: List[str],
        terminal_id: str | None = None,
        provider: Any = None,
    ) -> tuple[str, List[str]] | AutoResponderDecision | None:
        """Capture once; activated paths prove, publish, and token-read it."""
        try:
            from cli_agent_orchestrator.backends.registry import get_backend

            if terminal_id is not None and provider is not None:
                from cli_agent_orchestrator.services.seam_activation import (
                    receiver_state_active,
                )

                if (
                    receiver_state_active("auto_responder.frame_classify")
                    and not get_backend().supports_event_inbox()
                ):
                    from cli_agent_orchestrator.providers.screen_classification import (
                        ScreenClassification,
                        ScreenClassificationResult,
                        screen_classification_result,
                    )
                    from cli_agent_orchestrator.services.status_monitor import status_monitor

                    proof = status_monitor.prove_terminal_identity(terminal_id)
                    captured = get_backend().capture_viewport(
                        metadata["tmux_session"], metadata["tmux_window"]
                    )
                    captured_at = time.monotonic()
                    lines = captured.splitlines()
                    normalized = normalize_screen(lines)
                    if not normalized:
                        return None
                    if status_monitor._signal_emitting(provider):
                        prior = status_monitor.receiver_state_store.prior_classification(
                            (
                                terminal_id,
                                int(metadata["lifecycle_generation"]),
                                str(metadata["tmux_window"]),
                            ),
                            prefer_fresh=True,
                        )
                        classification = screen_classification_result(
                            provider.emit_screen_signals(lines),
                            () if prior is None else prior.signals,
                            provider.capabilities.liveness_anchor,
                        )
                    else:
                        status = provider.get_status_from_screen(lines)
                        classification = ScreenClassificationResult(
                            ScreenClassification(status, "none", None, None), ()
                        )
                    token = status_monitor.publish_fresh_observation(
                        terminal_id,
                        lines,
                        captured_at,
                        classification,
                        "fresh_capture",
                        proof,
                    )
                    view = status_monitor.receiver_state_store.snapshot_view(
                        (
                            terminal_id,
                            int(metadata["lifecycle_generation"]),
                            str(metadata["tmux_window"]),
                        ),
                        require_fresh=True,
                        max_age_s=2.0,
                        recovery_state=metadata.get("recovery_state"),
                        token=token,
                    )
                    if view is None:
                        return None
                    raw = view.raw_classification
                    return AutoResponderDecision(
                        normalized,
                        tuple(lines),
                        raw.status if raw is not None else view.latched_status,
                        raw,
                        token,
                    )

            captured = get_backend().capture_viewport(
                metadata["tmux_session"], metadata["tmux_window"]
            )
            lines = captured.splitlines()
            normalized = normalize_screen(lines)
            if not normalized:
                return None
            return normalized, lines
        except Exception:
            logger.debug(
                "auto-responder: fresh confirmation capture failed for %s",
                metadata.get("id", "unknown"),
                exc_info=True,
            )
            return None

    def _capture_for_analysis(
        self,
        metadata: Dict[str, Any],
        lines: List[str],
        terminal_id: str,
        provider: Any,
    ) -> tuple[str, List[str]] | AutoResponderDecision | None:
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.services.seam_activation import receiver_state_active

        if (
            receiver_state_active("auto_responder.frame_classify")
            and not get_backend().supports_event_inbox()
        ):
            return self._capture_fresh(metadata, lines, terminal_id, provider)
        return self._capture_fresh(metadata, lines)

    def _effect_barrier(
        self,
        terminal_id: str,
        metadata: Dict[str, Any],
        provider: Any,
        rule: Rule,
        pending_region: DialogRegion,
    ) -> bool:
        # D4 barrier-False enumeration (r4-S1/r5-S2, six outcomes): provider
        # None and capture failure are TRANSIENT (not verdicts) — request a D4
        # retry, no streak. settle mismatch and barrier _busy_veto request a
        # retry (busy-veto also streaks in c6). A fresh rule.matches-False
        # (dialog cleared) does NOT retry; consumed-digest (D5, c5) will not
        # either.
        if provider is None:
            self._request_detection_retry(terminal_id)
            return False
        fresh = self._capture_for_analysis(metadata, [], terminal_id, provider)
        if fresh is None:
            self._request_detection_retry(terminal_id)
            return False
        chrome_patterns = self._chrome_patterns(provider)
        # Classify on the UNFILTERED tail (unchanged behavior); match + settle on
        # the CHROME-FILTERED tail (F530 layer 1), mirroring the on_screen split.
        region = self._region_from_capture(fresh)
        # F635 #490: derive the match/settle region from the pyte-COMPOSITE screen
        # (the SAME domain the settle-digest is seeded in), not from ``fresh`` (the
        # raw ``capture_viewport`` tail). See ``_barrier_composite_region``: the two
        # capture paths diverge for a dialog wider than the pane (codex resume-cwd
        # long worktree paths), so comparing a truncated-viewport digest against a
        # composite settle-digest never agreed and withheld the send forever. Fall
        # back to the viewport tail only when no composite is available (preserving
        # prior behaviour for tests/paths without a live composite). The raw
        # viewport ``region`` is still used for the provider ``status`` classify.
        viewport_match_region = (
            self._region_from_capture(fresh, chrome_patterns) if chrome_patterns else region
        )
        composite_match_region = self._barrier_composite_region(terminal_id, chrome_patterns)
        if composite_match_region is not None:
            match_region = composite_match_region
        else:
            match_region = viewport_match_region
        status = self._classify_region(terminal_id, provider, region)
        if not rule.matches(match_region):
            return False  # dialog cleared / no longer matches — no retry
        # F640 #495: the SETTLE-digest comparison below runs in the composite
        # domain (F635's #490 fix — settle_digest is composite-seeded), but the
        # FIRE decision must ALSO be corroborated by the LIVE tmux viewport. The
        # composite retains text the viewport does not — the width-retained tail
        # of a line wider than the pane AND stale palimpsest rows when the pyte
        # screen size and the real pane disagree (see
        # ``status_monitor._resolve_screen_size``). Without this gate F635 fired a
        # rule's answer keys on a match present ONLY in the composite: during
        # codex init those keys landed in a mid-render/palimpsest frame or the
        # wrong widget, leaving codex with no ``›`` composer, so the deferred-init
        # send read an unreadable composer and the worker was torn down
        # (deferred_init_internal — the #495 signature). The genuine #490 chooser
        # is unaffected: its option LABELS are short and visible in the viewport;
        # only the long ``(path)`` tails truncate. When no composite is available
        # ``match_region`` IS the viewport region, so this gate is a no-op there.
        if composite_match_region is not None and not rule.matches(viewport_match_region):
            # Uncorroborated composite-only match — withhold the send. Re-arm a
            # detection tick: a genuine dialog will corroborate once the live
            # viewport catches up to the composite.
            self._request_detection_retry(terminal_id)
            return False
        if self._busy_veto(status):
            self._request_detection_retry(terminal_id)
            return False
        # D2(i) settle (r2-B3/r5-B1): normalized-domain digest match on the
        # chrome-filtered match region; empty settle_digest → skipped.
        settle_ok = (not pending_region.settle_digest) or (
            _digest_normalized(match_region.normalized) == pending_region.settle_digest
        )
        if not settle_ok:
            self._request_detection_retry(terminal_id)  # mid-repaint tearing
            return False
        # D5 consume gate: refuse to re-fire a pre-fire region digest already
        # consumed for this (terminal, rule-name, body-hash) — the redraw double-
        # fire guard, superseding COOLDOWN_S for re-fire decisions. A consumed-
        # digest hit does NOT request a retry (r4-S1). A changed region digest is
        # a new consume_digest and is eligible again (AC4).
        if pending_region.consume_digest:
            key = (terminal_id, rule.name, rule.body_hash)
            with self._lock:
                already = self._consumed_digests.get(key) == pending_region.consume_digest
            if already:
                return False
        return True

    @staticmethod
    def _region_from_capture(
        capture: tuple[str, List[str]] | AutoResponderDecision,
        chrome_patterns: Optional[List["re.Pattern[str]"]] = None,
    ) -> DialogRegion:
        if isinstance(capture, AutoResponderDecision):
            return dialog_region(list(capture.lines), chrome_patterns)
        return dialog_region(capture[1], chrome_patterns)

    @staticmethod
    def _classify_region(
        terminal_id: str, provider: Any, region: DialogRegion
    ) -> TerminalStatus | None:
        try:
            status = provider.get_status_from_screen(list(region.rows))
            return status if isinstance(status, TerminalStatus) else None
        except Exception:
            logger.debug(
                "auto-responder: provider region status parse failed for %s",
                terminal_id,
                exc_info=True,
            )
            return None

    @classmethod
    def _corroborates_fire(cls, region: DialogRegion, status: TerminalStatus | None) -> bool:
        return status == TerminalStatus.WAITING_USER_ANSWER or cls._has_dialog_proximity(
            region.normalized
        )

    @staticmethod
    def _busy_veto(status: TerminalStatus | None) -> bool:
        return status == TerminalStatus.PROCESSING

    @staticmethod
    def _scroll_excluded(old_rows: tuple[str, ...], new_rows: tuple[str, ...]) -> bool:
        """F516 D6: is the newer region a SCROLLED banner rather than a dialog?

        "Scrolled" (r3-S7): a maximal line-block present in BOTH captures' rows,
        offset-independent, with ≥1 new non-empty row BELOW the block in the
        newer capture. stdlib difflib.SequenceMatcher.get_matching_blocks — no
        hand-rolled diff. The new-row-below test applies at the newer block end.
        """
        import difflib

        if not old_rows or not new_rows:
            return False
        blocks = [
            b
            for b in difflib.SequenceMatcher(
                None, list(old_rows), list(new_rows)
            ).get_matching_blocks()
            if b.size > 0
        ]
        if not blocks:
            return False
        largest = max(blocks, key=lambda b: b.size)
        end_new = largest.b + largest.size
        return any(row.strip() for row in new_rows[end_new:])

    def _push_region_history(self, terminal_id: str, region: DialogRegion) -> bool:
        """F516 D6: append the eval-entry region to the last-2 history and return
        this eval's scroll-exclusion (banner) verdict, cached per-terminal for
        D3's match_verdict. Called EXACTLY ONCE per eval from the rule-loop entry
        capture (r4-B1) — never from barrier/retry captures."""
        with self._lock:
            history = self._region_history.get(terminal_id, [])
            prev = history[-1] if history else None
            banner = self._scroll_excluded(prev.rows, region.rows) if prev is not None else False
            history.append(region)
            del history[:-2]
            self._region_history[terminal_id] = history
            self._prefilter_verdict[terminal_id] = banner
        return banner

    def _reset_veto_streak(self, terminal_id: str) -> None:
        """F516 D6: reset edges are fire, pane output, and episode close."""
        with self._lock:
            state = self._veto_streak.get(terminal_id)
            if state is not None:
                state.count = 0
                state.episode_open = False

    def _note_veto_streak(
        self,
        terminal_id: str,
        metadata: Dict[str, Any],
        incarnation: TerminalIncarnation,
    ) -> None:
        """F516 D6: count one match-but-vetoed eval; at the ≥5 threshold emit
        exactly one supervisor push per episode through its OWN 300s floor (NOT
        the unknown-dialog floor, r2-S5). ~15s bound with D4's chain."""
        now = time.monotonic()
        should_push = False
        with self._lock:
            state = self._veto_streak.get(terminal_id)
            if state is None:
                state = _VetoStreakState()
                self._veto_streak[terminal_id] = state
            state.count += 1
            if (
                state.count >= VETO_STREAK_THRESHOLD
                and not state.episode_open
                and now - state.last_push_at >= VETO_STREAK_PUSH_FLOOR_S
            ):
                state.episode_open = True
                state.last_push_at = now
                should_push = True
        if should_push:
            self._push(
                terminal_id,
                metadata,
                f"[auto-responder] {_ar_display_name(terminal_id, metadata)} has a rule that "
                f"matched but was vetoed {VETO_STREAK_THRESHOLD}+ evals in a row (dialog may be "
                "stuck behind a busy classifier). Manual attention may be needed.",
                incarnation,
            )

    def _request_detection_retry(self, terminal_id: str) -> None:
        """F516 D4: ask status_monitor to re-arm a detection tick after this eval
        ended without firing (busy-veto / unknown-dialog / wait-rule-active / a
        transient barrier-False). FUNCTION-SCOPE import — status_monitor imports
        the responder lazily, so a module-level reverse import would cycle.
        Wrapped so a request failure can never raise into or alter on_screen
        (r4-S5). Called as a LEAF with no responder lock held (F522)."""
        try:
            from cli_agent_orchestrator.services.status_monitor import status_monitor

            status_monitor.schedule_detection_retry(terminal_id)
        except Exception:
            logger.debug(
                "auto-responder: detection-retry request failed for %s",
                terminal_id,
                exc_info=True,
            )

    def _snapshot_incarnation(
        self, terminal_id: str, metadata: Dict[str, Any]
    ) -> TerminalIncarnation:
        with self._lock:
            engine_generation = self._terminal_generation.get(terminal_id, 0)
        return (
            engine_generation,
            int(metadata.get("lifecycle_generation", 0)),
            str(metadata["tmux_session"]),
            str(metadata["tmux_window"]),
        )

    def _incarnation_matches_under_delivery_lock(
        self, terminal_id: str, expected: TerminalIncarnation
    ) -> bool:
        from cli_agent_orchestrator.clients.database import get_terminal_metadata

        metadata = get_terminal_metadata(terminal_id)
        if metadata is None:
            return False
        with self._lock:
            engine_generation = self._terminal_generation.get(terminal_id, 0)
        current = (
            engine_generation,
            int(metadata.get("lifecycle_generation", 0)),
            str(metadata["tmux_session"]),
            str(metadata["tmux_window"]),
        )
        return current == expected

    def _incarnation_is_current(self, terminal_id: str, expected: TerminalIncarnation) -> bool:
        from cli_agent_orchestrator.services.inbox_service import get_delivery_lock

        delivery_lock = get_delivery_lock(terminal_id)
        delivery_lock.acquire()
        try:
            return self._incarnation_matches_under_delivery_lock(terminal_id, expected)
        finally:
            delivery_lock.release()

    def _run_fenced_effect(
        self,
        terminal_id: str,
        expected: TerminalIncarnation,
        effect: Callable[[], None],
    ) -> bool:
        from cli_agent_orchestrator.services.inbox_service import get_delivery_lock

        delivery_lock = get_delivery_lock(terminal_id)
        delivery_lock.acquire()
        try:
            if not self._incarnation_matches_under_delivery_lock(terminal_id, expected):
                return False
            effect()
            return True
        finally:
            delivery_lock.release()

    @staticmethod
    def _payload_excerpt(normalized: str) -> str:
        if len(normalized) <= UNKNOWN_DIALOG_PAYLOAD_CHARS:
            return normalized
        return normalized[:UNKNOWN_DIALOG_PAYLOAD_CHARS] + "..."

    @staticmethod
    def _looks_like_dialog(normalized: str, provider_name: str) -> bool:
        if provider_name == "codex":
            # F597 #454: ``normalized`` is the CANONICAL string (lowercased,
            # punctuation folded to spaces), so codex's raw-screen
            # WAITING_PROMPT_PATTERN (which relies on capitalization, "y/n"
            # slashes and a line anchor) cannot match here. Use a canonical-
            # domain equivalent: an "approve"/"allow" lead followed by a yes/no
            # affordance somewhere in the region.
            if _CANONICAL_APPROVAL_PATTERN.search(normalized):
                return True
        return AutoResponder._has_dialog_proximity(normalized)

    @staticmethod
    def _has_dialog_proximity(normalized: str) -> bool:
        numbered_options = list(_NUMBERED_OPTION_PATTERN.finditer(normalized))
        for press_enter in _PRESS_ENTER_PATTERN.finditer(normalized):
            candidates = [
                option for option in numbered_options if option.start() < press_enter.start()
            ]
            if not candidates:
                continue
            nearest = max(candidates, key=lambda option: option.start())
            if press_enter.start() - nearest.end() <= DIALOG_PROXIMITY_CHARS:
                return True
        return False

    # ----- supervisor push -------------------------------------------------

    @staticmethod
    def _find_supervisor(session_name: str) -> Optional[str]:
        """F203 D19: Role-based supervisor identity resolver.

        Returns the terminal_id of the supervisor-role terminal in the session.
        Uses agent_profile (role marker) instead of the fragile provider-first
        heuristic that breaks with multiple claude_code terminals (F196/F205).

        Falls back to provider == "claude_code" only if no role-marked terminal
        exists (backward compat for sessions created before role tagging).
        """
        from cli_agent_orchestrator.clients.database import list_terminals_by_session

        terminals = list_terminals_by_session(session_name)

        # Primary: role-based lookup
        supervisor_profiles = {"supervisor", "code_supervisor", "chao_supervisor"}
        for terminal in terminals:
            profile = terminal.get("agent_profile", "")
            if profile in supervisor_profiles:
                return terminal["id"]

        # Fallback: first terminal with no caller_id and provider claude_code
        # (a supervisor has no caller; workers always have one)
        for terminal in terminals:
            if terminal.get("caller_id") is None and terminal["provider"] == "claude_code":
                return terminal["id"]

        # Legacy fallback: first claude_code terminal
        for terminal in terminals:
            if terminal["provider"] == "claude_code":
                return terminal["id"]

        return None

    def _push(
        self,
        terminal_id: str,
        metadata: Dict[str, Any],
        message: str,
        incarnation: TerminalIncarnation | None = None,
    ) -> None:
        from cli_agent_orchestrator.services.mailbox_service import create_routed_inbox_message

        supervisor_id = self._find_supervisor(metadata["tmux_session"])
        if not supervisor_id:
            logger.info(
                "auto-responder: no supervisor terminal in session %s for %s; log only",
                metadata["tmux_session"],
                terminal_id,
            )
            return
        if supervisor_id == terminal_id:
            logger.warning("auto-responder: refusing to push terminal %s to itself", terminal_id)
            return
        incarnation = incarnation or self._snapshot_incarnation(terminal_id, metadata)
        try:

            def publish() -> None:
                # F136-D6/D17: routed supervisor push (no inline deliver_pending)
                create_routed_inbox_message(terminal_id, supervisor_id, message)

            self._run_fenced_effect(
                terminal_id,
                incarnation,
                publish,
            )
        except Exception:
            logger.exception("auto-responder: failed to push to supervisor %s", supervisor_id)


# Module-level singleton
auto_responder = AutoResponder()
