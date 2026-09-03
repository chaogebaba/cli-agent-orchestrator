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
from typing import Callable, Dict, List, Optional, Tuple

# ─── Reused in-tree provider anchors (never re-implemented, blueprint §2.1) ────
# Imported lazily-safe at module import: these are module-level constants in the
# provider modules and carry no import cycle back to this module.
from cli_agent_orchestrator.providers.codex import (  # noqa: E402
    SYSTEM_NOTICE_PATTERN,
    TRANSIENT_API_ERROR_PATTERNS,
    TRANSIENT_ERROR_EXCLUSIONS,
    USER_PREFIX_PATTERN,
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
        """Render the ONE typed event line (blueprint §3 event shape)."""
        return (
            f"[CONDITION] terminal={terminal_id} kind={self.kind.value} "
            f"provider={self.provider} subtype={self.subtype} "
            f'evidence="{self.evidence}" '
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

# codex context footer (codex-context-exhausted-1). Reuse the codex footer shape:
# "Context NN% left". The threshold guard (blueprint AC/hazard): must NOT fire on
# a HEALTHY footer like "Context 77% left" — that is BUSY, not exhausted. Only a
# footer at/below the CONTEXT_EXHAUSTED_THRESHOLD is a hard stop.
CONTEXT_EXHAUSTED_THRESHOLD = 15
_CONTEXT_FOOTER = re.compile(r"Context\s+(\d+)%\s+left", re.IGNORECASE)
_KIRO_CONTEXT_TIP = re.compile(r"Running low on context\? Type /compact", re.IGNORECASE)

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
_GROK_BUSY = re.compile(r"Waiting for response", re.IGNORECASE)
_CLINE_BUSY = re.compile(r"\[thinking\]|\[run_commands\]", re.IGNORECASE)

# PROC_EXITED (cline text anchor; the process-state path is handled separately by
# the provider via pane_current_command == shell_baseline — see D5/precedence 1).
_CLINE_PROC_EXITED = re.compile(r"\[Command exited with code \d+\]")


def _first_evidence(rows: List[str], pattern: "re.Pattern[str]") -> Optional[str]:
    for row in rows:
        if pattern.search(row):
            return row.strip()
    return None


def _classify_capped(provider: str, brows: List[str]) -> Optional[Condition]:
    """CAPPED for the provider, banner-only (D2). Reset≠cap guard (§2.4)."""
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


def _classify_context(provider: str, brows: List[str]) -> Optional[Condition]:
    if provider == "codex":
        for row in brows:
            m = _CONTEXT_FOOTER.search(row)
            if m and int(m.group(1)) <= CONTEXT_EXHAUSTED_THRESHOLD:
                return Condition(
                    ConditionKind.CONTEXT_EXHAUSTED,
                    provider,
                    "footer_percent_status",
                    row.strip(),
                    Confidence.HIGH,
                )
        return None
    if provider == "kiro_cli":
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


# The per-kind classifiers, applied then ranked by §2.2 precedence.
_KIND_CLASSIFIERS: Tuple[Callable[[str, List[str]], Optional[Condition]], ...] = (
    _classify_capped,
    _classify_auth,
    _classify_net,
    _classify_context,
    _classify_dialog,
    _classify_transient,
    _classify_busy,
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
    text_exit = _first_evidence(brows, _CLINE_PROC_EXITED) if provider == "cline_cli" else None
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

    for classifier in _KIND_CLASSIFIERS:
        cond = classifier(provider, brows)
        if cond is not None:
            candidates.append(cond)

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
class ConditionLogStore:
    """Protocol for the durable decision log (F642 §2, D7). A production impl
    wraps ``clients.database.suppress_condition_by_log`` /
    ``record_condition_decision``; a test impl can be an in-memory list."""

    def should_suppress(self, terminal_id: str, kind: str, subtype: str, epoch: int) -> bool:
        """D7: read the latest memory-updating row and decide suppression."""
        raise NotImplementedError

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
        raise NotImplementedError


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
            self._record(
                terminal_id, "gated", cond.kind.value, cond.subtype, epoch
            )
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
        # F642 D5: the kind→surfaces map gates the INBOX leg. A BUSY-class kind
        # fires fleet+bus but declines the inbox push; the memory is STILL set
        # (this stays a `delivered` decision), so the row stays inside the de-dup
        # comparison (r3/B1) — recorded via suppressed_reason='busy_class'.
        inbox_declined = self._inbox_declined(cond.kind.value)
        pushes = 0 if inbox_declined else self._push_inbox(terminal_id, cond)
        self._project_cli(terminal_id, cond, label)
        self._record(
            terminal_id,
            "delivered",
            cond.kind.value,
            cond.subtype,
            epoch,
            surfaces=self._surfaces_str(cond.kind.value),
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

    def _inbox_declined(self, kind: str) -> bool:
        """D5: does the kind→surfaces map decline the inbox leg? Only consulted
        when a log store is wired (spine active); otherwise F611's fan-out is
        unchanged."""
        if self._log_store is None:
            return False
        from cli_agent_orchestrator.clients.delivery_ledger import busy_class_declines_inbox

        return busy_class_declines_inbox(kind)

    @staticmethod
    def _surfaces_str(kind: str) -> str:
        from cli_agent_orchestrator.clients.delivery_ledger import surfaces_for_kind

        surf = surfaces_for_kind(kind)
        parts = []
        if surf.fleet:
            parts.append("fleet")
        if surf.bus:
            parts.append("bus")
        if surf.inbox:
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
