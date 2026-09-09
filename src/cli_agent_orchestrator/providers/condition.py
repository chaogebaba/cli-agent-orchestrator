"""F611 (#467) — provider condition detection: a typed CAPPED/BLOCKED signal.

DETECTION-FIRST (blueprint §0). This module is a NEW read-time projection that
sits BESIDE each provider's ``get_status`` and does NOT touch the frozen
f506/f507 fusion plane (``StatusMonitor.fuse_status``). A **condition** is a
provider-attributable operating state that the 6-value ``TerminalStatus`` cannot
carry (a usage cap, an auth expiry, a blocking modal, a busy pane, …). F611
delivers it as a SEPARATE typed field, never a ``TerminalStatus`` member (D1).

The public entry points:

* :func:`classify_condition` — the core classifier. Given a raw pane buffer and
  the provider key, returns a :class:`Condition` (or ``None`` when nothing in the
  closed taxonomy matched). Precedence §2.2, confidence §2.3, banner-only scan
  §2.4 (D2), busy-last §2.2/D5.
* :class:`ConditionDelivery` — the ONE-event-fanned-out delivery seam (D4): a
  transition de-dup keyed on ``(terminal_id, kind, subtype, epoch)`` that fans a
  single event to the three surfaces. Never three producers.
* :func:`policy_for_condition` — the policy layer (§4, D6/D7/D8): maps a typed
  condition to an advisory action (kiro-fallback / stop-and-ask / advisory-only),
  WITHOUT adding any routing-table refusal code (D7) and WITHOUT auto-recovering
  auth/dialog (D8). A box-plane cap is advisory only (D6).

Every anchor here is quoted byte-exact from a corpus fixture under
``test/providers/fixtures/conditions/`` or ``test/providers/fixtures/status_truth``
and cited in the blueprint §2.1 table. Existing in-tree provider patterns are
REUSED, never re-implemented (imports below).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Protocol, Tuple

# ─── Reused in-tree provider anchors (never re-implemented, blueprint §2.1) ────
# Imported lazily-safe at module import: these are module-level constants in the
# provider modules and carry no import cycle back to this module.
from cli_agent_orchestrator.providers.codex import (  # noqa: E402
    CODEX_ACTIVITY_MARKER_PATTERN,
    SYSTEM_NOTICE_PATTERN,
    TRANSIENT_API_ERROR_PATTERNS,
    TRANSIENT_ERROR_EXCLUSIONS,
    USER_PREFIX_PATTERN,
    codex_activity_marker_live,
)
from cli_agent_orchestrator.utils.text import strip_terminal_escapes


class ConditionKind(str, Enum):
    """Closed condition taxonomy (blueprint §1, issue #467 §1).

    NOT a ``TerminalStatus`` member (D1) — a distinct vocabulary carried on a
    separate field.
    """

    CAPPED = "CAPPED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    NET_INTERRUPTED = "NET_INTERRUPTED"
    CONTEXT_EXHAUSTED = "CONTEXT_EXHAUSTED"
    DIALOG_BLOCKED = "DIALOG_BLOCKED"
    PROC_EXITED = "PROC_EXITED"
    TRANSIENT_OVERLOAD = "TRANSIENT_OVERLOAD"
    BUSY = "BUSY"
    # F792 (#649): an EXPECTED (not-anomalous) operating state — the seat has
    # ENDED its own turn and is idle while one or more in-harness background
    # AGENT lanes run (claude_code's "Waiting for N background agent(s) to
    # finish" line). It is NOT busy and NOT a notice: it is never delivered to
    # the supervisor inbox (it joins the F790 drain-class decline), it renders as
    # `· waiting`, and it is meaningful on an idle/completed seat (so it is
    # deliberately NOT a BUSY_CLASS_LABEL, which get_condition drops on a
    # quiescent seat).
    WAITING_ON_SUBAGENTS = "WAITING_ON_SUBAGENTS"


class Confidence(str, Enum):
    """Classification confidence (blueprint §2.3). Only ``high``/``medium``
    deliver an event; ``low`` logs but does not surface (D3)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Precedence order, FIRST MATCH WINS (blueprint §2.2). Lower rank = higher
# priority. NET_INTERRUPTED sits at 3.5 (a dropped connection is not a cap, not
# auth). BUSY is LAST (7) so a working pane is never mistaken for a stall (D5).
PRECEDENCE: Dict[ConditionKind, float] = {
    ConditionKind.PROC_EXITED: 1.0,
    ConditionKind.DIALOG_BLOCKED: 2.0,
    ConditionKind.AUTH_EXPIRED: 3.0,
    ConditionKind.NET_INTERRUPTED: 3.5,
    ConditionKind.CAPPED: 4.0,
    ConditionKind.CONTEXT_EXHAUSTED: 5.0,
    ConditionKind.TRANSIENT_OVERLOAD: 6.0,
    ConditionKind.BUSY: 7.0,
    # F792 (#649): LAST — a live seat spinner (BUSY, 7.0) always wins over the
    # subagent-wait line, so a seat that is genuinely working its own turn is
    # never mislabelled `· waiting`. In practice the two are mutually exclusive
    # (the wait line carries no spinner ellipsis), but the ordering makes the
    # "working beats waiting" tie-break explicit.
    ConditionKind.WAITING_ON_SUBAGENTS: 8.0,
}


#: F752 (#609): the fleet labels that assert the terminal is WORKING RIGHT NOW.
#: A condition in this class is only meaningful while the fused status is a
#: working one; on a seat whose status is idle/completed it is stale by
#: construction and must be neither written nor rendered. Kept here (beside the
#: taxonomy) so the server guard and the TUI guard share one definition.
BUSY_CLASS_LABELS: "frozenset[str]" = frozenset({ConditionKind.BUSY.value})


def is_busy_class_label(label: Optional[str]) -> bool:
    """True when ``label`` is a fleet condition label that asserts live work."""
    return label is not None and label in BUSY_CLASS_LABELS


#: F790 (#647) cut 3: the write-time cap on a condition's ``evidence`` field. A
#: raw pane row can run to many hundreds of characters; capping it at render time
#: keeps a condition event from carrying a multi-KB pane fragment onto any
#: surface. Kept beside the taxonomy so producer and any renderer share it.
_F790_EVIDENCE_MAX_CHARS: int = 300


@dataclass(frozen=True)
class Condition:
    """A typed, provider-attributable operating state (D1).

    ``scope`` is ``"provider"`` for a laptop/account-plane condition and
    ``"credential_plane"`` for a box-observed one (D6, M36). ``host`` and
    ``credential_plane`` carry the attribution the policy layer needs; a
    box-scoped CAPPED never rebinds a laptop position on its own (§2.4/§4).
    """

    kind: ConditionKind
    provider: str
    subtype: str
    evidence: str
    confidence: Confidence
    reset_hint: Optional[str] = None
    host: Optional[str] = None
    credential_plane: Optional[str] = None
    scope: str = "provider"

    def render_event(self, terminal_id: str) -> str:
        """Render the ONE typed event line (blueprint §3 event shape).

        F790 (#647) cut 3: the ``evidence`` field is a raw pane row and can run to
        many hundreds of characters (a wrapped provider TUI frame). It is capped
        at :data:`_F790_EVIDENCE_MAX_CHARS` (300) here, at write time, with the
        same truncation marker the wake-envelope rule uses — so a condition line
        can never carry a multi-KB pane fragment onto any surface.
        """
        evidence = self.evidence
        if len(evidence) > _F790_EVIDENCE_MAX_CHARS:
            dropped = len(evidence) - _F790_EVIDENCE_MAX_CHARS
            evidence = (
                evidence[:_F790_EVIDENCE_MAX_CHARS]
                + f" …[truncated {dropped} chars; full body in the inbox digest]"
            )
        return (
            f"[CONDITION] terminal={terminal_id} kind={self.kind.value} "
            f"provider={self.provider} subtype={self.subtype} "
            f'evidence="{evidence}" '
            f"reset_hint={self.reset_hint if self.reset_hint else 'none'} "
            f"host={self.host if self.host else 'none'} "
            f"credential_plane={self.credential_plane if self.credential_plane else 'none'} "
            f"confidence={self.confidence.value}"
        )


# ─── Banner-only scan (D2, blueprint §2.4) ─────────────────────────────────────
# Reuse the codex USER_PREFIX_PATTERN state machine idea: a row that opens a user
# region (``You ``/``› ``/``» ``) suppresses that region from banner scanning, so
# a "usage limit" substring quoted inside a supervisor/user message is NOT a
# signal. Assistant-prefix and idle-prompt rows also reset to neutral.
_ASSISTANT_PREFIX_PATTERN = r"^\s*(?:•|●|◇|◆|⏺)\s"
# A quoted trailer-block continuation is any indented line following a user
# prefix; the state persists until a non-user structural row appears.


def banner_rows(pane: str) -> List[str]:
    """Return only the BANNER (non-user, non-quoted) rows of a pane (D2).

    Mirrors ``codex.classify_idle_reason``'s state walk: entering a
    ``USER_PREFIX_PATTERN`` region sets ``state="user"`` and every following
    indented continuation row is suppressed until a new structural row (a
    provider banner glyph, an idle prompt, or a blank line at column 0) resets
    the state. The result is the set of rows a cap/auth/context anchor may match
    against — quoted user text is excluded by construction.
    """
    rows = [strip_terminal_escapes(r) for r in pane.splitlines()]
    out: List[str] = []
    state = "neutral"
    for row in rows:
        if re.search(USER_PREFIX_PATTERN, row):
            # A user turn begins. Suppress this row and its continuation.
            state = "user"
            continue
        if re.search(_ASSISTANT_PREFIX_PATTERN, row):
            # A provider banner/assistant glyph row is a real banner row AND
            # resets any suppressed user region.
            state = "neutral"
            out.append(row)
            continue
        if state == "user":
            # Continuation of a suppressed user block: an indented (leading
            # whitespace) or blank row stays suppressed; a flush-left
            # non-whitespace row re-enters neutral and is kept.
            if row.strip() == "" or row[:1].isspace():
                continue
            state = "neutral"
            out.append(row)
            continue
        out.append(row)
    return out


# ─── Anchor tables (blueprint §2.1) — verbatim substrings from fixtures ────────
# Each provider maps to an ordered list of (kind, subtype, matcher, confidence).
# Matchers run against BANNER rows (D2) unless the kind is BUSY/PROC-scoped.
# The classifier applies §2.2 precedence AFTER collecting matches, so table order
# within a provider is not load-bearing for precedence.

_CAPPED_RESET_HINTS: Tuple[Tuple[str, Callable[["re.Match[str]"], str]], ...] = (
    # (regex to search for a reset hint on the capped pane, hint-extractor)
    (r"try again at ([0-9]{1,2}:[0-9]{2}\s*[AP]M)", lambda m: f"try again at {m.group(1)}"),
    (r"return next month", lambda m: "return next month"),
    (r"resets in ([0-9dhms\s]+?)(?:[.)]|$)", lambda m: f"resets in {m.group(1).strip()}"),
    (r"once you have usage again", lambda m: "Try Again once you have usage again"),
)


def _extract_reset_hint(text: str) -> Optional[str]:
    for pat, extract in _CAPPED_RESET_HINTS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return extract(m)
    return None


# The codex hard-cap banner (codex-capped-1/2): "You've hit your usage limit".
_CODEX_CAP_HARD = re.compile(r"You've hit your usage limit", re.IGNORECASE)
# The reset-availability NOTICE is NOT a cap (D2 reset≠cap guard, §2.4).
_CODEX_RESET_NOTICE = re.compile(SYSTEM_NOTICE_PATTERN)
_KIRO_CAP = re.compile(r"reached your monthly usage limit", re.IGNORECASE)
_GROK_CAP = re.compile(r"You hit your (?:weekly|daily|monthly) limit", re.IGNORECASE)
_CLINE_CAP = re.compile(
    r"reached your monthly Clinepass limit|ClinePass limit reached", re.IGNORECASE
)

_CODEX_AUTH = re.compile(r"access token could not be refreshed|Please sign in again", re.IGNORECASE)
_CLAUDE_AUTH = re.compile(r"Failed to authenticate: OAuth session expired", re.IGNORECASE)

_NET_INTERRUPTED = re.compile(r"Your connection was interrupted", re.IGNORECASE)

# F836 (#693): per-provider footer-percent SEMANTICS. The two providers that
# print a context percentage in their status footer mean OPPOSITE things by the
# number, so a single "is the number small/large?" rule mis-reads one of them:
#
#   * codex footer  "Context NN% left · 5h MM% left"  → NN is the context
#     REMAINING. Exhausted only when little is LEFT: NN <= CODEX threshold.
#   * kiro footer    "agent · model · ◑ NN%"          → NN is the context USED
#     (a pie glyph ◔◑◕● precedes it). Exhausted only when a LOT is used:
#     NN >= KIRO threshold. The glyph alone (◑ etc.) NEVER means exhausted — a
#     healthy warm kiro seat sits at ◑ 30% used (the F836 false positive).
#
# Thresholds are the provider's REAL hard-stop point (brief F836): codex <= 10%
# left, kiro >= 90% used. A footer between the extremes is BUSY/healthy, never a
# condition.
CODEX_CONTEXT_LEFT_THRESHOLD = 10  # codex: exhausted at <= 10% context LEFT
KIRO_CONTEXT_USED_THRESHOLD = 90  # kiro:  exhausted at >= 90% context USED
# codex footer shape "Context NN% left"; the FIRST "Context …% left" is the
# context window (a trailing "· 5h MM% left" is the rate-limit window, not
# context — anchoring on the "Context" keyword keeps them apart).
_CODEX_CONTEXT_FOOTER = re.compile(r"Context\s+(\d+)%\s+left", re.IGNORECASE)
# kiro footer shape "… · <pie glyph> NN%". Match the percent that FOLLOWS a pie
# glyph (◔◑◕●) so a stray percentage elsewhere on the row is not read as context,
# and so the number's provenance (a kiro context gauge) is explicit. The glyph is
# required — a bare "NN%" is not a kiro context reading, and the glyph WITHOUT a
# number is never exhaustion (never on a glyph alone, brief F836).
_KIRO_CONTEXT_FOOTER = re.compile(r"[◔◑◕●]\s*(\d+)%", re.IGNORECASE)
_KIRO_CONTEXT_TIP = re.compile(r"Running low on context\? Type /compact", re.IGNORECASE)

# F836 r3 (#693): the footer percent is located by POSITION relative to the live
# COMPOSER PROMPT, not by the content shape of a matched row. The r1/r2 matchers
# scanned every raw row and decided "this is the footer" from a leading-glyph
# denylist plus a same-row co-signature; that never established the row was the
# LIVE provider status bar, so fenced / indented / bulleted quotes of a status
# bar (which carry the same shape) still fired a false high-confidence
# CONTEXT_EXHAUSTED (r2 gate BLOCKER). The structural invariant is instead:
#
#   * The live status bar sits DIRECTLY BESIDE the newest composer prompt — the
#     row where the seat is waiting for input. Everything above the newest
#     composer is transcript / user territory (a fenced quote, an indented paste,
#     a Markdown bullet), and is NEVER eligible to be the footer, whatever it
#     contains. This holds by construction: a quoted status bar is scrollback and
#     therefore ABOVE the live composer.
#   * The two providers place the bar on OPPOSITE sides of their composer, so the
#     region is defined per provider (proven by the pinned corpus fixtures):
#       - codex composer "› Ask Codex to do anything" → the status bar is redrawn
#         one/two rows BELOW it (codex-footer-*, codex-context-exhausted-1).
#       - kiro composer  " ask a question or describe a task ↵" → the status bar
#         sits one/two rows ABOVE it (kiro-cli-footer-*, status_truth/kiro_cli).
#   * The footer signature is required on the row DIRECTLY ADJACENT to the
#     composer (the first non-blank row on the footer side), never on "any row"
#     of the region — a footer-shaped row separated from the composer by a code
#     fence or prose is scrollback, not the one live status bar.
#   * A pane with NO composer marker (mid-init, alt-screen, a bare transcript
#     paste) has no live status bar to read → footer classification does NOT
#     fire. We never guess.
#
# No leading-glyph denylist and no same-row co-signature are used to decide
# footer-ness any more: exclusion of quoted/fenced/bulleted rows is a consequence
# of the position rule (they are above the composer), not of a content rule.

# The live composer-prompt markers. These are the placeholder rows the provider
# TUI draws where the seat waits for input; each is a stable literal in the real
# capture corpus.
#
# F836 r5 (#693): a plain substring search over the raw pane is NOT proof that a
# row is the live composer (codex EMPIRICAL-GATE-NO): a fenced/indented FULL
# snapshot quote (composer + blank + footer) and a prose row that merely CONTAINS
# the composer phrase both carry the phrase, and the r3/r4 position rule promoted
# them to live chrome. The substring markers below are kept ONLY to answer "does
# this pane mention the composer phrase at all" (cheap short-circuit); a row is
# accepted as the LIVE composer only by the anchored WHOLE-ROW matchers
# (_CODEX_COMPOSER_ROW / _KIRO_COMPOSER_ROW) plus the bottom-of-viewport bound.
_CODEX_COMPOSER_PROMPT = re.compile(r"Ask Codex to do anything", re.IGNORECASE)
_KIRO_COMPOSER_PROMPT = re.compile(r"ask a question or describe a task", re.IGNORECASE)

# F836 r5 (#693): EXACT full-row composer anchors. The live composer is a WHOLE
# row the TUI draws, not a phrase embedded in prose or quoted inside a fence. The
# measured real-capture shapes are (after escape-strip):
#   codex: '› Ask Codex to do anything'
#   kiro : ' ask a question or describe a task ↵'
#          '›  ask a question or describe a task ↵'
#          ' Ask a question or describe a task ↵  ctrl+g: agent monitor'
# so the anchor allows ONLY: an optional leading '›' composer glyph, at most ONE
# leading space (a genuine composer is flush-left or glyph-led — an indented
# PASTE uses >=2 leading spaces), the exact phrase, and optional trailing chrome
# drawn from the DOCUMENTED grammar ONLY — the '↵' submit hint and/or a known
# affordance token ('ctrl+g…' agent-monitor hint, '/copy…' clipboard affordance).
# F836 r6 (#693): the trailing group was previously '(?:\s.*)?', which accepted
# ARBITRARY prose after the phrase (codex r5 SHOULD: a Kiro footer + composer-
# phrase prose row anchored as live). It is now constrained to the chrome grammar
# above: a row whose trailing text is anything other than the '↵' hint or a
# recognised affordance token is NOT the live composer. A row that begins with a
# fence (```), quote ('>' not the '›' glyph), bullet ('•'/'-'/'*'), or >=2 spaces
# of indent is transcript and can NEVER be the live composer, whatever it quotes.
_CODEX_COMPOSER_ROW = re.compile(r"^\u203a Ask Codex to do anything\s*$", re.IGNORECASE)
# Trailing chrome grammar (F836 r6): optional '↵' submit hint, then zero or more
# whitespace-separated known affordance runs. Each affordance run must BEGIN with
# a documented chrome token ('ctrl+g' or '/copy'); everything after that token on
# the row is the affordance's own label ('ctrl+g: agent monitor', '/copy to
# clipboard'). This admits the three measured real shapes and rejects a row that
# merely appends arbitrary transcript prose after the phrase.
_KIRO_COMPOSER_TRAILING_CHROME = r"(?:\s*\u21b5)?(?:\s+(?:ctrl\+g|/copy)\b[^\n]*)?"
_KIRO_COMPOSER_ROW = re.compile(
    r"^(?:\u203a\s*| ?)ask a question or describe a task" + _KIRO_COMPOSER_TRAILING_CHROME + r"$",
    re.IGNORECASE,
)
# Rows that can never be the live composer even if they contain the phrase: a
# leading fence / block-quote / bullet / list marker marks transcript territory.
_TRANSCRIPT_ROW_LEAD = re.compile(r"^\s*(?:```|~~~|>|-|\*|\u2022|\d+[.)]|#)")

# F836 r5 (#693): the live composer + footer are chrome at the BOTTOM of the
# viewport. Measured across all 22 real fixtures with a composer+adjacent footer,
# the composer is within the last 2 NON-BLANK rows of the pane (max 1 non-blank
# row below it: codex draws its footer below the composer; kiro draws a trailing
# '/copy to clipboard' affordance). K bounds how many non-blank rows may sit BELOW
# the composer and still count as live chrome; K=2 (allow <=2 non-blank rows
# below) is the observed max (1) plus a one-row margin. A composer occurrence with
# more transcript below it is scrollback, whatever the row says.
_COMPOSER_MAX_NONBLANK_BELOW = 2


def _last_index(rows: List[str], pattern: "re.Pattern[str]") -> int:
    """Index of the LAST row matching ``pattern``, or -1 when none match."""
    found = -1
    for i, row in enumerate(rows):
        if pattern.search(row):
            found = i
    return found


def _nonblank_below(rows: List[str], idx: int) -> int:
    """Count of non-blank rows strictly BELOW ``idx`` (bottom-of-viewport bound)."""
    return sum(1 for r in rows[idx + 1 :] if r.strip() != "")


def _live_composer_index(rows: List[str], row_anchor: "re.Pattern[str]") -> int:
    """Index of the LIVE composer row, or -1 when there is no live composer.

    F836 r5 (#693): a row is the live composer only when it (1) matches the exact
    WHOLE-ROW composer anchor (never a substring inside prose, never a
    fenced/quoted/bulleted/indented row — those are rejected by the anchor's
    leading-character bound and by ``_TRANSCRIPT_ROW_LEAD``) AND (2) sits at the
    BOTTOM of the viewport (at most ``_COMPOSER_MAX_NONBLANK_BELOW`` non-blank
    rows below it). We scan from the bottom up and return the FIRST (lowest) row
    that satisfies both — a higher occurrence is transcript. Returns -1 when no
    row qualifies, so a quoted full snapshot (whose composer is buried above real
    chrome, or is a fenced/prose row) yields no live composer and cannot fire.
    """
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        # Match the anchor against the RAW row (not stripped): the leading-glyph /
        # leading-whitespace bound in the anchor is load-bearing. A genuine
        # composer is flush-left or '›'-glyph-led with at most one leading space;
        # an INDENTED paste (>=2 leading spaces) of the composer phrase is
        # transcript and must not anchor (F836 r5 indented-snapshot case).
        if not row_anchor.match(row):
            continue
        # A fenced/quoted/bulleted/indented lead is transcript, never the composer
        # — even if the remainder matches the phrase (defence in depth over the
        # anchor's own leading-char bound).
        if _TRANSCRIPT_ROW_LEAD.match(row):
            continue
        if _nonblank_below(rows, i) > _COMPOSER_MAX_NONBLANK_BELOW:
            # A clean-anchored composer that is NOT bottom-of-viewport is anomalous
            # (no real capture puts >1 non-blank row below the composer). Treat it
            # as not-live rather than a hard stop.
            continue
        # Any non-blank row BELOW the composer that is transcript chrome (a closing
        # ``` fence, a quote/bullet lead) means this "composer" is the inner line
        # of a quoted FULL snapshot, not live bottom chrome. Real trailing chrome
        # ('/copy to clipboard') is not a transcript lead, so this rejects the
        # fenced-snapshot case without touching genuine panes.
        if any(r.strip() != "" and _TRANSCRIPT_ROW_LEAD.match(r) for r in rows[i + 1 :]):
            continue
        return i
    return -1


def _codex_footer_percent_row(rows: List[str]) -> Optional[str]:
    """The codex status-bar row BELOW the newest codex composer prompt, or None.

    Position rule (F836 r3): the codex status bar is redrawn directly below the
    live "› Ask Codex to do anything" composer, separated only by blank rows. The
    live status bar is therefore the FIRST non-blank row after the last composer
    prompt; it fires only when that adjacent row bears the codex context figure.
    Rows at/above the composer are transcript/user territory (a quoted or fenced
    "Context 8% left" lives there) and are never eligible; a footer-shaped row
    that is not the row adjacent to the composer (e.g. a fenced quote separated by
    a code fence) is not the live bar. No composer prompt → no live footer → None.

    Bottom-of-viewport invariant (F836 r5, #693): the codex status bar is the
    LAST non-blank row of the live pane — the composer sits directly above it and
    nothing live is drawn below it. A footer-shaped row with ANY non-blank row
    below it (e.g. a closing ``` fence, more transcript) is a quoted snapshot, not
    the live bar, and does not fire. This is what rejects a fenced FULL snapshot
    whose inner composer row happens to anchor cleanly (codex EMPIRICAL-GATE-NO).
    """
    composer = _live_composer_index(rows, _CODEX_COMPOSER_ROW)
    if composer < 0:
        return None
    saw_blank = False
    for offset, row in enumerate(rows[composer + 1 :]):
        idx = composer + 1 + offset
        if row.strip() == "":
            saw_blank = True
            continue
        # First non-blank row below the composer: the live status bar iff a blank
        # separator precedes it, it carries the context figure, AND it is the
        # BOTTOM chrome row of the pane (no non-blank row below it). Otherwise
        # there is no live footer (a flush or non-bottom footer-shaped row is
        # transcript / a quoted snapshot).
        if saw_blank and _CODEX_CONTEXT_FOOTER.search(row) and _nonblank_below(rows, idx) == 0:
            return row.strip()
        return None
    return None


def _kiro_footer_percent_row(rows: List[str]) -> Optional[str]:
    """The kiro status-bar row ABOVE the newest kiro composer prompt, or None.

    Position rule (F836 r3): the kiro status bar sits directly above the live
    " ask a question or describe a task ↵" composer, separated only by blank
    rows. The live status bar is therefore the FIRST non-blank row scanning
    UPWARD from the last composer prompt; it fires only when that adjacent row
    bears the pie-glyph percent. A fenced/quoted "… ● 92%" further up (separated
    from the composer by a code fence or prose) is not the row adjacent to the
    composer and is never eligible; rows at/below the composer are trailing chrome
    ("/copy to clipboard"). No composer prompt → no live footer → None.

    Live-bar co-signature (F836 r4, S1): the live TUI redraws bar + blank +
    composer — every real captured kiro pane separates the status bar from the
    composer by at least one blank row. A footer-shaped row FLUSH against the
    composer (no blank between) is a pasted/quoted status line, not the live bar,
    so it does not fire.
    """
    composer = _live_composer_index(rows, _KIRO_COMPOSER_ROW)
    if composer < 0:
        return None
    saw_blank = False
    for row in reversed(rows[:composer]):
        if row.strip() == "":
            saw_blank = True
            continue
        # First non-blank row above the composer: the live status bar iff a blank
        # separator precedes it AND it carries the pie-glyph percent; otherwise
        # there is no live footer (a flush footer-shaped row is transcript).
        if saw_blank and _KIRO_CONTEXT_FOOTER.search(row):
            return row.strip()
        return None
    return None


# DIALOG_BLOCKED anchors.
_CODEX_TRUST = re.compile(
    r"subdirectory of a Git project\. Trusting will apply to the repository root",
    re.IGNORECASE,
)
_GROK_TRUST = re.compile(r"Do you trust the contents of this directory\?", re.IGNORECASE)
_CLAUDE_LOGIN = re.compile(r"Select login method:", re.IGNORECASE)

# TRANSIENT_OVERLOAD anchors.
_KIRO_TRAFFIC = re.compile(
    r"experiencing a high volume of traffic\. Try changing the model", re.IGNORECASE
)
_CODEX_CAPACITY = re.compile(r"^⚠ Selected model is at capacity", re.IGNORECASE)
# F738 (#595): cline self-abort. The pane blames "another client", but there is
# none — it is the fallback arm of cline's abort-reason string, printed when its
# own loop detector (5 consecutive byte-identical tool calls) or the
# consecutive-mistake limit aborts the run from inside the same process. It is
# TRANSIENT_OVERLOAD, not CAPPED/PROC_EXITED: cline preserves session state, so
# the run is recovered by re-dispatching the same message (policy NONE — this
# must never rebind a lane or stop-and-ask). Precedence 6 beats BUSY (7), so the
# abort wins over the `[run_commands]` churn still on the pane above it.
_CLINE_SELF_ABORT = re.compile(r"\[abort\] aborted by another client")

# BUSY anchors (precedence 7 — last).
_CODEX_BUSY = re.compile(r"Working \(.*esc to interrupt\)", re.IGNORECASE)
_KIRO_BUSY = re.compile(r"Thinking\.\.\. \(esc to cancel\)|Kiro is working", re.IGNORECASE)
_CLAUDE_BUSY = re.compile(r"[✶✢✽✻✳·*][^\n]*\u2026|Cooked for|Cultivat", re.IGNORECASE)
# F792 (#649): claude_code's subagent-wait line — "✻ Waiting for N background
# agent(s) to finish" (glyph optional/animating). An EXPECTED not-busy state:
# the seat ended its own turn and is idle while an in-harness Agent lane runs.
# Kept in sync with claude_code.SUBAGENT_WAIT_PATTERN by shape (a provider module
# must not import a peer provider's regex here — same convention as the BUSY
# anchors). The "agent" keyword after "Waiting for" is what distinguishes it from
# the GH #392 "dynamic workflow/task to finish" line (which stays BUSY/working).
_CLAUDE_SUBAGENT_WAIT = re.compile(
    r"[✶✢✽✻✳·*][ \t\xa0]+Waiting for\b[^\n]*\bagents?\b", re.IGNORECASE
)
_GROK_BUSY = re.compile(r"Waiting for response", re.IGNORECASE)
_CLINE_BUSY = re.compile(r"\[thinking\]|\[run_commands\]", re.IGNORECASE)

# F862 (#718): the chatgpt_web runner is a deterministic PROCESS, not a TUI whose
# chrome carries a natural-language banner. When it hits a typed failure surface
# (D4) it prints ONE structured marker line to its pane —
# ``[chatgpt_web] CONDITION <code>[ reset=<hint>]`` — and the classifier below
# maps the code onto the condition plane. Classifying on this MARKER (not on any
# assistant text, D4 Do-NOT) keeps detection keyed to chrome/driver state.
_CHATGPT_WEB_CONDITION = re.compile(
    r"\[chatgpt_web\]\s+CONDITION\s+(?P<code>[a-z_]+)(?:\s+reset=(?P<reset>[^\n]+))?",
)

# PROC_EXITED (cline text anchor; the process-state path is handled separately by
# the provider via pane_current_command == shell_baseline — see D5/precedence 1).
_CLINE_PROC_EXITED = re.compile(r"\[Command exited with code \d+\]")

# F775 (#632): reset anchor for scoping the PROC_EXITED text evidence. A stale
# ``[Command exited with code N]`` line in scrollback must NOT keep re-asserting
# PROC_EXITED once the pane has moved past it. The reliable boundary is the SHELL
# PROMPT — the starship ``❯`` baseline the pane returns to when the process ends
# and a NEW session/turn begins below it. A chained cline tool marker
# (``[run_commands]`` / ``[search_codebase]`` …) is NOT a boundary: cline runs
# several tool calls inside ONE turn and ``[Command exited …]`` is just one tool's
# result, so the exit line is still live evidence while the same turn continues
# (see fixture cline-cli-proc-exited-1: a ``[search_codebase]`` follows the exit
# within the same turn and the corpus still expects PROC_EXITED). Tail-limiting to
# BUSY_TAIL_ROWS + the F752 quiescent downgrade handle the live-worker sticky case.
_CLINE_SHELL_PROMPT = re.compile(r"^\s*❯")

# F832 (#689): codex replays the PRIOR conversation transcript on `codex resume
# <uuid>` (e.g. after an account swap). An OLD "You've hit your usage limit" line
# from a since-superseded account is redrawn ABOVE the "Resuming session…" boot
# marker, and the CAPPED classifier fires on it — a false CAPPED on a terminal
# that is in fact logged in and working. The fix scopes the codex CAPPED scan to
# pane text that belongs to the CURRENT incarnation: only rows AFTER the resume
# boot marker. A cap line above that marker is replayed history and must NOT
# match; a live cap line below it (the codex composer is always redrawn at the
# bottom, so a genuine current cap sits below the marker) still does.
#
# The discriminator is the RESUME MARKER alone, NOT the composer prompt: codex
# always redraws "› Ask Codex …" at the bottom of the pane, so in the normal
# (non-resumed) layout a genuine current cap banner sits ABOVE the composer
# prompt (see fixture codex-capped-2). Keying on the prompt would wrongly scope
# out every real cap; keying on the resume marker only excludes true replay.
_CODEX_RESUME_MARKER = re.compile(r"Resuming session", re.IGNORECASE)


def _codex_live_rows(rows: List[str]) -> List[str]:
    """F832 (#689): the rows belonging to the CURRENT codex incarnation.

    Returns only the rows AFTER the last resume boot marker ("Resuming
    session"). When no resume marker is present the whole buffer is returned
    unchanged — a fresh, never-resumed pane has no replayed history to exclude.
    Used to keep the codex CAPPED scan off a cap banner replayed from the prior
    transcript, WITHOUT scoping out a genuine current cap in the normal layout
    (where the cap banner sits above the always-bottom composer prompt).
    """
    boundary = -1
    for i, row in enumerate(rows):
        if _CODEX_RESUME_MARKER.search(row):
            boundary = i
    if boundary < 0:
        return rows
    return rows[boundary + 1 :]


def _scoped_proc_exited_evidence(brows: List[str]) -> Optional[str]:
    """F775 (#632): the exit-code line ONLY when it is live evidence.

    Scopes the ``[Command exited with code N]`` scan to the BUSY_TAIL_ROWS window
    (same tail bound BUSY uses — a statement about the present is only believable
    in the live tail) AND clears it once a NEWER shell prompt appears after it
    (the process returned to shell / a new turn began below the exit line).
    Returns the exit-code row, or ``None`` when the evidence is stale or absent.
    """
    tail = brows[-BUSY_TAIL_ROWS:]
    last_exit = -1
    for i, row in enumerate(tail):
        if _CLINE_PROC_EXITED.search(row):
            last_exit = i
    if last_exit < 0:
        return None
    for row in tail[last_exit + 1 :]:
        if _CLINE_SHELL_PROMPT.match(row):
            return None
    return tail[last_exit].strip()


def _first_evidence(rows: List[str], pattern: "re.Pattern[str]") -> Optional[str]:
    for row in rows:
        if pattern.search(row):
            return row.strip()
    return None


def _classify_capped(provider: str, brows: List[str]) -> Optional[Condition]:
    """CAPPED for the provider, banner-only (D2). Reset≠cap guard (§2.4).

    F832 (#689): for codex the caller passes banner rows already scoped to the
    CURRENT incarnation (rows after the last resume boot marker — see
    ``_codex_live_rows``, whose sole boundary is "Resuming session"), so a cap
    line replayed from the prior transcript never matches. The composer prompt is
    NOT a boundary here: codex always redraws the composer at the bottom, so a
    genuine current cap banner sits above it in the normal (non-resumed) layout.
    """
    cap_pat: Optional["re.Pattern[str]"] = {
        "codex": _CODEX_CAP_HARD,
        "kiro_cli": _KIRO_CAP,
        "grok_cli": _GROK_CAP,
        "cline_cli": _CLINE_CAP,
    }.get(provider)
    if cap_pat is None:
        return None
    ev = _first_evidence(brows, cap_pat)
    if ev is None:
        return None
    subtype = {
        "codex": "usage_limit_hard",
        "kiro_cli": "monthly_usage_limit",
        "grok_cli": "weekly_limit_choice",
        "cline_cli": "usage_limit_monthly",
    }[provider]
    hint = _extract_reset_hint("\n".join(brows))
    return Condition(
        kind=ConditionKind.CAPPED,
        provider=provider,
        subtype=subtype,
        evidence=ev,
        confidence=Confidence.HIGH,
        reset_hint=hint,
    )


def _classify_auth(provider: str, brows: List[str]) -> Optional[Condition]:
    if provider == "codex":
        ev = _first_evidence(brows, _CODEX_AUTH)
        if ev:
            return Condition(
                ConditionKind.AUTH_EXPIRED,
                provider,
                "token_refresh_failed",
                ev,
                Confidence.HIGH,
                reset_hint="sign in again",
            )
    if provider == "claude_code":
        ev = _first_evidence(brows, _CLAUDE_AUTH)
        if ev:
            return Condition(
                ConditionKind.AUTH_EXPIRED, provider, "oauth_expired", ev, Confidence.HIGH
            )
    return None


def _classify_net(provider: str, brows: List[str]) -> Optional[Condition]:
    # kiro_cli is CLOSED (status_truth/kiro_cli/error-3). The other four ship
    # DISABLED until a real screen lands (§7 GAP plan) — no anchor, no match.
    if provider != "kiro_cli":
        return None
    ev = _first_evidence(brows, _NET_INTERRUPTED)
    if ev:
        return Condition(
            ConditionKind.NET_INTERRUPTED, provider, "connection_interrupted", ev, Confidence.HIGH
        )
    return None


def _classify_context(
    provider: str, brows: List[str], raw_rows: Optional[List[str]] = None
) -> Optional[Condition]:
    # F836 r3 (#693): the footer status bar is located by POSITION relative to the
    # live composer prompt, not by the content shape of a matched row (r2 gate
    # BLOCKER: fenced/indented/bulleted quotes of a status bar carry the same
    # shape and fired a false CONTEXT_EXHAUSTED). _codex_footer_percent_row /
    # _kiro_footer_percent_row return the LIVE status-bar row (below the codex
    # composer, above the kiro composer) or None when there is no composer to
    # anchor on. Anything above the newest composer is transcript/user territory
    # and never eligible. The scan runs on the RAW pane rows (the status bar and
    # the composer both sit in the user-region continuation that banner_rows
    # suppresses); the softer kiro low-context TIP still scans brows.
    rows = raw_rows if raw_rows is not None else brows
    if provider == "codex":
        # codex: NN is context REMAINING → exhausted at NN <= threshold.
        row = _codex_footer_percent_row(rows)
        if row is not None:
            m = _CODEX_CONTEXT_FOOTER.search(row)
            if m and int(m.group(1)) <= CODEX_CONTEXT_LEFT_THRESHOLD:
                return Condition(
                    ConditionKind.CONTEXT_EXHAUSTED,
                    provider,
                    "footer_percent_status",
                    row,
                    # F836 r6 (#693): footer_percent_status caps at MEDIUM, never
                    # HIGH. The classifier's ONLY input is plaintext pane bytes
                    # (base.py:348-376, fleet_app.py:577-592, condition.py:952),
                    # so no arrangement of row anchors can be a second, live-only
                    # signal — a pasted/truncated full snapshot that ends at the
                    # viewport bottom preserves every structural predicate (codex
                    # EMPIRICAL-GATE-NO r5). MEDIUM still surfaces on fleet/TUI/CLI
                    # via ``should_deliver`` but the inbox (acting) leg is DECLINED
                    # by ``drain_class_declines_inbox`` (delivery_ledger.py) — this
                    # is the ADVISORY, never-hard-stop class, mirroring
                    # ``low_context_tip``. A hard stop would need a genuinely
                    # independent live signal (a cursor/provider-state fact through
                    # the capture boundary), which plaintext cannot supply.
                    Confidence.MEDIUM,
                )
        return None
    if provider == "kiro_cli":
        # kiro: NN is context USED (pie glyph ◔◑◕● precedes it) → exhausted at
        # NN >= threshold. A glyph alone, or a healthy low USED% (e.g. ◑ 30%), is
        # NOT exhaustion (F836 false positive).
        row = _kiro_footer_percent_row(rows)
        if row is not None:
            m = _KIRO_CONTEXT_FOOTER.search(row)
            if m and int(m.group(1)) >= KIRO_CONTEXT_USED_THRESHOLD:
                return Condition(
                    ConditionKind.CONTEXT_EXHAUSTED,
                    provider,
                    "footer_percent_status",
                    row,
                    # F836 r6 (#693): caps at MEDIUM, never HIGH — see the codex
                    # arm above. Plaintext-only footer matches are advisory
                    # (fleet/TUI/CLI) and DECLINE the inbox leg; never a hard stop.
                    Confidence.MEDIUM,
                )
        # The welcome/low-context TIP is a softer, medium-confidence signal.
        ev = _first_evidence(brows, _KIRO_CONTEXT_TIP)
        if ev:
            return Condition(
                ConditionKind.CONTEXT_EXHAUSTED,
                provider,
                "low_context_tip",
                ev,
                Confidence.MEDIUM,
                reset_hint="/compact",
            )
    return None


def _classify_dialog(provider: str, brows: List[str]) -> Optional[Condition]:
    if provider == "codex":
        ev = _first_evidence(brows, _CODEX_TRUST)
        if ev:
            return Condition(
                ConditionKind.DIALOG_BLOCKED, provider, "trust_dir_dialog", ev, Confidence.HIGH
            )
    if provider == "grok_cli":
        ev = _first_evidence(brows, _GROK_TRUST)
        if ev:
            return Condition(
                ConditionKind.DIALOG_BLOCKED, provider, "trust_dir_dialog", ev, Confidence.HIGH
            )
    if provider == "claude_code":
        ev = _first_evidence(brows, _CLAUDE_LOGIN)
        if ev:
            return Condition(
                ConditionKind.DIALOG_BLOCKED, provider, "login_wizard", ev, Confidence.HIGH
            )
    return None


def _classify_transient(provider: str, brows: List[str]) -> Optional[Condition]:
    # A cap is never transient: 'usage limit' is a TRANSIENT_ERROR_EXCLUSION
    # (codex.py:216-223). Only match a transient anchor NOT excluded.
    if provider == "kiro_cli":
        ev = _first_evidence(brows, _KIRO_TRAFFIC)
        if ev:
            return Condition(
                ConditionKind.TRANSIENT_OVERLOAD,
                provider,
                "model_high_traffic",
                ev,
                Confidence.HIGH,
                reset_hint="Try changing the model and re-running your prompt",
            )
    if provider == "cline_cli":
        ev = _first_evidence(brows, _CLINE_SELF_ABORT)
        if ev:
            return Condition(
                ConditionKind.TRANSIENT_OVERLOAD,
                provider,
                "self_abort_loop_limit",
                ev,
                Confidence.HIGH,
                reset_hint="re-dispatch the same message; cline preserved session state",
            )
    if provider == "codex":
        for row in brows:
            if not _CODEX_CAPACITY.search(row):
                continue
            excluded = any(re.search(p, row) for p in TRANSIENT_ERROR_EXCLUSIONS)
            is_transient = any(re.search(p, row) for p in TRANSIENT_API_ERROR_PATTERNS)
            if is_transient and not excluded:
                return Condition(
                    ConditionKind.TRANSIENT_OVERLOAD,
                    provider,
                    "model_at_capacity",
                    row.strip(),
                    Confidence.HIGH,
                )
    return None


#: F752 (#609): BUSY is a statement about the PRESENT, so its anchor is only
#: believable in the live tail of the pane. Every other kind matches a banner
#: that stays true while it is on screen; a busy marker does not. cline is the
#: proof: ``[thinking]``/``[run_commands]`` are printed LOG lines, not a spinner
#: that erases itself, so a whole-buffer scan keeps matching a run that ended
#: hours ago (sample: terminal 1243fb68 re-emitted ``tool_churn`` at 10:05:48Z on
#: a pane parked since 08:21Z). Scanning only the tail matches the rest of the
#: tree's liveness convention (``pane_liveness.PANE_LIVENESS_TAIL_LINES`` = 45,
#: not imported here — a provider module must not pull in a service).
BUSY_TAIL_ROWS: int = 45


def _codex_activity_evidence(pane: str) -> Optional[str]:
    """F782 (#639): the first codex activity bullet newer than the last ``›``
    prompt, for use as the BUSY condition's evidence row. Mirrors the position
    walk in ``codex.codex_activity_marker_live`` so evidence and verdict agree.
    """
    rows = [strip_terminal_escapes(r) for r in pane.splitlines()]
    last_prompt = -1
    for i, row in enumerate(rows):
        stripped = row.lstrip()
        if stripped.startswith("›") and "Ask Codex to do anything" not in row:
            last_prompt = i
    for row in rows[last_prompt + 1 :]:
        if CODEX_ACTIVITY_MARKER_PATTERN.match(row):
            return row.strip()
    return None


def _classify_busy(provider: str, brows: List[str]) -> Optional[Condition]:
    pat, subtype = {
        "codex": (_CODEX_BUSY, "working_marker"),
        "kiro_cli": (_KIRO_BUSY, "thinking_spinner"),
        "claude_code": (_CLAUDE_BUSY, "asterisk_spinner"),
        "grok_cli": (_GROK_BUSY, "spinner_waiting"),
        "cline_cli": (_CLINE_BUSY, "tool_churn"),
    }.get(provider, (None, ""))
    if pat is None:
        return None
    ev = _first_evidence(brows[-BUSY_TAIL_ROWS:], pat)
    if ev:
        return Condition(ConditionKind.BUSY, provider, subtype, ev, Confidence.HIGH)
    return None


def _classify_waiting_on_subagents(provider: str, brows: List[str]) -> Optional[Condition]:
    """F792 (#649): claude_code seat idle while a background AGENT lane runs.

    Matches the "Waiting for N background agent(s) to finish" line in the live
    tail (a statement about the present, like BUSY — tail-scoped so a stale
    scrollback line cannot re-assert it). Returns the EXPECTED, not-busy
    ``WAITING_ON_SUBAGENTS`` condition. Only claude_code renders this line."""
    if provider != "claude_code":
        return None
    ev = _first_evidence(brows[-BUSY_TAIL_ROWS:], _CLAUDE_SUBAGENT_WAIT)
    if ev:
        return Condition(
            ConditionKind.WAITING_ON_SUBAGENTS,
            provider,
            "background_agents",
            ev,
            Confidence.HIGH,
        )
    return None


# F862 (#718): map the chatgpt_web runner's structured CONDITION marker (D4) onto
# the condition plane. The runner prints ``[chatgpt_web] CONDITION <code>`` where
# <code> is a RunnerErrorCode value; this translates the human-gated / cap /
# net / auth codes into the closed taxonomy. bot_flagged rides DIALOG_BLOCKED
# with the ``bot_flagged`` subtype so the auto-responder wait branch carries it to
# WAITING_USER_ANSWER (D4). Codes that map to a plain ERROR TerminalStatus
# (ui_changed, model_drift, truncated_answer, invalid_verdict, submit_unknown,
# report_invalid, read_forbidden, egress_forbidden, attachment_identity,
# upload_unconfirmed, pin_drift) carry NO condition — they are ordinary ERROR and
# return None here.
_CHATGPT_WEB_CODE_MAP: Dict[str, Tuple[ConditionKind, str]] = {
    "auth_wall": (ConditionKind.AUTH_EXPIRED, "auth_wall"),
    "captcha": (ConditionKind.DIALOG_BLOCKED, "captcha"),
    "bot_flagged": (ConditionKind.DIALOG_BLOCKED, "bot_flagged"),
    "access_denied": (ConditionKind.DIALOG_BLOCKED, "access_denied"),
    "quota": (ConditionKind.CAPPED, "quota"),
    "net_interrupted": (ConditionKind.NET_INTERRUPTED, "reconnect_once"),
    "context_too_large": (ConditionKind.CONTEXT_EXHAUSTED, "bundle_over_limit"),
    "proc_exited": (ConditionKind.PROC_EXITED, "browser_crash"),
}


def _classify_chatgpt_web(provider: str, brows: List[str]) -> Optional[Condition]:
    """F862 (#718): translate the runner's CONDITION marker into a Condition (D4).

    Classifies on the STRUCTURED marker the runner prints, never on assistant
    text. Only the human-gated / cap / net / auth / context / proc classes map to
    a condition; every other typed code is a plain ERROR and returns None."""
    if provider != "chatgpt_web":
        return None
    for row in brows:
        match = _CHATGPT_WEB_CONDITION.search(row)
        if not match:
            continue
        code = match.group("code")
        mapped = _CHATGPT_WEB_CODE_MAP.get(code)
        if mapped is None:
            return None
        kind, subtype = mapped
        reset = match.group("reset")
        return Condition(
            kind,
            provider,
            subtype,
            row.strip(),
            Confidence.HIGH,
            reset_hint=reset.strip() if reset else None,
        )
    return None


# The per-kind classifiers, applied then ranked by §2.2 precedence.
# NOTE: CAPPED and CONTEXT are NOT in this tuple — both are dispatched explicitly
# in classify_condition. CAPPED so codex can scope the scan to the current
# incarnation (F832 #689). CONTEXT so the footer-percent scan runs on the RAW
# pane rows (F836 #693): the status footer sits BELOW the composer prompt, which
# banner_rows suppresses as user-region continuation.
_KIND_CLASSIFIERS: Tuple[Callable[[str, List[str]], Optional[Condition]], ...] = (
    _classify_auth,
    _classify_net,
    _classify_dialog,
    _classify_transient,
    _classify_busy,
    _classify_waiting_on_subagents,
    _classify_chatgpt_web,
)


def classify_condition(
    pane: str,
    provider: str,
    *,
    proc_exited: bool = False,
    host: Optional[str] = None,
    credential_plane: Optional[str] = None,
) -> Optional[Condition]:
    """Classify the operating condition of ``pane`` for ``provider`` (D1/D2/D5).

    Returns the highest-precedence :class:`Condition` in the closed taxonomy, or
    ``None`` when nothing matched. ``proc_exited`` is the PROCESS-STATE fact from
    the provider (``pane_current_command == shell_baseline`` for cline, D5
    precedence 1) — a dead process outranks any residual pane text and is
    ``confidence=low`` when inferred from process-state alone with no exit-code
    line (D3/AC7); a text ``[Command exited with code N]`` line lifts it to high.

    ``host``/``credential_plane`` are attribution (D6): when
    ``credential_plane`` is set the returned condition carries
    ``scope="credential_plane"`` and is advisory-only for the policy layer.
    """
    brows = banner_rows(pane)
    candidates: List[Condition] = []

    # PROC_EXITED (precedence 1): process-state fact OR a text exit-code line.
    # F775 (#632): the text line is scoped to the live tail and cleared by a
    # newer turn marker / shell prompt, so a stale scrollback exit line no longer
    # re-asserts PROC_EXITED on a worker that has since moved on.
    text_exit = _scoped_proc_exited_evidence(brows) if provider == "cline_cli" else None
    if text_exit is not None:
        candidates.append(
            Condition(
                ConditionKind.PROC_EXITED,
                provider,
                "command_exit_code",
                text_exit,
                Confidence.HIGH,
            )
        )
    elif proc_exited:
        candidates.append(
            Condition(
                ConditionKind.PROC_EXITED,
                provider,
                "shell_baseline_return",
                "pane_current_command == shell_baseline",
                Confidence.LOW,
            )
        )

    # CAPPED (precedence 4): dispatched explicitly so codex can scope the scan to
    # the CURRENT incarnation. F832 (#689): on a RESUMED codex terminal the prior
    # transcript (including an old "usage limit" line) is replayed ABOVE the
    # "Resuming session" boot marker; scanning the whole buffer fired a false
    # CAPPED. For codex we scope the banner rows to those AFTER the last resume
    # boot marker (_codex_live_rows); a never-resumed pane has no marker and is
    # scanned whole. Other providers scan unchanged.
    raw_rows = [strip_terminal_escapes(r) for r in pane.splitlines()]
    if provider == "codex":
        live_rows = _codex_live_rows(raw_rows)
        capped_brows = banner_rows("\n".join(live_rows))
    else:
        capped_brows = brows
    capped = _classify_capped(provider, capped_brows)
    if capped is not None:
        candidates.append(capped)

    # CONTEXT (precedence 5): F836 (#693) — the footer-percent scan runs on the
    # RAW pane rows (the status footer sits below the composer prompt, which
    # banner_rows suppresses); the kiro low-context TIP still scans brows.
    context = _classify_context(provider, brows, raw_rows)
    if context is not None:
        candidates.append(context)

    for classifier in _KIND_CLASSIFIERS:
        cond = classifier(provider, brows)
        if cond is not None:
            candidates.append(cond)

    # F782 (#639): codex has a SECOND live-work marker class beyond the
    # ``• Working (… esc to interrupt)`` footer that ``_classify_busy`` matches:
    # the ``• Waiting for agents`` wait loop and any ``• <Verb>ing …`` activity
    # bullet newer than the last ``›`` prompt. That test is position-aware, so it
    # runs on the RAW pane (``banner_rows`` strips the composer prompt used as the
    # position anchor), not on ``brows``. Only add it when the footer path did not
    # already surface BUSY, so evidence stays specific.
    if provider == "codex" and not any(c.kind is ConditionKind.BUSY for c in candidates):
        if codex_activity_marker_live(pane):
            ev = _codex_activity_evidence(pane)
            candidates.append(
                Condition(
                    ConditionKind.BUSY,
                    provider,
                    "activity_marker",
                    ev or "• activity marker",
                    Confidence.HIGH,
                )
            )

    if not candidates:
        return None

    winner = min(candidates, key=lambda c: PRECEDENCE[c.kind])

    # D6 credential-plane attribution / scope.
    scope = "credential_plane" if credential_plane else "provider"
    if host is not None or credential_plane is not None or scope != "provider":
        winner = Condition(
            kind=winner.kind,
            provider=winner.provider,
            subtype=winner.subtype,
            evidence=winner.evidence,
            confidence=winner.confidence,
            reset_hint=winner.reset_hint,
            host=host,
            credential_plane=credential_plane,
            scope=scope,
        )
    return winner


def should_deliver(cond: Condition) -> bool:
    """D3 confidence gate: only ``high``/``medium`` surface an event."""
    return cond.confidence in (Confidence.HIGH, Confidence.MEDIUM)


# ─── D4: ONE event, fanned out; never three producers ──────────────────────────
@dataclass(frozen=True)
class DeliveryResult:
    """The outcome of one delivery attempt (D4). ``delivered`` is False on a
    suppressed repeat (same tuple within an epoch) or a sub-threshold confidence
    (D3). The three surfaces are driven from this ONE result — never three
    independent producers. ``inbox_pushes`` counts the supervisor inbox pushes
    this call actually performed (0 or 1)."""

    delivered: bool
    fleet_field: Optional[str]
    inbox_pushes: int
    reason: str


# The three fan-out sinks the delivery layer drives from ONE event (§3). Each is
# injected so production wires the real effect (status-monitor fleet field, the
# inbox enqueue, the CLI/bus projection) while tests inject fakes. A sink raising
# must never break the transition path — the delivery layer swallows sink errors.
FleetSink = Callable[[str, Optional[str]], None]  # (terminal_id, fleet_label|None)
InboxSink = Callable[[str, "Condition"], None]  # (terminal_id, condition) -> one push
CliSink = Callable[[str, "Condition", str], None]  # (terminal_id, condition, fleet_label)


# ─── F642 D5/D7: durable condition-decision log integration ─────────────────────
# The store is injected so production wires the DB-backed ``condition_ledger``
# (durable across a cao-server restart, AC9) while tests wire a fake or a real
# DB. When NO store is present, ``ConditionDelivery`` falls back to the in-memory
# ``_last`` dict — byte-identical to F611's behaviour, so nothing that does not
# opt into the spine changes.
class ConditionLogStore(Protocol):
    """Protocol for the durable decision log (F642 §2, D7). A production impl
    wraps ``clients.database.suppress_condition_by_log`` /
    ``record_condition_decision``; a test impl can be an in-memory list.

    F807 (#664): a structural :class:`typing.Protocol` so the production
    ``DbConditionLogStore`` (which does NOT nominally inherit this) satisfies the
    ``log_store`` parameter under ``mypy --strict`` — the seam has always been
    duck-typed at runtime; this only makes the type checker agree."""

    def should_suppress(self, terminal_id: str, kind: str, subtype: str, epoch: int) -> bool:
        """D7: read the latest memory-updating row and decide suppression."""
        ...

    def record(
        self,
        *,
        terminal_id: str,
        decision: str,
        kind: Optional[str],
        subtype: Optional[str],
        epoch: Optional[int],
        surfaces: Optional[str] = None,
        suppressed_reason: Optional[str] = None,
        inbox_message_id: Optional[int] = None,
    ) -> None:
        """Append one decision row (one per ``deliver()`` exit, D5/AC20/AC24)."""
        ...


class ConditionDelivery:
    """The single delivery seam (D4, blueprint §3).

    ONE typed event per terminal TRANSITION — a change in the ``(kind, subtype)``
    pair for a terminal. De-dup key = ``(terminal_id, kind, subtype, epoch)``; a
    repeat of the same tuple within an epoch is suppressed. A new epoch (a fresh
    dispatch) re-arms. The delivery layer fans the ONE event to three surfaces:
    the fleet ``condition`` field, ONE supervisor inbox push, and the ``cao`` CLI
    projection. Callers never emit from more than one producer.

    The three surfaces are PERFORMED here through injected sinks (not merely
    modeled): production passes the status-monitor fleet setter, the inbox
    enqueue, and the bus/CLI projector; tests pass fakes and assert side effects.
    A ``None`` sink is a no-op leg (e.g. a caller that only wants the fleet
    field). Sink exceptions are swallowed so a delivery failure never breaks the
    status transition that triggered it.

    F642: when a :class:`ConditionLogStore` is injected, de-dup consults the
    DURABLE log (surviving a restart, AC9) instead of the in-memory dict, the
    kind→surfaces map (D5) gates the INBOX leg (BUSY-class kinds fire fleet+bus
    but NOT inbox), and EVERY ``deliver()`` exit writes a decision row —
    including the confidence gate (AC24), the one otherwise-invisible outcome.
    """

    def __init__(
        self,
        *,
        fleet_sink: Optional[FleetSink] = None,
        inbox_sink: Optional[InboxSink] = None,
        cli_sink: Optional[CliSink] = None,
        log_store: Optional[ConditionLogStore] = None,
    ) -> None:
        # terminal_id -> last delivered (kind, subtype, epoch)
        self._last: Dict[str, Tuple[str, str, int]] = {}
        self._fleet_sink = fleet_sink
        self._inbox_sink = inbox_sink
        self._cli_sink = cli_sink
        self._log_store = log_store

    def deliver(self, terminal_id: str, cond: Optional[Condition], *, epoch: int) -> DeliveryResult:
        if cond is None:
            # A transition that clears any condition: drop the fleet label so the
            # row stops rendering a stale CAPPED/BLOCKED. No inbox/CLI on a clear.
            self._set_fleet(terminal_id, None)
            self._last.pop(terminal_id, None)
            # F642 D7: a clear writes an explicit `cleared` decision row (NULL
            # tuple), mirroring the pop at the in-memory path — the durable memory
            # is re-armed without losing the history (AC21(c)).
            self._record(terminal_id, "cleared", None, None, None)
            return DeliveryResult(False, None, 0, "no_condition")
        if not should_deliver(cond):
            # D3: low confidence logs but never surfaces on ANY of the three.
            # F642 AC24: the gate still writes a `gated` decision row — the one
            # routing outcome that would otherwise be invisible everywhere. It is
            # SKIPPED by the de-dup comparison (it moves no memory).
            self._record(terminal_id, "gated", cond.kind.value, cond.subtype, epoch)
            return DeliveryResult(False, None, 0, "confidence_below_gate")
        label = self._fleet_label(cond)
        key = (cond.kind.value, cond.subtype, epoch)
        if self._is_duplicate(terminal_id, cond, epoch, key):
            # D4/D7: same tuple as the latest DELIVERED row → suppress the repeat.
            # The fleet field is idempotently re-affirmed; the inbox push and CLI
            # projection are NOT re-fired. A `deduped` audit row is written
            # (AC20) and SKIPPED by future comparisons.
            self._set_fleet(terminal_id, label)
            self._record(
                terminal_id,
                "deduped",
                cond.kind.value,
                cond.subtype,
                epoch,
                suppressed_reason="dedup_epoch",
            )
            return DeliveryResult(False, label, 0, "deduped_same_epoch")
        self._last[terminal_id] = key
        # ONE event → three surfaces, fanned out here (never three producers).
        self._set_fleet(terminal_id, label)
        # F642 D5 / F790 (#647): the drain-class predicate gates the INBOX leg. A
        # BUSY-class kind OR a command_exit PROC_EXITED fires fleet+bus but
        # declines the inbox push — the SAME class the supervisor-inbox-drain hook
        # withholds, moved upstream to the producer so no native envelope can
        # bypass it. The memory is STILL set (this stays a `delivered` decision),
        # so the row stays inside the de-dup comparison (r3/B1) — recorded via
        # suppressed_reason='busy_class'.
        inbox_declined = self._inbox_declined(cond.kind.value, cond.subtype)
        pushes = 0 if inbox_declined else self._push_inbox(terminal_id, cond)
        self._project_cli(terminal_id, cond, label)
        self._record(
            terminal_id,
            "delivered",
            cond.kind.value,
            cond.subtype,
            epoch,
            surfaces=self._surfaces_str(cond.kind.value, cond.subtype),
            suppressed_reason="busy_class" if inbox_declined else None,
        )
        return DeliveryResult(True, label, pushes, "delivered")

    # ── F642 helpers ──────────────────────────────────────────────────────────
    def _is_duplicate(
        self,
        terminal_id: str,
        cond: "Condition",
        epoch: int,
        key: Tuple[str, str, int],
    ) -> bool:
        if self._log_store is not None:
            return self._log_store.should_suppress(
                terminal_id, cond.kind.value, cond.subtype, epoch
            )
        return self._last.get(terminal_id) == key

    def _inbox_declined(self, kind: str, subtype: str) -> bool:
        """D5 / F790 (#647) / F807 (#664): does the inbox leg decline for this
        condition?

        Declined when EITHER the drain-class predicate matches (BUSY, a
        command_exit PROC_EXITED, a CONTEXT_EXHAUSTED ``low_context_tip``, or a
        CONTEXT_EXHAUSTED ``footer_percent_status`` — the plaintext-only footer
        reading that F836 r6 (#693) made ADVISORY, never a hard stop — the SAME
        class the supervisor-inbox-drain hook withholds, F718 #574 / F807 / F836)
        OR the F642 routing map (``KIND_SURFACES``) has no inbox surface for the
        kind. The second arm preserves base behaviour for the map's other
        inbox=False kinds (NET_INTERRUPTED / TRANSIENT_OVERLOAD): F790 must only
        STOP enqueuing the drain class, never START enqueuing a kind the map
        already declined (gate r1 B1). Keyed on ``(kind, subtype)`` so the
        command_exit PROC_EXITED and low_context_tip cases are matched exactly.

        F807 (#664): this is consulted REGARDLESS of whether a durable log store
        is wired. F790's decline was previously gated behind a wired
        ``_log_store`` (early ``return False``), which left it dormant in the
        production construction that omitted the store — so BUSY-class and
        low_context_tip rows were still enqueued to the seat. The decline is a
        pure routing decision over ``(kind, subtype)`` with no dependency on the
        durable ledger, so it now falls through to the predicate unconditionally;
        F611's three-surface fan-out for the NON-declined class is unchanged."""
        from cli_agent_orchestrator.clients.delivery_ledger import (
            drain_class_declines_inbox,
            surfaces_for_kind,
        )

        return drain_class_declines_inbox(kind, subtype) or not surfaces_for_kind(kind).inbox

    @staticmethod
    def _surfaces_str(kind: str, subtype: str = "") -> str:
        from cli_agent_orchestrator.clients.delivery_ledger import (
            drain_class_declines_inbox,
            surfaces_for_kind,
        )

        surf = surfaces_for_kind(kind)
        # F807 (#664): the recorded surfaces string reflects the ACTUAL routing,
        # so a subtype-level decline (low_context_tip) records "fleet,bus" even
        # though the kind-keyed map still carries inbox=True for hard exhaustion.
        inbox = surf.inbox and not drain_class_declines_inbox(kind, subtype)
        parts = []
        if surf.fleet:
            parts.append("fleet")
        if surf.bus:
            parts.append("bus")
        if inbox:
            parts.append("inbox")
        return ",".join(parts)

    def _record(
        self,
        terminal_id: str,
        decision: str,
        kind: Optional[str],
        subtype: Optional[str],
        epoch: Optional[int],
        *,
        surfaces: Optional[str] = None,
        suppressed_reason: Optional[str] = None,
        inbox_message_id: Optional[int] = None,
    ) -> None:
        if self._log_store is None:
            return
        try:
            self._log_store.record(
                terminal_id=terminal_id,
                decision=decision,
                kind=kind,
                subtype=subtype,
                epoch=epoch,
                surfaces=surfaces,
                suppressed_reason=suppressed_reason,
                inbox_message_id=inbox_message_id,
            )
        except Exception:  # audit write must never break the transition
            pass

    def _set_fleet(self, terminal_id: str, label: Optional[str]) -> None:
        if self._fleet_sink is None:
            return
        try:
            self._fleet_sink(terminal_id, label)
        except Exception:  # a sink failure must not break the transition
            pass

    def _push_inbox(self, terminal_id: str, cond: Condition) -> int:
        if self._inbox_sink is None:
            return 1  # modeled push when no real sink (test/degraded parity)
        try:
            self._inbox_sink(terminal_id, cond)
            return 1
        except Exception:
            return 0

    def _project_cli(self, terminal_id: str, cond: Condition, label: str) -> None:
        if self._cli_sink is None:
            return
        try:
            self._cli_sink(terminal_id, cond, label)
        except Exception:
            pass

    @staticmethod
    def _fleet_label(cond: Condition) -> str:
        """The fleet-row label rendered instead of ``unknown`` (§3 surface 1)."""
        if cond.kind is ConditionKind.CAPPED:
            return "CAPPED"
        if cond.kind in (ConditionKind.DIALOG_BLOCKED,):
            return "BLOCKED"
        if cond.kind is ConditionKind.AUTH_EXPIRED:
            return "AUTH"
        return str(cond.kind.value)


# ─── §4 / D6 / D7 / D8: policy layer ───────────────────────────────────────────
class PolicyAction(str, Enum):
    """The advisory action the policy layer derives from a typed condition (§4).

    F611 adds NO routing-table refusal code (D7): CAPPED is a RUNTIME condition
    consumed by F574's chain walk, not a ``load_routing_table`` validation error.
    """

    FALLBACK_KIRO = "fallback_kiro"  # laptop-plane CAPPED → kiro for that position
    STOP_AND_ASK = "stop_and_ask"  # kiro also capped, OR auth/dialog gate (D8)
    ADVISORY_ONLY = "advisory_only"  # box-plane CAPPED (D6) — never rebinds
    NONE = "none"  # BUSY / context / net / transient — no policy action


def policy_for_condition(
    cond: Condition, *, position: str = "dev", kiro_capped: bool = False
) -> PolicyAction:
    """Map a typed condition to an advisory policy action (§4, D6/D7/D8).

    * A box-scoped CAPPED (``scope == "credential_plane"``, D6) is ADVISORY only —
      it never rebinds a laptop position; the supervisor cross-checks the laptop
      plane first (M36).
    * A laptop-plane CAPPED falls back to kiro for that position; if kiro is ALSO
      capped, STOP and ask (CLAUDE.md:235-243). No self-elected substitute (M34).
    * AUTH_EXPIRED and a ``wait``-class DIALOG_BLOCKED are standing human gates:
      STOP and ask, never auto-recover or rebind (D8).
    * Everything else (BUSY / CONTEXT / NET / TRANSIENT) carries no policy action.
    """
    if cond.kind is ConditionKind.CAPPED:
        if cond.scope == "credential_plane":
            return PolicyAction.ADVISORY_ONLY
        return PolicyAction.STOP_AND_ASK if kiro_capped else PolicyAction.FALLBACK_KIRO
    if cond.kind in (ConditionKind.AUTH_EXPIRED, ConditionKind.DIALOG_BLOCKED):
        return PolicyAction.STOP_AND_ASK
    return PolicyAction.NONE
