"""F640 (#495): codex terminals die at init — barrier fire-gate over-broadened.

ROOT CAUSE
----------
F635 (#490) fixed a matched-but-no-fire deadlock by deriving ``_effect_barrier``'s
match/settle region from the pyte-COMPOSITE screen (``get_rendered_screen``) — the
SAME domain the settle-digest is seeded in. That settle-domain change is correct.

But F635 *also* moved the barrier's ``rule.matches(match_region)`` FIRE-GATE onto
the composite. The composite retains text the live tmux VIEWPORT does not:

  * width-retained tail: a pane narrower than the pyte screen keeps the full
    logical line in the composite while the viewport truncates it, and
  * stale palimpsest rows: when the pyte screen size and the real pane size
    disagree, the composite degrades into a palimpsest of stale rows (see
    ``status_monitor._resolve_screen_size``).

Consequence (F640/#495): the barrier now FIRES a rule's answer keys on a WAITING
dialog whose actionable match exists ONLY in the composite and is NOT corroborated
by the live viewport. At base the barrier matched the viewport, so such an
uncorroborated match was absent and the send was WITHHELD. Post-F635 the keys are
sent into codex during init — into a mid-render/palimpsest frame or the wrong
widget — leaving codex with no ``›`` composer. The deferred-init delivery then
reads the composer, ``read_composer_draft`` returns ``None`` (no ``›``), and
``draft_guard.preserve_draft_before_send`` raises
``DeliveryDeferredError('Composer state is unreadable ...')``; three retries burn
the 180 s deadline and the terminal is torn down (``deferred_init_internal``).

THE FIX
-------
``_effect_barrier`` keeps the composite region for the SETTLE-DIGEST comparison
(preserving the #490 fix), but the FIRE-GATE now requires ``rule.matches`` to hold
on BOTH the composite AND the live viewport region. A dialog whose actionable
option labels are visible in the viewport (the real #490 resume-cwd chooser — its
labels are short; only the ``(path)`` tails truncate) still fires. A "match" that
lives only in width-retained/stale composite text no longer fires.

These tests mock the tmux boundary and drive the real fire path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.services import auto_responder as ar

# A resume-cwd-style chooser rule keyed on the option LABELS (short, width-safe).
RULE = ar.Rule(
    "codex-resume-working-directory",
    True,
    "regex",
    "Choose working directory to resume this session",
    ["Use session directory", "Use current directory", "Press enter to continue"],
    ["Down", "Enter"],
)


class _WaitingProvider:
    """Classifier says WAITING → the D2 fast-path makes the match eligible on the
    first eval, isolating the barrier fire-gate under test."""

    capabilities = ProviderCapabilities(supports_screen_detection=True)

    def get_status_from_screen(self, _lines):
        return TerminalStatus.WAITING_USER_ANSWER

    def chrome_row_patterns(self):
        return None


def _metadata():
    return {
        "id": "term1",
        "tmux_session": "cao-sess",
        "tmux_window": "win",
        "provider": "codex",
        "provider_session_id": None,
        "lifecycle_generation": 7,
    }


def _wire(monkeypatch, tmp_path, *, composite, viewport):
    """Mock the OUTERMOST boundaries only; let settle/barrier/fire run for real.

    ``composite`` is what ``get_rendered_screen`` (the settle-seed domain and, post-
    F635, the barrier match domain) returns. ``viewport`` is what the raw tmux
    capture (``capture_viewport``) returns — the LIVE pane.
    """
    metadata = _metadata()
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.capture_viewport.return_value = "\n".join(viewport)

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda tid: metadata if tid == metadata["id"] else None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_env.get_session_env", lambda _s: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session", lambda _s: []
    )
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.seam_activation.receiver_state_active", lambda _op: False
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
        lambda tid: list(composite),
    )
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [RULE])
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    monkeypatch.setattr(ar.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ar, "_clock_sleep", lambda _s: None)

    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._t, self._a, self._k = target, args, kwargs or {}

        def start(self):
            self._t(*self._a, **(self._k or {}))

    monkeypatch.setattr(ar.threading, "Thread", _SyncThread)
    return backend


def _sent_keys(backend):
    return [call.args[2] for call in backend.send_special_key.call_args_list]


# A chooser whose option LABELS are pushed PAST a narrow pane width: the composite
# keeps them; the live viewport (truncated at 80 cols) does NOT. This is the F640
# shape — the match is uncorroborated by the live pane.
_PAD = " " * 90
_UNCORROBORATED_COMPOSITE = [
    "Choose working directory to resume this session",
    "  Current = your current working directory",
    "  1." + _PAD + "Use session directory (/home/x/proj)",
    "> 2." + _PAD + "Use current directory (/home/x/proj/.cao/worktrees/abc)",
    "  3." + _PAD + "Press enter to continue",
]
_VIEWPORT_TRUNCATED = [r[:80] for r in _UNCORROBORATED_COMPOSITE]


# ---------------------------------------------------------------------------
# Regression: an uncorroborated composite-only match must NOT fire keys.
# ---------------------------------------------------------------------------


def test_uncorroborated_composite_match_does_not_fire(monkeypatch, tmp_path):
    """F640/#495 regression. The rule's actionable option labels exist ONLY in the
    composite (pushed past the 80-col viewport). The barrier must NOT send keys —
    a fire uncorroborated by the live pane is what drove codex into a no-composer
    state at init. Pre-fix (F635 fire-gate on the composite) this sent
    ['Down','Enter']; the fix requires viewport corroboration, so ZERO keys."""
    # Precondition: the rule matches the composite but NOT the truncated viewport.
    assert RULE.matches(ar.dialog_region(_UNCORROBORATED_COMPOSITE))
    assert not RULE.matches(ar.dialog_region(_VIEWPORT_TRUNCATED))

    backend = _wire(
        monkeypatch,
        tmp_path,
        composite=_UNCORROBORATED_COMPOSITE,
        viewport=_VIEWPORT_TRUNCATED,
    )

    ar.AutoResponder().on_screen("term1", _WaitingProvider(), list(_UNCORROBORATED_COMPOSITE))

    assert _sent_keys(backend) == [], (
        "an actionable match present only in the width-retained/palimpsest "
        "composite and NOT in the live viewport must NOT fire keys (F640/#495)"
    )


# ---------------------------------------------------------------------------
# Control: the genuine #490 wide chooser still fires (fix does not regress F635).
# ---------------------------------------------------------------------------

# The real resume-cwd chooser: option LABELS are short and survive truncation; only
# the long (worktree path) tails differ between composite and viewport. The rule
# matches BOTH, so the send must still fire — F635's #490 fix is preserved.
_WIDE_COMPOSITE = [
    "Choose working directory to resume this session",
    "  Current = your current working directory",
    "› 1. Use session directory (/home/chao/VScode_projects/grok-box-setup)",
    "  2. Use current directory "
    "(/home/chao/VScode_projects/grok-box-setup/.cao/worktrees/59c6884c-longlonglong)",
    "  3. Always use session directory",
    "  Press enter to continue",
]
_WIDE_VIEWPORT = [r[:72] for r in _WIDE_COMPOSITE]


def test_corroborated_wide_chooser_still_fires(monkeypatch, tmp_path):
    """Control / #490 preservation: the real resume-cwd chooser's option labels
    are visible in BOTH the composite and the truncated viewport (only the path
    tails truncate). The barrier MUST still fire its answer keys."""
    # Precondition: matches BOTH domains despite the path-tail divergence.
    assert RULE.matches(ar.dialog_region(_WIDE_COMPOSITE))
    assert RULE.matches(ar.dialog_region(_WIDE_VIEWPORT))
    # And the two digests genuinely differ (the #490 cross-domain condition).
    assert ar._digest_normalized(
        ar.dialog_region(_WIDE_COMPOSITE).normalized
    ) != ar._digest_normalized(ar.dialog_region(_WIDE_VIEWPORT).normalized)

    backend = _wire(monkeypatch, tmp_path, composite=_WIDE_COMPOSITE, viewport=_WIDE_VIEWPORT)

    ar.AutoResponder().on_screen("term1", _WaitingProvider(), list(_WIDE_COMPOSITE))

    assert _sent_keys(backend)[:2] == ["Down", "Enter"], (
        "the genuine #490 wide chooser (labels visible in the viewport) must "
        "still fire — the F640 fix must not reintroduce the #490 deadlock"
    )
