"""F836 (#693) — per-provider footer-percent SEMANTICS for CONTEXT_EXHAUSTED.

The footer percentage means OPPOSITE things per provider:

  * kiro footer  "agent · model · ◑ NN%"        → NN is context USED  → exhausted
    only at NN >= 90 (a pie glyph ◔◑◕● precedes it; the glyph alone is never
    exhaustion — a healthy warm kiro seat sits at ◑ 30% used).
  * codex footer "Context NN% left · 5h MM% left" → NN is context LEFT  → exhausted
    only at NN <= 10 (the trailing "5h MM% left" is the rate window, not context).

The pre-fix detector fired CONTEXT_EXHAUSTED on the kiro ◑ 30% footer (a false
positive) and used a single left%-only threshold of 15. This arm pins one row per
real footer sample, positive and negative, for both providers.

Table-driven: each row is (fixture, provider, expected-kind-or-None, subtype).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.condition import (
    CODEX_CONTEXT_LEFT_THRESHOLD,
    KIRO_CONTEXT_USED_THRESHOLD,
    ConditionKind,
    classify_condition,
)

_COND = Path(__file__).parent / "fixtures" / "conditions"


def _load(name: str) -> str:
    return (_COND / f"{name}.txt").read_text(encoding="utf-8")


# (fixture, provider, expected ConditionKind or None, expected subtype-when-fired)
_FOOTER_CASES = [
    # kiro: 30% USED is healthy (the F836 false positive) → no condition.
    ("kiro-cli-footer-healthy-1", "kiro_cli", None, None),
    # kiro: 92% USED >= 90 → genuine CONTEXT_EXHAUSTED.
    (
        "kiro-cli-footer-exhausted-1",
        "kiro_cli",
        ConditionKind.CONTEXT_EXHAUSTED,
        "footer_percent_status",
    ),
    # codex: 97% LEFT is healthy → no condition (left% > 10).
    ("codex-footer-healthy-1", "codex", None, None),
    # codex: 7% LEFT <= 10 → genuine CONTEXT_EXHAUSTED.
    (
        "codex-footer-exhausted-1",
        "codex",
        ConditionKind.CONTEXT_EXHAUSTED,
        "footer_percent_status",
    ),
]


@pytest.mark.parametrize("name,provider,kind,subtype", _FOOTER_CASES)
def test_footer_percent_semantics(name, provider, kind, subtype) -> None:
    cond = classify_condition(_load(name), provider)
    if kind is None:
        assert cond is None, f"{name}: expected NO condition, got {cond!r}"
    else:
        assert cond is not None, f"{name}: expected {kind}, got None"
        assert cond.kind is kind, f"{name}: {cond.kind} != {kind}"
        assert cond.subtype == subtype, f"{name}: subtype {cond.subtype!r} != {subtype!r}"


def test_kiro_glyph_alone_is_never_exhausted() -> None:
    """The pie glyph WITHOUT a number (or with a low number) is never a
    condition — exhaustion is decided by the USED% value, not the glyph."""
    for glyph in ("◔", "◑", "◕", "●"):
        pane = f"kiro_cli_dev · Auto · {glyph}      /path · (branch)\n›  ask\n"
        assert classify_condition(pane, "kiro_cli") is None, f"glyph {glyph} alone fired"
        low = f"kiro_cli_dev · Auto · {glyph} 12%      /path · (branch)\n›  ask\n"
        assert classify_condition(low, "kiro_cli") is None, f"glyph {glyph} 12% fired"


def test_kiro_threshold_boundary() -> None:
    """kiro fires at exactly the USED threshold, not one below it."""
    below = f"kiro_cli_dev · Auto · ● {KIRO_CONTEXT_USED_THRESHOLD - 1}%   /p · (b)\n›  ask\n"
    at = f"kiro_cli_dev · Auto · ● {KIRO_CONTEXT_USED_THRESHOLD}%   /p · (b)\n›  ask\n"
    assert classify_condition(below, "kiro_cli") is None
    cond = classify_condition(at, "kiro_cli")
    assert cond is not None and cond.kind is ConditionKind.CONTEXT_EXHAUSTED


def test_codex_threshold_boundary() -> None:
    """codex fires at exactly the LEFT threshold, not one above it."""
    above = f"› Ask Codex to do anything\n  m · Context {CODEX_CONTEXT_LEFT_THRESHOLD + 1}% left · 5h 0% left\n"
    at = f"› Ask Codex to do anything\n  m · Context {CODEX_CONTEXT_LEFT_THRESHOLD}% left · 5h 0% left\n"
    assert classify_condition(above, "codex") is None
    cond = classify_condition(at, "codex")
    assert cond is not None and cond.kind is ConditionKind.CONTEXT_EXHAUSTED


def test_codex_rate_window_not_read_as_context() -> None:
    """A healthy context with a LOW rate-limit window ('5h 3% left') must NOT
    fire — the 3% is the rate window, not the context percentage."""
    pane = "› Ask Codex to do anything\n  m · Context 88% left · 5h 3% left\n"
    assert classify_condition(pane, "codex") is None
