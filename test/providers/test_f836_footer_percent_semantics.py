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
    condition — exhaustion is decided by the USED% value, not the glyph. The
    status bar sits above the real kiro composer placeholder (F836 r3 anchor)."""
    _composer = "\n ask a question or describe a task ↵\n"
    for glyph in ("◔", "◑", "◕", "●"):
        pane = f"kiro_cli_dev · Auto · {glyph}      /path · (branch){_composer}"
        assert classify_condition(pane, "kiro_cli") is None, f"glyph {glyph} alone fired"
        low = f"kiro_cli_dev · Auto · {glyph} 12%      /path · (branch){_composer}"
        assert classify_condition(low, "kiro_cli") is None, f"glyph {glyph} 12% fired"


def test_kiro_threshold_boundary() -> None:
    """kiro fires at exactly the USED threshold, not one below it.

    F836 r3: the status bar is anchored to the live kiro composer placeholder
    ("ask a question or describe a task"), which sits BELOW the status bar in the
    real TUI — so the boundary panes carry the real composer row, not a bare "›".
    """
    _composer = " ask a question or describe a task ↵\n"
    below = f"kiro_cli_dev · Auto · ● {KIRO_CONTEXT_USED_THRESHOLD - 1}%   /p · (b)\n{_composer}"
    at = f"kiro_cli_dev · Auto · ● {KIRO_CONTEXT_USED_THRESHOLD}%   /p · (b)\n{_composer}"
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


# ── F836 r2 (#693) — footer matching must anchor to the status-bar row, never
# user-authored prompt/transcript text (EMPIRICAL-GATE-NO blocker fix) ─────────

# (label, provider, pane) — each MUST classify to no condition: the footer-like
# text is user-authored (a "›" prompt) or plain transcript, not the status bar.
_R2_NEGATIVE = [
    (
        "codex-user-prompt",
        "codex",
        "• Sure.\n\n› Please analyze this literal string: Context 8% left\n",
    ),
    (
        "codex-transcript",
        "codex",
        "• The user pasted: Context 8% left · 5h 47% left earlier.\n• Done.\n",
    ),
    (
        "kiro-user-prompt",
        "kiro_cli",
        "  Working.\n\n› Explain this copied status text: kiro_cli_dev · Auto · ● 92%\n",
    ),
    (
        "kiro-transcript",
        "kiro_cli",
        "  I see the string ● 92% in your paste.\n",
    ),
    # A prompt quoting the FULL codex status bar verbatim (rate-window included)
    # is still user text: the leading "›" disqualifies the row.
    (
        "codex-user-prompt-full-footer",
        "codex",
        "› paste: ~/x · main · gpt-5.6-sol high · Context 8% left · 5h 47% left\n",
    ),
]


@pytest.mark.parametrize("label,provider,pane", _R2_NEGATIVE)
def test_r2_user_footer_text_does_not_fire(label, provider, pane) -> None:
    assert classify_condition(pane, provider) is None, f"{label}: user text wrongly fired"


def test_r2_real_footer_still_fires_below_composer() -> None:
    """The genuine status bar sits BELOW the "›" composer and must still fire —
    the anchoring must not cost below-composer reachability."""
    codex = (
        "• working\n\n› Ask Codex to do anything\n\n"
        "  ~/x · main · gpt-5.6-sol high · Context 7% left · 5h 47% left\n"
    )
    c = classify_condition(codex, "codex")
    assert c is not None and c.kind is ConditionKind.CONTEXT_EXHAUSTED
    assert c.subtype == "footer_percent_status"

    kiro = (
        "  Parked idle.\n\n"
        "kiro_cli_dev · Auto · ● 92%                    /data/x · (cao/x)\n\n"
        "›  ask a question or describe a task\n"
    )
    k = classify_condition(kiro, "kiro_cli")
    assert k is not None and k.kind is ConditionKind.CONTEXT_EXHAUSTED
    assert k.subtype == "footer_percent_status"


def test_r2_codex_context_without_rate_window_is_not_a_footer() -> None:
    """A row with "Context N% left" but NO 5h rate-window is not the codex status
    bar (the real bar always carries the rate-window) — do not fire."""
    pane = "  a note: Context 8% left\n"
    assert classify_condition(pane, "codex") is None


def test_r2_kiro_glyph_without_chrome_is_not_a_footer() -> None:
    """A row with a glyph+percent but no leading agent·mode chrome is not the
    kiro status bar — do not fire."""
    pane = "  ● 92% of the way there\n"
    assert classify_condition(pane, "kiro_cli") is None


# ── F836 r3 (#693) — footer located by POSITION relative to the live composer,
# never by content shape. The r2 gate reported four fresh false positives that a
# glyph-denylist + co-signature let through; each is quoted/fenced/bulleted
# status-bar text that lives ABOVE the composer (or on a pane with no composer at
# all) and MUST NOT fire. (EMPIRICAL-GATE-NO blocker fix.) ────────────────────

# The four r2-verdict adversarial probes (verbatim substance). Each MUST be quiet:
# a fenced/indented/bulleted quote of a status bar is transcript territory, above
# the live composer (or on a composer-less pane), so the position rule excludes it
# by construction — no new glyph rule.
_R3_VERDICT_NEGATIVE = [
    # A fenced Kiro status quote inside a ``` code fence, ABOVE the live composer.
    (
        "fenced-kiro-quote",
        "kiro_cli",
        "• Here is what my bar showed:\n"
        "```text\n"
        "kiro_cli_dev · Auto · ● 92%    /path · (branch)\n"
        "```\n"
        "\n"
        " ask a question or describe a task ↵\n",
    ),
    # A fenced Codex status quote inside a ``` code fence, ABOVE the live composer.
    (
        "fenced-codex-quote",
        "codex",
        "• Here is what my bar showed:\n"
        "```text\n"
        "~/x · main · gpt-5.6-sol high · Context 8% left · 5h 47% left\n"
        "```\n"
        "\n"
        "› Ask Codex to do anything\n",
    ),
    # An indented Codex transcript continuation line under a bullet — the pane has
    # NO live composer marker, so there is no live status bar to read.
    (
        "indented-codex-transcript",
        "codex",
        "• You pasted this status:\n"
        "    ~/x · main · gpt-5.6-sol high · Context 8% left · 5h 47% left\n",
    ),
    # A Markdown bullet quoting a Kiro status — no live composer marker on the pane.
    (
        "markdown-kiro-bullet",
        "kiro_cli",
        "• Sure, I see it.\n- copied · status · ● 92%\n",
    ),
]


@pytest.mark.parametrize("label,provider,pane", _R3_VERDICT_NEGATIVE)
def test_r3_verdict_probes_do_not_fire(label, provider, pane) -> None:
    assert classify_condition(pane, provider) is None, f"{label}: quoted status wrongly fired"


def test_r3_genuine_footer_below_composer_fires_codex() -> None:
    """A genuine codex status bar at 8% left, BELOW the live composer, fires with
    high confidence (the required positive)."""
    pane = (
        "• done.\n\n"
        "› Ask Codex to do anything\n\n"
        "  ~/x · main · gpt-5.6-sol high · Context 8% left · 5h 47% left\n"
    )
    c = classify_condition(pane, "codex")
    assert c is not None and c.kind is ConditionKind.CONTEXT_EXHAUSTED
    assert c.subtype == "footer_percent_status" and c.confidence.value == "high"


def test_r3_genuine_footer_fires_kiro() -> None:
    """A genuine kiro status bar at 92% used, ABOVE the live composer, fires with
    high confidence."""
    pane = (
        "  Parked idle.\n\n"
        "kiro_cli_dev · Auto · ● 92%                    /data/x · (cao/x)\n\n"
        " ask a question or describe a task ↵\n"
    )
    k = classify_condition(pane, "kiro_cli")
    assert k is not None and k.kind is ConditionKind.CONTEXT_EXHAUSTED
    assert k.subtype == "footer_percent_status" and k.confidence.value == "high"


def test_r3_quoted_footer_above_and_live_footer_fires_once_codex() -> None:
    """A pane that BOTH quotes a footer above the composer AND has a genuine live
    footer below it fires exactly once, reading the LIVE row — the quote is inert.

    The quoted row reads 3% left (would be exhausted if read); the live row reads
    a healthy 77% left. The verdict must reflect the LIVE row: NOT exhausted."""
    pane = (
        "• Earlier my bar said:\n"
        "```text\n"
        "~/x · main · gpt-5.6-sol high · Context 3% left · 5h 9% left\n"
        "```\n\n"
        "› Ask Codex to do anything\n\n"
        "  ~/x · main · gpt-5.6-sol high · Context 77% left · 5h 47% left\n"
    )
    # The live row (77% left) is healthy → no condition; the quoted 3% is ignored.
    assert classify_condition(pane, "codex") is None


def test_r3_quoted_footer_above_and_live_footer_fires_once_kiro() -> None:
    """kiro twin of the above: a quoted 92%-used bar above the composer plus a
    genuine healthy 30%-used live bar → reads the LIVE row only → not exhausted."""
    pane = (
        "• Earlier my bar said:\n"
        "```text\n"
        "kiro_cli_dev · Auto · ● 92%    /path · (branch)\n"
        "```\n\n"
        "kiro_cli_dev · Auto · ◑ 30%                    /data/x · (cao/x)\n\n"
        " ask a question or describe a task ↵\n"
    )
    assert classify_condition(pane, "kiro_cli") is None


def test_r3_no_composer_pane_does_not_fire() -> None:
    """A pane with NO composer marker (mid-init, alt-screen, a bare transcript
    paste) has no live status bar to anchor on → footer classification is quiet,
    for both providers, even when a footer-shaped row is present."""
    codex_no_composer = "  ~/x · main · gpt-5.6-sol high · Context 4% left · 5h 9% left\n"
    assert classify_condition(codex_no_composer, "codex") is None
    kiro_no_composer = "kiro_cli_dev · Auto · ● 95%                    /data/x · (cao/x)\n"
    assert classify_condition(kiro_no_composer, "kiro_cli") is None
