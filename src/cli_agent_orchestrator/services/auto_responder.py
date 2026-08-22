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
DIALOG_REGION_LINES = 15

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
                return None

        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            self._clear_wait_rule(terminal_id)
            return None

        # Per-terminal opt-out via ``cao launch --env CAO_AUTO_ANSWER=false``.
        # Plumbing is session-scoped (env_vars are persisted per tmux session,
        # not per terminal -- see services/session_env.py), which is an exact
        # match for the common case of one worker per session but degrades to
        # session-wide for multi-window sessions.
        session_env = get_session_env(metadata["tmux_session"])
        if session_env.get("CAO_AUTO_ANSWER", "true").lower() == "false":
            self._clear_wait_rule(terminal_id)
            return None

        if self._find_supervisor(metadata["tmux_session"]) == terminal_id:
            self._clear_wait_rule(terminal_id)
            logger.debug("auto-responder: skipping supervisor terminal %s", terminal_id)
            return None

        provider_name = metadata["provider"]
        region = dialog_region(lines)
        if not region.normalized:
            self._clear_wait_rule(terminal_id)
            return None
        supplied_status = self._classify_region(terminal_id, provider, region)
        incarnation = self._snapshot_incarnation(terminal_id, metadata)

        for rule in _store.get_rules(provider_name):
            if not rule.matches(region.normalized):
                continue
            if rule.is_wait:
                fresh = self._capture_for_analysis(metadata, lines, terminal_id, provider)
                if fresh is None:
                    return None
                fresh_region = self._region_from_capture(fresh)
                if not rule.matches(fresh_region.normalized):
                    return None
                with self._lock:
                    self._wait_rule_active[terminal_id] = (rule.name, time.monotonic())
                return TerminalStatus.WAITING_USER_ANSWER
            if self._busy_veto(supplied_status):
                continue
            if not self._corroborates_fire(region, supplied_status):
                continue
            self._clear_wait_rule(terminal_id)
            state = self._state_for(terminal_id, rule.name)
            if time.monotonic() < state.cooldown_until:
                return None  # redraw double-fire guard
            self._fire(
                terminal_id,
                metadata,
                provider,
                rule,
                region.normalized,
                state,
                incarnation,
            )
            return None

        self._clear_wait_rule(terminal_id)
        return self._check_unknown(
            terminal_id,
            metadata,
            provider_name,
            provider,
            lines,
            region,
            supplied_status,
            incarnation,
        )

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
        normalized: str,
        state: _RuleState,
        incarnation: TerminalIncarnation,
    ) -> bool:
        if not self._effect_barrier(terminal_id, metadata, provider, rule):
            return False
        if not self._send_answer(terminal_id, metadata, rule, incarnation):
            return False
        self._log(terminal_id, rule, "fired", normalized)
        state.cooldown_until = time.monotonic() + COOLDOWN_S
        threading.Thread(
            target=self._verify_and_retry,
            args=(terminal_id, metadata, provider, rule, state, incarnation),
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
    ) -> None:
        """Runs off the event-loop thread: 1s-later recheck, retry <=3 total fires."""
        for attempt in range(2, RETRY_MAX + 1):
            time.sleep(RETRY_DELAY_S)
            if not self._incarnation_is_current(terminal_id, incarnation):
                return
            region = self._current_normalized(terminal_id)
            if region is None or not rule.matches(region.normalized):
                return
            if not self._effect_barrier(terminal_id, metadata, provider, rule):
                return
            if not self._send_answer(terminal_id, metadata, rule, incarnation):
                return
            self._log(terminal_id, rule, f"retry-{attempt}", region.normalized)
            state.cooldown_until = time.monotonic() + COOLDOWN_S

        time.sleep(RETRY_DELAY_S)
        if not self._incarnation_is_current(terminal_id, incarnation):
            return
        region = self._current_normalized(terminal_id)
        if region is not None and rule.matches(region.normalized):
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
        if not self._effect_barrier(terminal_id, metadata, provider, rule):
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
    ) -> bool:
        if provider is None:
            return False
        fresh = self._capture_for_analysis(metadata, [], terminal_id, provider)
        if fresh is None:
            return False
        region = self._region_from_capture(fresh)
        status = self._classify_region(terminal_id, provider, region)
        return (
            rule.matches(region.normalized)
            and not self._busy_veto(status)
            and self._corroborates_fire(region, status)
        )

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
