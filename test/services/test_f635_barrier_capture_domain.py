"""F635 #490: auto-responder matched-but-no-fire — barrier settle-digest domain.

ROOT CAUSE
----------
A matched rule's settle-digest is seeded (in ``_on_screen`` / ``_verify_and_retry``)
from the pyte-COMPOSITE screen (``status_monitor.get_rendered_screen``). But
``_effect_barrier`` re-captured the pane via ``capture_viewport`` (the RAW tmux
viewport) and compared *that* digest against the composite seed. Those are two
DIFFERENT capture domains:

  * composite (get_rendered_screen): the full logical pyte screen — long lines
    are retained at their true width;
  * viewport (capture_viewport): the visible cells only — a line wider than the
    pane is TRUNCATED at the terminal width.

For the codex ``resume-cwd`` chooser, whose options carry long absolute worktree
paths, the two canonical/normalized strings — and thus ``_digest_normalized`` —
legitimately DIFFER even though it is the SAME static dialog (``rule.matches`` is
True on both). The barrier's cross-domain ``settle_ok`` equality therefore NEVER
agreed: the send was withheld on every eval, ``matched/firing`` was re-logged
~2/s, and the per-rule cooldown (set only on a real send) was never armed — the
exact deadlock reported in #490 (distinct from #386, where the rule reports
NO-match; here it reports firing yet nothing reaches the pane).

THE FIX
-------
The match+settle decision must run in the SAME domain the settle-digest was
seeded in. ``_effect_barrier`` now derives its match/settle region from the
composite screen (``_current_normalized_filtered`` → ``get_rendered_screen``),
falling back to the viewport region only when no composite is available. The raw
viewport capture is still used for the provider ``status`` classify.

These tests mock the tmux boundary (``send_special_key``) and drive the real
fire path. A mutant that reverts the fix (barrier matches on the viewport) or one
that drops the send outright must go RED.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.services import auto_responder as ar

# The resume-cwd chooser as the DETECTION TICK sees it (pyte composite): the
# option lines carry the full, untruncated worktree paths.
COMPOSITE_SCREEN = [
    "  Choose working directory to resume this session",
    "",
    "  Current = your current working directory",
    "",
    "› 1. Use session directory (/home/chao/VScode_projects/grok-box-setup)",
    "  2. Use current directory "
    "(/home/chao/VScode_projects/grok-box-setup/.cao/worktrees/59c6884c)",
    "  3. Always use session directory",
    "  4. Always use current directory",
    "",
    "  Press enter to continue",
]

# The SAME dialog as the raw tmux viewport returns it: the long option lines are
# TRUNCATED at the pane width. Same words up to the cut, different tail — so the
# canonical string and digest differ from the composite above.
VIEWPORT_TRUNCATED_SCREEN = [
    "  Choose working directory to resume this session",
    "",
    "  Current = your current working directory",
    "",
    "› 1. Use session directory (/home/chao/VScode_projects/grok-box-set",
    "  2. Use current directory (/home/chao/VScode_projects/grok-box-set",
    "  3. Always use session directory",
    "  4. Always use current directory",
    "",
    "  Press enter to continue",
]

# #490's supervisor rule: regex match on the chooser title, options=["continue"],
# answer selects option 2 then confirms.
RESUME_RULE = ar.Rule(
    "codex-resume-workdir-card",
    True,
    "regex",
    "choose working directory to resume this session",
    ["continue"],
    ["2", "Enter"],
)


class _WaitingProvider:
    """Classifier says WAITING → D2 fast-path makes the match eligible on the
    first eval, isolating the barrier's settle-domain behaviour under test."""

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
        "lifecycle_generation": 0,
    }


def _wire(monkeypatch, tmp_path, *, composite, viewport):
    """Mock the OUTERMOST boundaries only; let settle/barrier/fire run for real.

    ``composite`` is what the pyte-composite path (get_rendered_screen) returns —
    the domain the settle-digest is seeded in. ``viewport`` is what the raw tmux
    capture (capture_viewport / send_keys target) returns.
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
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.seam_activation.receiver_state_active", lambda _op: False
    )
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
        lambda tid: list(composite),
    )
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [RESUME_RULE])
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    # Neutralise real waits; run the verify/retry thread synchronously so the
    # whole fire→retry chain executes inside the on_screen call.
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


# ---------------------------------------------------------------------------
# Regression: keys REACH the pane even when composite and viewport diverge.
# ---------------------------------------------------------------------------


def test_matched_rule_delivers_keys_despite_capture_domain_divergence(monkeypatch, tmp_path):
    """The composite (detection tick / settle seed) retains the full worktree
    path; the raw viewport truncates it. The rule matches BOTH, and a firing rule
    MUST deliver its key sequence to the pane. Pre-fix this deadlocked with zero
    sends (the barrier compared the truncated-viewport digest against the
    composite seed and never agreed)."""
    backend = _wire(
        monkeypatch, tmp_path, composite=COMPOSITE_SCREEN, viewport=VIEWPORT_TRUNCATED_SCREEN
    )

    result = ar.AutoResponder().on_screen("term1", _WaitingProvider(), COMPOSITE_SCREEN)

    # The rule fired: the exact answer sequence reached the tmux boundary, in
    # order, on the correct session/window.
    keys = _sent_keys(backend)
    assert keys[:2] == ["2", "Enter"], f"expected ['2','Enter'] first, got {keys!r}"
    for call in backend.send_special_key.call_args_list:
        assert call.args[:2] == ("cao-sess", "win")
    assert result is None  # fired → falls through to normal detection


def test_precondition_composite_and_viewport_digests_differ():
    """Guard: the two capture domains really DO produce different digests for
    this dialog while both still match the rule — otherwise the regression above
    would be vacuously green."""
    comp = ar.dialog_region(COMPOSITE_SCREEN)
    view = ar.dialog_region(VIEWPORT_TRUNCATED_SCREEN)
    assert RESUME_RULE.matches(comp)
    assert RESUME_RULE.matches(view)
    assert ar._digest_normalized(comp.normalized) != ar._digest_normalized(view.normalized)


# ---------------------------------------------------------------------------
# Mutant kills.
# ---------------------------------------------------------------------------


def test_mutant_barrier_matches_on_viewport_deadlocks(monkeypatch, tmp_path):
    """MUTANT (reverts the fix): force the barrier to derive its match/settle
    region from the RAW viewport again (composite unavailable). The cross-domain
    settle-digest never agrees → the send is withheld → ZERO keys reach the pane.
    This is the #490 deadlock; the fixed code must NOT behave this way."""
    backend = _wire(
        monkeypatch, tmp_path, composite=COMPOSITE_SCREEN, viewport=VIEWPORT_TRUNCATED_SCREEN
    )
    # Simulate the pre-fix behaviour: no composite available to the barrier, so it
    # falls back to the truncated viewport for match/settle (the old code path).
    monkeypatch.setattr(
        ar.AutoResponder, "_current_normalized_filtered", lambda self, tid, chrome: None
    )

    ar.AutoResponder().on_screen("term1", _WaitingProvider(), COMPOSITE_SCREEN)

    assert _sent_keys(backend) == [], (
        "mutant matching on the truncated viewport must deadlock (no keys) — "
        "this is exactly the #490 defect the fix removes"
    )


def test_mutant_dropping_send_goes_red(monkeypatch, tmp_path):
    """MUTANT: neutralise ``_send_answer`` (drop the tmux send). No key reaches
    the pane, so the delivery assertion the regression relies on goes RED."""
    backend = _wire(
        monkeypatch, tmp_path, composite=COMPOSITE_SCREEN, viewport=VIEWPORT_TRUNCATED_SCREEN
    )
    monkeypatch.setattr(ar.AutoResponder, "_send_answer", lambda self, *a, **k: True)

    ar.AutoResponder().on_screen("term1", _WaitingProvider(), COMPOSITE_SCREEN)

    assert backend.send_special_key.call_count == 0
    # And the regression's own assertion would fail on this mutant:
    with pytest.raises(AssertionError):
        assert _sent_keys(backend)[:2] == ["2", "Enter"]


def test_short_dialog_unaffected_still_fires(monkeypatch, tmp_path):
    """Control: a dialog with no width-dependent lines (trust-dir style) has
    identical composite/viewport digests and must keep firing exactly as before —
    the fix does not disturb the working case."""
    short = [
        "Do you trust the contents of this directory?",
        "› 1. Yes, continue",
        "  2. No, quit",
        "Press enter to continue",
    ]
    rule = ar.Rule(
        "codex-trust-dir",
        True,
        "contains",
        "Do you trust the contents of this directory?",
        ["Yes, continue", "No, quit"],
        ["Enter"],
    )
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [rule])
    backend = _wire(monkeypatch, tmp_path, composite=short, viewport=short)
    # _wire installed the resume rule; override to the trust rule for this case.
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [rule])

    ar.AutoResponder().on_screen("term1", _WaitingProvider(), short)

    assert _sent_keys(backend)[:1] == ["Enter"]
