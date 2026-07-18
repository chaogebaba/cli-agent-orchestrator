"""Preserve human TUI composer drafts before CAO injects input."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import DRAFT_LOG_DIR, PYTE_SCREEN_ROWS
from cli_agent_orchestrator.providers.claude_code import CLAUDE_DIALOG_PATTERN
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

DRAFT_STABILITY_INITIAL_DELAY_SECONDS = 0.3
DRAFT_STABILITY_RECHECK_SECONDS = 0.5
DRAFT_STABILITY_TIMEOUT_SECONDS = 30.0
DRAFT_CLEAR_MAX_ITERATIONS = 50
DRAFT_CLEAR_RECHECK_DELAY_SECONDS = 0.05
DRAFT_CLEAR_PROBE_RECHECK_DELAY_SECONDS = 0.3
# Transient None re-read after clear-keys: retry before conservative "changed".
DRAFT_CLEAR_PROBE_NONE_RETRIES = 2
DRAFT_CLEAR_PROBE_NONE_RETRY_DELAY_SECONDS = 0.15
STASH_SNAPSHOT_RETRIES = 3


class DeliveryDeferredError(Exception):
    """Raised when transient terminal state makes delivery unsafe to attempt."""


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


@dataclass(frozen=True)
class ComposerSnapshot:
    chip_present: bool
    draft: str | None


@dataclass(frozen=True)
class PreparedNativeStash:
    """Final pre-mutation authorization for a native-stash provider send."""

    chip_present: bool = False


def prepare_native_stash_before_send(
    terminal_id: str,
    provider: Any,
    *,
    defer_on_dialog: bool = False,
) -> PreparedNativeStash:
    """Authorize only a stable empty native composer without emitting keys."""
    authority_reader = getattr(provider, "read_composer_draft_authority", None)
    state_reader = getattr(provider, "read_composer_draft_state", None)
    if callable(authority_reader) and callable(
        getattr(type(provider), "read_composer_draft_authority", None)
    ):
        try:
            state, chip_present = authority_reader(defer_on_dialog=defer_on_dialog)
        except Exception:
            state, chip_present = "unresolved", False
    elif callable(state_reader):
        try:
            state = state_reader(defer_on_dialog=defer_on_dialog)
        except Exception:
            state = "unresolved"
        chip_present = False
    else:
        raise DeliveryDeferredError(
            f"Composer state is unreadable for terminal {terminal_id}"
        )
    if state == "dialog":
        raise DeliveryDeferredError("Claude dialog is active")
    if state != "empty":
        raise DeliveryDeferredError(
            f"Composer state is {state or 'unresolved'} for terminal {terminal_id}"
        )
    return PreparedNativeStash(chip_present=chip_present)


def apply_prepared_native_stash(prepared: PreparedNativeStash) -> bool:
    """Apply the already-authorized no-op stash state without reclassification."""
    return prepared.chip_present


def stash_draft_before_send(
    terminal_id: str,
    metadata: dict[str, Any],
    provider: Any,
    defer_on_dialog: bool = False,
) -> bool:
    """Apply native stash; return whether a chip is present for the paste."""
    if not isinstance(getattr(provider, "composer_stash_keys", None), list):
        return False

    for _ in range(STASH_SNAPSHOT_RETRIES):
        snapshot = _read_stash_snapshot(metadata, provider, defer_on_dialog)
        if snapshot is None:
            continue
        draft = snapshot.draft
        if isinstance(draft, str) and draft:
            draft = _wait_for_stable_draft(terminal_id, metadata, provider, draft)
            if not draft:
                continue
            _append_draft_log(terminal_id, draft)
            snapshot = _read_stash_snapshot(metadata, provider, defer_on_dialog)
            if snapshot is None or snapshot.draft != draft:
                continue

        before_key = _read_stash_snapshot(metadata, provider, defer_on_dialog)
        if before_key != snapshot:
            continue
        if snapshot.draft is None:
            break
        if snapshot.chip_present:
            if snapshot.draft:
                cleared = _clear_stash_draft(
                    terminal_id,
                    metadata,
                    provider,
                    snapshot.draft,
                    defer_on_dialog,
                )
                if cleared is not None and cleared.draft == "":
                    return cleared.chip_present
                raise DeliveryDeferredError(
                    f"Could not confirm composer clear for terminal {terminal_id}"
                )
            return True
        if snapshot.draft == "":
            return False

        _send_stash_keys(metadata, provider)
        confirmed = _read_stash_snapshot(metadata, provider, defer_on_dialog)
        if confirmed is not None and confirmed.chip_present and confirmed.draft == "":
            return True
        logger.warning("Native composer stash unconfirmed for terminal %s; degrading", terminal_id)
        raise DeliveryDeferredError(
            f"Could not confirm native composer stash for terminal {terminal_id}"
        )

    logger.warning(
        "Composer snapshot unreadable or changing for terminal %s; deferring delivery",
        terminal_id,
    )
    raise DeliveryDeferredError(
        f"Composer snapshot unreadable or changing for terminal {terminal_id}"
    )


def _read_stash_snapshot(
    metadata: dict[str, Any],
    provider: Any,
    defer_on_dialog: bool = False,
) -> ComposerSnapshot | None:
    try:
        captured = get_backend().get_history(
            metadata["tmux_session"],
            metadata["tmux_window"],
            tail_lines=PYTE_SCREEN_ROWS,
            strip_escapes=False,
        )
    except Exception:
        return None
    match_capture = strip_terminal_escapes(captured)
    if defer_on_dialog and CLAUDE_DIALOG_PATTERN.search(match_capture):
        raise DeliveryDeferredError("Claude dialog is active")
    try:
        lines = captured.splitlines()
        draft = provider.read_composer_draft(lines)
    except Exception:
        return None
    pattern = getattr(provider, "composer_stashed_chip_pattern", None)
    if pattern is None:
        return None
    return ComposerSnapshot(bool(pattern.search(captured)), draft)


def _send_stash_keys(metadata: dict[str, Any], provider: Any) -> None:
    backend = get_backend()
    for key in provider.composer_stash_keys:
        backend.send_special_key(metadata["tmux_session"], metadata["tmux_window"], key)


def _clear_stash_draft(
    terminal_id: str,
    metadata: dict[str, Any],
    provider: Any,
    draft: str,
    defer_on_dialog: bool = False,
) -> ComposerSnapshot | None:
    """C-u until empty; return the confirmed snapshot, or the latest observed one."""
    cap = draft.count("\n") + 4
    latest = None
    for _ in range(cap):
        snapshot = _read_stash_snapshot(metadata, provider, defer_on_dialog)
        if snapshot is not None:
            latest = snapshot
        if snapshot is not None and snapshot.draft == "":
            return snapshot
        if not _send_clear_keys(terminal_id, metadata, provider):
            break
    logger.warning("Could not confirm composer clear for terminal %s", terminal_id)
    return latest


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
    if draft is None:
        raise DeliveryDeferredError(
            f"Composer state is unreadable for terminal {terminal_id}"
        )
    if draft == "":
        return None

    draft = _wait_for_stable_draft(terminal_id, metadata, provider, draft)
    if draft == "":
        return None

    # Ghost-text discrimination is provider-authorized. Codex has empirically
    # clear-immune suggestions; an unchanged clear on any other provider is an
    # unconfirmed mutation and must defer rather than authorize a paste.
    if not _clear_step_changed_draft(terminal_id, metadata, provider, draft):
        logger.info(
            "Composer text for terminal %s unaffected by clear keys (ghost suggestion); "
            "not preserving as draft",
            terminal_id,
        )
        return None

    _append_draft_log(terminal_id, draft)
    if not _clear_composer(terminal_id, metadata, provider):
        # Never restore what we could not clear: re-pasting on top of the
        # leftover text would duplicate it in the composer.
        raise DeliveryDeferredError(
            f"Could not confirm composer clear for terminal {terminal_id}"
        )
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
        raise DeliveryDeferredError(
            f"Composer state became unreadable for terminal {terminal_id}"
        )
    if latest == first_draft:
        return latest

    deadline = time.monotonic() + DRAFT_STABILITY_TIMEOUT_SECONDS
    previous = latest
    while time.monotonic() < deadline:
        time.sleep(DRAFT_STABILITY_RECHECK_SECONDS)
        latest = _read_provider_draft(terminal_id, metadata, provider)
        if latest is None:
            raise DeliveryDeferredError(
                f"Composer state became unreadable for terminal {terminal_id}"
            )
        if latest == previous:
            return latest
        previous = latest
    logger.warning("Composer draft for terminal %s did not stabilize before delivery", terminal_id)
    return previous


def _send_clear_keys(terminal_id: str, metadata: dict[str, Any], provider: Any) -> bool:
    """Send one round of the provider's composer clear keys. False if none configured."""
    clear_keys = list(getattr(provider, "composer_clear_keys", []) or [])
    if not clear_keys:
        logger.warning("Draft preservation enabled for %s but no clear keys configured", terminal_id)
        return False
    backend = get_backend()
    for key in clear_keys:
        backend.send_special_key(metadata["tmux_session"], metadata["tmux_window"], key)
    return True


def _clear_step_changed_draft(
    terminal_id: str,
    metadata: dict[str, Any],
    provider: Any,
    draft: str,
) -> bool:
    """One clear iteration, then re-read: did the composer text change?

    True  ⇒ real draft (clear keys affected it).
    False ⇒ provider-authorized clear-immune ghost suggestion.
    Missing keys, exhausted None re-reads, and unchanged non-ghost text defer.
    """
    if not _send_clear_keys(terminal_id, metadata, provider):
        raise DeliveryDeferredError(
            f"Composer clear keys are unavailable for terminal {terminal_id}"
        )
    time.sleep(DRAFT_CLEAR_PROBE_RECHECK_DELAY_SECONDS)
    current: Optional[str] = None
    attempts = 1 + DRAFT_CLEAR_PROBE_NONE_RETRIES
    for attempt in range(attempts):
        if attempt > 0:
            time.sleep(DRAFT_CLEAR_PROBE_NONE_RETRY_DELAY_SECONDS)
        current = _read_draft_via_capture(metadata, provider)
        if current is not None:
            changed = current != draft
            if not changed and getattr(provider, "clear_immune_ghosts", False) is not True:
                raise DeliveryDeferredError(
                    f"Composer clear was not confirmed for terminal {terminal_id}"
                )
            logger.info(
                "clear-probe terminal=%s path=reread attempt=%d/%d current=%r "
                "verdict=%s",
                terminal_id,
                attempt + 1,
                attempts,
                current[:80] if current else current,
                "changed(real_draft)" if changed else "unchanged(ghost)",
            )
            return changed
        logger.debug(
            "clear-probe terminal=%s path=reread attempt=%d/%d got None",
            terminal_id,
            attempt + 1,
            attempts,
        )
    raise DeliveryDeferredError(
        f"Composer state is unreadable after clear for terminal {terminal_id}"
    )


def _provider_accepts_escapes(provider: Any) -> bool:
    return getattr(provider, "composer_parse_accepts_escapes", False) is True


def _read_provider_draft_from_capture(
    metadata: dict[str, Any],
    provider: Any,
    *,
    strip_escapes: bool,
) -> Optional[str]:
    """Capture pane and parse draft. ``strip_escapes`` selects plain vs -e."""
    try:
        captured = get_backend().get_history(
            metadata["tmux_session"],
            metadata["tmux_window"],
            tail_lines=PYTE_SCREEN_ROWS,
            strip_escapes=strip_escapes,
        )
    except Exception:
        return None
    try:
        return provider.read_composer_draft(captured.splitlines())
    except Exception:
        return None


def _read_draft_via_capture(
    metadata: dict[str, Any],
    provider: Any,
) -> Optional[str]:
    """Capture-based draft read, escape-preserving only when provider opts in.

    Codex sets ``composer_parse_accepts_escapes`` so dim-SGR ghosts are visible.
    Grok and other plain parsers must not receive ANSI. Opt-in providers that
    return None from an escape-preserving parse get a plain-capture last resort.
    """
    if _provider_accepts_escapes(provider):
        draft = _read_provider_draft_from_capture(
            metadata, provider, strip_escapes=False
        )
        if draft is not None:
            return draft
        return _read_provider_draft_from_capture(
            metadata, provider, strip_escapes=True
        )
    return _read_provider_draft_from_capture(metadata, provider, strip_escapes=True)


def _clear_composer(terminal_id: str, metadata: dict[str, Any], provider: Any) -> bool:
    """Drive the composer to empty. Returns True when confirmed empty."""
    for _ in range(DRAFT_CLEAR_MAX_ITERATIONS):
        current = _read_provider_draft(terminal_id, metadata, provider)
        if current == "":
            return True
        if not _send_clear_keys(terminal_id, metadata, provider):
            return False
        time.sleep(DRAFT_CLEAR_RECHECK_DELAY_SECONDS)

    logger.warning(
        "Composer draft for terminal %s remained after %d clear iterations",
        terminal_id,
        DRAFT_CLEAR_MAX_ITERATIONS,
    )
    return False


def _read_provider_draft(
    terminal_id: str,
    metadata: dict[str, Any],
    provider: Any,
) -> Optional[str]:
    # Escape-preserving capture is opt-in (codex dim-ghost detection). Other
    # providers keep plain capture / pyte so ANSI does not break their parsers.
    if _provider_accepts_escapes(provider):
        captured_draft = _read_draft_via_capture(metadata, provider)
        if captured_draft is not None:
            return captured_draft

    screen = _read_screen_lines(terminal_id, metadata)
    if screen is None:
        # Non-opt-in: try plain capture when pyte is also unavailable.
        if not _provider_accepts_escapes(provider):
            return _read_draft_via_capture(metadata, provider)
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
        # Plain capture only — escape-preserving is opt-in via _read_draft_via_capture.
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
