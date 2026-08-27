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
newlines are not. All matching therefore runs against the composited screen
with every run of whitespace (including newlines) collapsed to a single
space -- never against raw lines. Rules must never encode newlines.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
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
- name: codex-usage-resets
  enabled: true
  match_mode: regex
  question: 'You have \\d+ usage limit resets available'
  options: ["Yes, continue", "No, quit"]   # all must appear (normalized)
  answer: ["Enter"]                         # tmux special-key names, 0.1s apart
- name: codex-trust-dir
  enabled: true
  match_mode: contains
  question: "Do you trust the contents of this directory?"
  options: ["Yes, continue", "No, quit"]
  answer: ["Enter"]
- name: codex-resume-working-directory
  enabled: true
  match_mode: contains
  question: "Choose working directory to resume this session"
  options: ["Press enter"]
  answer: ["Enter"]
""",
}

# Generic unknown-dialog heuristic (any provider): numbered options like
# "1. Yes, continue" plus a "press enter to continue"-style footer.
_NUMBERED_OPTION_PATTERN = re.compile(r"\b[1-3]\.\s+\S")
_PRESS_ENTER_PATTERN = re.compile(r"press enter", re.IGNORECASE)


def normalize_screen(lines: List[str]) -> str:
    """Flatten composited screen lines into whitespace-normalized text.

    Every run of whitespace/newlines collapses to a single space -- this is
    the line-break trap invariant. Never match against raw ``lines``.
    """
    text = " ".join(lines)
    return re.sub(r"\s+", " ", text).strip()


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

    def with_digests(self, settle: str, consume: str) -> "DialogRegion":
        """Return a copy carrying the two pending-fire digests."""
        return DialogRegion(
            rows=self.rows,
            normalized=self.normalized,
            settle_digest=settle,
            consume_digest=consume,
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


def dialog_region(screen: List[str]) -> DialogRegion:
    """Return the rendered dialog-bearing tail without normalizing provider input."""
    end = len(screen)
    while end and not screen[end - 1].strip():
        end -= 1
    rows = tuple(screen[max(0, end - DIALOG_REGION_LINES) : end])
    return DialogRegion(rows=rows, normalized=normalize_screen(list(rows)))


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


@dataclass
class Rule:
    name: str
    enabled: bool
    match_mode: str
    question: str
    options: List[str]
    answer: Any  # list[str] of tmux special-key names, or the literal "wait"

    @property
    def is_wait(self) -> bool:
        return self.answer == "wait"

    def matches(self, normalized: str) -> bool:
        if not self.enabled:
            return False
        if self.match_mode == "regex":
            if not re.search(self.question, normalized):
                return False
        else:
            if self.question not in normalized:
                return False
        return all(opt in normalized for opt in self.options)


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

    def match_verdict(
        self, provider_name: str, lines: List[str]
    ) -> "RuleMatchVerdict | None":
        """F516 D3p: whitelist text-match verdict for a consult caller.

        Handed pre-captured ``lines`` (this never captures itself). Computes the
        dialog region and the normalized whitelist match against the provider's
        rules and returns a metadata-free ``RuleMatchVerdict`` for the first
        matching non-wait rule, or ``None`` when nothing matches. Provider dialog
        classification is a SEPARATE consult (consult (a)) — this never calls
        ``_classify_region``. Wait-rules (human-gated) are treated as a match for
        deferral purposes: a wait dialog on screen must also defer a paste.

        The D6 still-moving banner-mark suppression (returning ``None`` for a
        scrolling banner) is wired in commit 6 once the per-terminal pre-filter
        verdict cache exists; between commit 2 and commit 6 every match on the
        consult path defers (ACCEPTED INTERIM, blueprint r8-S2).
        """
        region = dialog_region(lines)
        if not region.normalized:
            return None
        for rule in _store.get_rules(provider_name):
            if not rule.matches(region.normalized):
                continue
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
            for key in [key for key in self._rule_state if key[0] == terminal_id]:
                self._rule_state.pop(key, None)

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
        region = dialog_region(lines)
        if not region.normalized:
            self._clear_wait_rule(terminal_id)
            self._log_decision(terminal_id, "not_running", "empty_region")
            return None
        supplied_status = self._classify_region(terminal_id, provider, region)
        incarnation = self._snapshot_incarnation(terminal_id, metadata)

        for rule in _store.get_rules(provider_name):
            if not rule.matches(region.normalized):
                continue
            if rule.is_wait:
                fresh = self._capture_for_analysis(metadata, lines, terminal_id, provider)
                if fresh is None:
                    self._log_decision(terminal_id, "no_match", "wait_rule_capture_failed", rule.name)
                    return None
                fresh_region = self._region_from_capture(fresh)
                if not rule.matches(fresh_region.normalized):
                    self._log_decision(terminal_id, "no_match", "wait_rule_fresh_mismatch", rule.name)
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
                # retry (r3-B4; it counts toward D6's veto streak in c6).
                self._request_detection_retry(terminal_id)
                continue
            # D2: classifier status is a fast-path corroborator only, NEVER a
            # veto. The ONE retained exception is _busy_veto (PROCESSING),
            # unchanged — the proven M1/F55 quoted-prose suppressor. The old
            # _corroborates_fire veto (WAITING or dialog-proximity) is gone from
            # the fire path; _has_dialog_proximity is retained but no longer
            # consulted here (r5-S3). Region discipline + settle now gate the
            # fire, both inside _effect_barrier. The D6 no-history HOLD /
            # scroll-exclusion is added in commit 6; until then an uncorroborated
            # match traverses the barrier and fires (AC7 test_m3 is expected RED
            # for commits 3-5, green from commit 6 via D6's no-history HOLD).
            self._clear_wait_rule(terminal_id)
            state = self._state_for(terminal_id, rule.name)
            if time.monotonic() < state.cooldown_until:
                self._log_decision(terminal_id, "no_match", "cooldown_active", rule.name)
                return None  # redraw double-fire guard
            self._log_decision(terminal_id, "matched", "firing", rule.name)
            # D2(i)/D5: seed the pending-fire record's two digests, both over the
            # normalized-domain rule-loop region (r5-B1).
            match_digest = _digest_normalized(region.normalized)
            pending_region = region.with_digests(settle=match_digest, consume=match_digest)
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
        self._log_decision(terminal_id, "no_match", "no_rule_matched")
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

    def _state_for(self, terminal_id: str, rule_name: str) -> _RuleState:
        with self._lock:
            key = (terminal_id, rule_name)
            state = self._rule_state.get(key)
            if state is None:
                state = _RuleState()
                self._rule_state[key] = state
            return state

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
        for attempt in range(2, RETRY_MAX + 1):
            time.sleep(RETRY_DELAY_S)
            if not self._incarnation_is_current(terminal_id, incarnation):
                return
            attempt_region = self._current_normalized(terminal_id)
            if attempt_region is None or not rule.matches(attempt_region.normalized):
                return
            attempt_region = attempt_region.with_digests(
                settle=_digest_normalized(attempt_region.normalized),
                consume=region.consume_digest,
            )
            if not self._effect_barrier(
                terminal_id, metadata, provider, rule, attempt_region
            ):
                return
            if not self._send_answer(terminal_id, metadata, rule, incarnation):
                return
            self._log(terminal_id, rule, f"retry-{attempt}", attempt_region.normalized)
            state.cooldown_until = time.monotonic() + COOLDOWN_S

        time.sleep(RETRY_DELAY_S)
        if not self._incarnation_is_current(terminal_id, incarnation):
            return
        final_region = self._current_normalized(terminal_id)
        if final_region is not None and rule.matches(final_region.normalized):
            self._surface_retry_exhausted(terminal_id, metadata, rule, provider, incarnation)

    @staticmethod
    def _current_normalized(terminal_id: str) -> Optional[DialogRegion]:
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        lines = status_monitor.get_rendered_screen(terminal_id)
        if lines is None:
            return None
        return dialog_region(lines)

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
        region = self._region_from_capture(fresh)
        status = self._classify_region(terminal_id, provider, region)
        if not rule.matches(region.normalized):
            return False  # dialog cleared / no longer matches — no retry
        if self._busy_veto(status):
            self._request_detection_retry(terminal_id)
            return False
        # D2(i) settle (r2-B3/r5-B1): normalized-domain digest match; empty
        # settle_digest → skipped (retry-exhausted surface path).
        settle_ok = (not pending_region.settle_digest) or (
            _digest_normalized(region.normalized) == pending_region.settle_digest
        )
        if not settle_ok:
            self._request_detection_retry(terminal_id)  # mid-repaint tearing
            return False
        return True

    @staticmethod
    def _region_from_capture(
        capture: tuple[str, List[str]] | AutoResponderDecision,
    ) -> DialogRegion:
        if isinstance(capture, AutoResponderDecision):
            return dialog_region(list(capture.lines))
        return dialog_region(capture[1])

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
            from cli_agent_orchestrator.providers.codex import WAITING_PROMPT_PATTERN

            if re.search(WAITING_PROMPT_PATTERN, normalized):
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
