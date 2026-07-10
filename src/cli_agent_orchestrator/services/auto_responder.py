"""Auto-answers blocking TUI dialogs (whitelist-only) from the composited screen.

Consumer: status_monitor's screen-detection path (``_detect_screen``), called
once per detection tick (rising edge + quiescence) for providers that opt in
via ``supports_screen_detection``. See blueprints/auto-responder.md in the
outer cli-subagents repo for the full design.

Scope is intentionally narrow: only dialogs matching a rule the supervisor
(or a human) has authored in ``~/.aws/cli-agent-orchestrator/auto-answers/
<provider>.yaml`` are ever auto-answered. An unmatched screen is suspect only
when dialog markers are close together and the provider parser reports a
non-ready status. This deliberately errs toward silence for novel dialogs that
parse as IDLE or COMPLETED; the stalled-callback watchdog remains the fallback.
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
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

AUTO_ANSWER_DIR = CAO_HOME_DIR / "auto-answers"
AUTO_ANSWER_LOG_DIR = CAO_HOME_DIR / "logs" / "auto-answers"

RETRY_MAX = 3
RETRY_DELAY_S = 1.0
COOLDOWN_S = 5.0
KEY_DELAY_S = 0.1
UNKNOWN_DIALOG_PUSH_FLOOR_S = 300.0
UNKNOWN_DIALOG_PAYLOAD_CHARS = 600
DIALOG_PROXIMITY_CHARS = 200

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

    def on_screen(
        self, terminal_id: str, provider: Any, lines: List[str]
    ) -> Optional[TerminalStatus]:
        """Inspect the composited screen; return a status override or None.

        None means "no opinion" -- the caller should fall through to normal
        provider detection. Never raises.
        """
        if os.environ.get("CAO_AUTO_ANSWER", "true").lower() == "false":
            return None
        if not getattr(provider, "supports_screen_detection", False):
            return None
        try:
            return self._on_screen(terminal_id, provider, lines)
        except Exception:
            logger.exception("auto-responder: error handling terminal %s", terminal_id)
            return None

    def _on_screen(
        self, terminal_id: str, provider: Any, lines: List[str]
    ) -> Optional[TerminalStatus]:
        from cli_agent_orchestrator.clients.database import get_terminal_metadata
        from cli_agent_orchestrator.services.session_env import get_session_env

        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            return None

        # Per-terminal opt-out via ``cao launch --env CAO_AUTO_ANSWER=false``.
        # Plumbing is session-scoped (env_vars are persisted per tmux session,
        # not per terminal -- see services/session_env.py), which is an exact
        # match for the common case of one worker per session but degrades to
        # session-wide for multi-window sessions.
        session_env = get_session_env(metadata["tmux_session"])
        if session_env.get("CAO_AUTO_ANSWER", "true").lower() == "false":
            return None

        if self._find_supervisor(metadata["tmux_session"]) == terminal_id:
            logger.debug("auto-responder: skipping supervisor terminal %s", terminal_id)
            return None

        provider_name = metadata["provider"]
        normalized = normalize_screen(lines)
        if not normalized:
            return None

        for rule in _store.get_rules(provider_name):
            if not rule.matches(normalized):
                continue
            if rule.is_wait:
                return TerminalStatus.WAITING_USER_ANSWER
            state = self._state_for(terminal_id, rule.name)
            if time.monotonic() < state.cooldown_until:
                return None  # redraw double-fire guard
            self._fire(terminal_id, metadata, rule, normalized, state)
            return None

        return self._check_unknown(
            terminal_id, metadata, provider_name, provider, lines, normalized
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
        rule: Rule,
        normalized: str,
        state: _RuleState,
    ) -> None:
        self._send_answer(metadata, rule)
        self._log(terminal_id, rule, "fired", normalized)
        state.cooldown_until = time.monotonic() + COOLDOWN_S
        threading.Thread(
            target=self._verify_and_retry,
            args=(terminal_id, metadata, rule, state),
            daemon=True,
        ).start()

    def _verify_and_retry(
        self, terminal_id: str, metadata: Dict[str, Any], rule: Rule, state: _RuleState
    ) -> None:
        """Runs off the event-loop thread: 1s-later recheck, retry <=3 total fires."""
        for attempt in range(2, RETRY_MAX + 1):
            time.sleep(RETRY_DELAY_S)
            normalized = self._current_normalized(terminal_id)
            if normalized is None or not rule.matches(normalized):
                return
            self._send_answer(metadata, rule)
            self._log(terminal_id, rule, f"retry-{attempt}", normalized)
            state.cooldown_until = time.monotonic() + COOLDOWN_S

        time.sleep(RETRY_DELAY_S)
        normalized = self._current_normalized(terminal_id)
        if normalized is not None and rule.matches(normalized):
            self._surface_retry_exhausted(terminal_id, metadata, rule)

    @staticmethod
    def _current_normalized(terminal_id: str) -> Optional[str]:
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        lines = status_monitor.get_rendered_screen(terminal_id)
        if lines is None:
            return None
        return normalize_screen(lines)

    @staticmethod
    def _send_answer(metadata: Dict[str, Any], rule: Rule) -> None:
        from cli_agent_orchestrator.backends.registry import get_backend

        backend = get_backend()
        for i, key in enumerate(rule.answer):
            if i > 0:
                time.sleep(KEY_DELAY_S)
            backend.send_special_key(metadata["tmux_session"], metadata["tmux_window"], key)

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
        self, terminal_id: str, metadata: Dict[str, Any], rule: Rule
    ) -> None:
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        status_monitor.force_status(terminal_id, TerminalStatus.WAITING_USER_ANSWER)
        self._push(
            terminal_id,
            metadata,
            f"[auto-responder] rule '{rule.name}' fired {RETRY_MAX}x on terminal "
            f"{terminal_id} but the dialog persists. Manual attention needed.",
        )

    # ----- unknown-dialog heuristic ---------------------------------------

    def _check_unknown(
        self,
        terminal_id: str,
        metadata: Dict[str, Any],
        provider_name: str,
        provider: Any,
        lines: List[str],
        normalized: str,
    ) -> Optional[TerminalStatus]:
        is_suspect = self._looks_like_dialog(normalized, provider_name)
        if is_suspect:
            try:
                status = provider.get_status_from_screen(lines)
                is_suspect = status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)
            except Exception:
                logger.debug(
                    "auto-responder: provider status parse failed for %s",
                    terminal_id,
                    exc_info=True,
                )

        if not is_suspect:
            close_episode = False
            with self._lock:
                state = self._unknown_state.get(terminal_id)
                if state and state.episode_open:
                    state.non_dialog_ticks += 1
                    if state.non_dialog_ticks >= 2:
                        state.episode_open = False
                        state.non_dialog_ticks = 0
                        close_episode = True
            if state and state.episode_open and not close_episode:
                return TerminalStatus.WAITING_USER_ANSWER
            return None

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
            dialog_text = self._payload_excerpt(normalized)
            self._push(
                terminal_id,
                metadata,
                "[auto-responder] unknown blocking dialog on terminal "
                f"{terminal_id} (provider={provider_name}); no rule matched, the "
                "worker is stalled. Ask the user how to answer it (auto-answer "
                "default / other keys / always wait), then append a rule to "
                f"~/.aws/cli-agent-orchestrator/auto-answers/{provider_name}.yaml.\n\n"
                f"Dialog text (normalized): {dialog_text}",
            )
        return TerminalStatus.WAITING_USER_ANSWER

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
        from cli_agent_orchestrator.clients.database import list_terminals_by_session

        for terminal in list_terminals_by_session(session_name):
            if terminal["provider"] == "claude_code":
                return terminal["id"]
        return None

    def _push(self, terminal_id: str, metadata: Dict[str, Any], message: str) -> None:
        from cli_agent_orchestrator.clients.database import create_inbox_message
        from cli_agent_orchestrator.services.inbox_service import inbox_service

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
        try:
            create_inbox_message(terminal_id, supervisor_id, message)
            inbox_service.deliver_pending(supervisor_id, registry=None)
        except Exception:
            logger.exception("auto-responder: failed to push to supervisor %s", supervisor_id)


# Module-level singleton
auto_responder = AutoResponder()
