"""Preserve human TUI composer drafts before CAO injects input."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import DRAFT_LOG_DIR, PYTE_SCREEN_ROWS
from cli_agent_orchestrator.services.status_monitor import status_monitor

logger = logging.getLogger(__name__)

DRAFT_STABILITY_INITIAL_DELAY_SECONDS = 0.3
DRAFT_STABILITY_RECHECK_SECONDS = 0.5
DRAFT_STABILITY_TIMEOUT_SECONDS = 30.0
DRAFT_CLEAR_MAX_ITERATIONS = 50
DRAFT_CLEAR_RECHECK_DELAY_SECONDS = 0.05


@dataclass
class PreservedDraft:
    terminal_id: str
    session_name: str
    window_name: str
    text: str
    submit_delay: float

    def restore(self, backend: Any | None = None) -> None:
        """Paste the stashed draft back into the composer without submitting."""
        if not self.text:
            return
        backend = backend or get_backend()
        try:
            backend.send_keys(
                self.session_name,
                self.window_name,
                self.text,
                enter_count=0,
                force_bracketed_paste=True,
                submit_delay=self.submit_delay,
            )
        except Exception as e:
            logger.warning(
                "Failed to restore composer draft for terminal %s: %s",
                self.terminal_id,
                e,
            )


def preserve_draft_before_send(
    terminal_id: str,
    metadata: dict[str, Any],
    provider: Any | None,
) -> Optional[PreservedDraft]:
    """Stash and clear an unsent composer draft before message injection.

    Providers opt in explicitly via ``supports_draft_preservation is True``.
    Returns a PreservedDraft when a non-empty draft was found and should be
    restored after the caller injects its message.
    """
    if getattr(provider, "supports_draft_preservation", False) is not True:
        return None

    draft = _read_provider_draft(terminal_id, metadata, provider)
    if not draft:
        return None

    draft = _wait_for_stable_draft(terminal_id, metadata, provider, draft)
    if not draft:
        return None

    _append_draft_log(terminal_id, draft)
    _clear_composer(terminal_id, metadata, provider)
    return PreservedDraft(
        terminal_id=terminal_id,
        session_name=metadata["tmux_session"],
        window_name=metadata["tmux_window"],
        text=draft,
        submit_delay=getattr(provider, "paste_submit_delay", 0.3),
    )


def _wait_for_stable_draft(
    terminal_id: str,
    metadata: dict[str, Any],
    provider: Any,
    first_draft: str,
) -> str:
    time.sleep(DRAFT_STABILITY_INITIAL_DELAY_SECONDS)
    latest = _read_provider_draft(terminal_id, metadata, provider)
    if latest is None:
        return first_draft
    if latest == first_draft:
        return latest

    deadline = time.monotonic() + DRAFT_STABILITY_TIMEOUT_SECONDS
    previous = latest
    while time.monotonic() < deadline:
        time.sleep(DRAFT_STABILITY_RECHECK_SECONDS)
        latest = _read_provider_draft(terminal_id, metadata, provider)
        if latest is None:
            return previous
        if latest == previous:
            return latest
        previous = latest
    logger.warning("Composer draft for terminal %s did not stabilize before delivery", terminal_id)
    return previous


def _clear_composer(terminal_id: str, metadata: dict[str, Any], provider: Any) -> None:
    backend = get_backend()
    clear_keys = list(getattr(provider, "composer_clear_keys", []) or [])
    if not clear_keys:
        logger.warning("Draft preservation enabled for %s but no clear keys configured", terminal_id)
        return

    for _ in range(DRAFT_CLEAR_MAX_ITERATIONS):
        current = _read_provider_draft(terminal_id, metadata, provider)
        if current == "":
            return
        for key in clear_keys:
            backend.send_special_key(metadata["tmux_session"], metadata["tmux_window"], key)
        time.sleep(DRAFT_CLEAR_RECHECK_DELAY_SECONDS)

    logger.warning(
        "Composer draft for terminal %s remained after %d clear iterations",
        terminal_id,
        DRAFT_CLEAR_MAX_ITERATIONS,
    )


def _read_provider_draft(
    terminal_id: str,
    metadata: dict[str, Any],
    provider: Any,
) -> Optional[str]:
    screen = _read_screen_lines(terminal_id, metadata)
    if screen is None:
        return None
    try:
        return provider.read_composer_draft(screen)
    except Exception:
        logger.exception("Failed to parse composer draft for terminal %s", terminal_id)
        return None


def _read_screen_lines(terminal_id: str, metadata: dict[str, Any]) -> Optional[list[str]]:
    screen = status_monitor.get_rendered_screen(terminal_id)
    if screen is not None:
        return screen

    try:
        captured = get_backend().get_history(
            metadata["tmux_session"],
            metadata["tmux_window"],
            tail_lines=PYTE_SCREEN_ROWS,
            strip_escapes=True,
        )
    except Exception as e:
        logger.warning("Failed to capture screen for draft preservation on %s: %s", terminal_id, e)
        return None
    return captured.splitlines()


def _append_draft_log(terminal_id: str, draft: str) -> None:
    DRAFT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    path = DRAFT_LOG_DIR / f"{terminal_id}.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(f"--- {timestamp} terminal_id={terminal_id} ---\n")
        f.write(draft)
        if not draft.endswith("\n"):
            f.write("\n")
