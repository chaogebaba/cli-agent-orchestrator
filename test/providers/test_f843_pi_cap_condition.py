"""F843 (#700): the pi_cli / ClinePass 429 INFERENCE_CAP_ERROR banner must
classify as a CAPPED condition (subtype ``usage_limit_window``) with a
``resets in …`` reset hint, high confidence, provider ``pi_cli``.

The pane text below is VERBATIM from the live panes recorded in the issue
(five pi lanes, 2026-09-08): the per-retry banner
    Error: 429: {"code":"INFERENCE_CAP_ERROR","message":"Error 429: You have
    reached your 5-hour Clinepass limit. The limit resets in 1h 29m, please try
    again later."}
repeated, then the final
    Error: Retry failed after 3 attempts: 429: {...same...}
Both the UNWRAPPED (single-row) and WRAPPED (JSON split across two pane rows)
forms are exercised, plus a fenced/quoted negative (a supervisor pasting the
banner back to the seat must NOT fire a false cap — the F836 quoted-text rule).
"""

from __future__ import annotations

from cli_agent_orchestrator.providers.condition import (
    ConditionKind,
    Confidence,
    classify_condition,
    should_deliver,
)

# ── The verbatim banner (single logical line, unwrapped) ────────────────────────
_BANNER = (
    'Error: 429: {"code":"INFERENCE_CAP_ERROR","message":"Error 429: You have '
    "reached your 5-hour Clinepass limit. The limit resets in 1h 29m, please try "
    'again later."}'
)
_RETRY_FAILED = (
    'Error: Retry failed after 3 attempts: 429: {"code":"INFERENCE_CAP_ERROR",'
    '"message":"Error 429: You have reached your 5-hour Clinepass limit. The '
    'limit resets in 1h 29m, please try again later."}'
)

# Pi's composer chrome (two box rules + a footer context readout) drawn below the
# transcript. A genuine capped pane sits at the composer with the banner above it.
_RULE = "─" * 120
_FOOTER = (
    "↑26k ↓172 R3.9k CH97.6% $0.002 0.4%/1.0M (auto)"
    "                                        cline-pass/glm-5.3-flash • high"
)


def _capped_pane_unwrapped() -> str:
    """The live capped pane, banner UNWRAPPED (each Error line on one row)."""
    rows = [
        " Call the probe_echo tool with text=HELLO2. Then say DONE2.",
        "",
        _BANNER,
        _BANNER,
        _BANNER,
        _BANNER,
        _RETRY_FAILED,
        "",
        _RULE,
        " ",
        _RULE,
        "/data/claude-scratch/worker-scratch/f843/probe",
        _FOOTER,
    ]
    return "\n".join(rows)


def _capped_pane_wrapped() -> str:
    """The live capped pane, banner WRAPPED across two pane rows.

    Pi wraps the long JSON at the terminal width: the ``{"code":
    "INFERENCE_CAP_ERROR"`` prefix lands on the first row and the human
    ``message`` continues on the next. The CAPPED anchor is the machine code on
    the first row, so wrapping does not defeat detection.
    """
    row1 = 'Error: 429: {"code":"INFERENCE_CAP_ERROR","message":"Error 429: You have'
    row2 = (
        "reached your 5-hour Clinepass limit. The limit resets in 1h 29m, please "
        'try again later."}'
    )
    rows = [
        " Call the probe_echo tool with text=HELLO2. Then say DONE2.",
        "",
        row1,
        row2,
        row1,
        row2,
        "",
        _RULE,
        " ",
        _RULE,
        "/data/claude-scratch/worker-scratch/f843/probe",
        _FOOTER,
    ]
    return "\n".join(rows)


def _quoted_in_fence_pane() -> str:
    """A supervisor pastes the banner back to the seat INSIDE a code fence.

    This is transcript/quoted text, not a live cap — it must NOT fire (the F836
    quoted-text discipline the brief cites). Pi is otherwise idle at its
    composer, having answered the question below the fence.
    """
    rows = [
        " Here is the error we saw earlier — is this a cap?",
        "",
        "```",
        _BANNER,
        _RETRY_FAILED,
        "```",
        "",
        " Yes, that is a ClinePass usage cap; the window resets shortly.",
        "",
        _RULE,
        " ",
        _RULE,
        "/data/claude-scratch/worker-scratch/f843/probe",
        _FOOTER,
    ]
    return "\n".join(rows)


def test_pi_cap_unwrapped_classifies_capped() -> None:
    cond = classify_condition(_capped_pane_unwrapped(), "pi_cli")
    assert cond is not None, "expected a CAPPED condition for the pi 429 banner"
    assert cond.kind is ConditionKind.CAPPED
    assert cond.subtype == "usage_limit_window"
    assert cond.provider == "pi_cli"
    assert cond.confidence is Confidence.HIGH
    assert cond.reset_hint == "resets in 1h 29m"
    assert should_deliver(cond), "a high-confidence cap must surface an event"


def test_pi_cap_wrapped_classifies_capped() -> None:
    """The wrapped banner (JSON split across two rows) still classifies CAPPED
    with the reset hint — the anchor is the machine code on the first row."""
    cond = classify_condition(_capped_pane_wrapped(), "pi_cli")
    assert cond is not None
    assert cond.kind is ConditionKind.CAPPED
    assert cond.subtype == "usage_limit_window"
    assert cond.reset_hint == "resets in 1h 29m"


def test_pi_cap_quoted_in_fence_does_not_fire() -> None:
    """A code-fenced quote of the banner is transcript, not a live cap → no
    CAPPED (F836 quoted-text discipline)."""
    cond = classify_condition(_quoted_in_fence_pane(), "pi_cli")
    assert (
        cond is None or cond.kind is not ConditionKind.CAPPED
    ), f"a fenced/quoted 429 must not fire CAPPED, got {cond}"


def test_pi_cap_reset_hint_comma_boundary() -> None:
    """The pi banner ends the duration with a COMMA (``resets in 1h 29m,``); the
    hint extractor must stop at the comma and not swallow the trailing prose."""
    cond = classify_condition(_capped_pane_unwrapped(), "pi_cli")
    assert cond is not None and cond.reset_hint == "resets in 1h 29m"
