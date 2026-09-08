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
    F836 r4: the live TUI redraws bar + blank + composer (every real capture
    separates the bar from the composer by a blank row); the boundary panes carry
    that blank separator so they exercise the live-bar path.
    """
    _composer = "\n ask a question or describe a task ↵\n"
    below = f"kiro_cli_dev · Auto · ● {KIRO_CONTEXT_USED_THRESHOLD - 1}%   /p · (b)\n{_composer}"
    at = f"kiro_cli_dev · Auto · ● {KIRO_CONTEXT_USED_THRESHOLD}%   /p · (b)\n{_composer}"
    assert classify_condition(below, "kiro_cli") is None
    cond = classify_condition(at, "kiro_cli")
    assert cond is not None and cond.kind is ConditionKind.CONTEXT_EXHAUSTED


def test_codex_threshold_boundary() -> None:
    """codex fires at exactly the LEFT threshold, not one above it.

    F836 r4: the live TUI redraws bar + blank + composer; the boundary panes
    carry the blank separator so they exercise the live-bar path.
    """
    above = f"› Ask Codex to do anything\n\n  m · Context {CODEX_CONTEXT_LEFT_THRESHOLD + 1}% left · 5h 0% left\n"
    at = f"› Ask Codex to do anything\n\n  m · Context {CODEX_CONTEXT_LEFT_THRESHOLD}% left · 5h 0% left\n"
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


# ── F836 r4 (#693) — the r3 residual (S1): a verbatim status bar typed/pasted as
# the line DIRECTLY adjacent to the live composer (no blank separator) is a
# transcript paste, not the live bar. INVARIANT: the live TUI redraws
# bar + blank + composer — every real captured pane in the corpus carries that
# blank separator; a FLUSH footer-shaped row is transcript and must not fire.
# (See _codex_footer_percent_row / _kiro_footer_percent_row.) ──────────────────

# The composer literals in their exact live-pane forms (match the r3 attachment).
_KC = " ask a question or describe a task ↵"
_CC = "› Ask Codex to do anything"


def test_r4_kiro_adjacent_quote_flush_does_not_fire() -> None:
    """S1 residual (kiro): a quoted ``● 92%`` bar on the row DIRECTLY above the
    composer, nothing between, is a pasted status line — NOT the live bar — and
    must be quiet. (On r3 HEAD this fired a false high-confidence exhaustion.)"""
    pane = (
        "You asked about the pane. The last capture was:\n"
        "kiro_cli_dev · Auto · ● 92%    /repo · (cao/x)\n" + _KC + "\n"
    )
    assert classify_condition(pane, "kiro_cli") is None


def test_r4_codex_adjacent_below_flush_does_not_fire() -> None:
    """S1 residual (codex twin): a footer-shaped row DIRECTLY below the composer,
    nothing between, is only reachable in a pasted pane (a live codex composer is
    bottom-most) — so a FLUSH footer below the composer must be quiet."""
    pane = _CC + "\n~/x · main · gpt-5.6-sol high · Context 8% left · 5h 47% left\n"
    assert classify_condition(pane, "codex") is None


def test_r4_kiro_quote_separated_by_prose_is_quiet() -> None:
    """Control (unchanged from r3): one prose row between the quote and the
    composer -> correctly quiet (separated quotes were already inert)."""
    pane = (
        "The last capture was: kiro_cli_dev · Auto · ● 92%\n"
        "Let me know if you want me to recover that seat.\n" + _KC + "\n"
    )
    assert classify_condition(pane, "kiro_cli") is None


def test_r4_codex_reads_last_composer_not_first() -> None:
    """S2 (M1 gap): two codex composers; the newest has a healthy footer below,
    the older an exhausted footer below. Reading the LAST composer -> quiet.
    Pins last-composer selection (mutant M1 'pick first composer' must die)."""
    pane = (
        _CC + "\n\n"
        "~/x · main · gpt-5.6-sol high · Context 3% left · 5h 9% left\n"
        "  ... later ...\n" + _CC + "\n\n"
        "~/x · main · gpt-5.6-sol high · Context 77% left · 5h 47% left\n"
    )
    assert classify_condition(pane, "codex") is None


def test_r4_codex_reads_adjacent_row_not_any_row_in_region() -> None:
    """S3 (M6 gap): prose row directly below the composer, exhausted footer two
    rows down. Reading the ADJACENT row only -> quiet. Pins adjacent-only, not
    any-row (mutant M6 'accept any footer-shaped row below the composer' dies)."""
    pane = (
        _CC + "\n\n"
        "  some assistant prose here\n"
        "~/x · main · gpt-5.6-sol high · Context 8% left · 5h 47% left\n"
    )
    assert classify_condition(pane, "codex") is None


# ── F836 r5 (#693) — close the codex EMPIRICAL-GATE-NO on r3+r4. The r3/r4
# position rule located the live composer by an UNANCHORED substring search
# (_last_index over the raw pane) plus a blank-gap adjacency check. A quoted FULL
# snapshot (composer + blank + footer) — fenced, indented, or split across prose
# rows — reproduces that exact structure, so the selector promoted quoted
# transcript to live chrome and fired a high-confidence hard stop (5 fresh
# adversarial panes in the codex verdict). The r5 fix is structural, no content
# denylist:
#   1. EXACT full-row composer anchors (_CODEX_COMPOSER_ROW / _KIRO_COMPOSER_ROW):
#      the composer must match the provider's composer line as a WHOLE row
#      (optional '›' glyph, bounded leading whitespace, exact phrase, optional
#      trailing chrome) — never a substring in prose, never a fenced/indented row.
#   2. Bottom-of-viewport invariant: the live composer sits within the last
#      non-blank rows of the pane (measured max 1 non-blank row below across all
#      22 real captures; bound K=2), and for codex the footer is the LAST
#      non-blank row of the pane. A composer occurrence with transcript chrome
#      (a closing ``` fence, more rows) below it is scrollback.
# Cursor/styling metadata is NOT available to the classifier this round (the pane
# arrives as a plain `tmux capture-pane -p -e` string with no cursor row — see the
# report's "second live signal" finding), so the bottom-of-viewport structural
# invariant IS the second independent signal. ─────────────────────────────────

# The FIVE codex-verdict adversarial panes (verbatim substance). Each MUST be
# quiet: a fenced/indented/prose quote of a FULL snapshot is transcript, and its
# "composer" fails the whole-row anchor and/or the bottom-of-viewport bound.
_R5_VERDICT_NEGATIVE = [
    (
        "fenced-full-codex-snapshot",
        "codex",
        "• Here is my full terminal:\n"
        "```text\n"
        "› Ask Codex to do anything\n"
        "\n"
        "  ~/x · main · gpt-5.6-sol high · Context 8% left · 5h 47% left\n"
        "```\n",
    ),
    (
        "fenced-full-kiro-snapshot",
        "kiro_cli",
        "• Here is my full terminal:\n"
        "```text\n"
        "kiro_cli_dev · Auto · ● 92%    /path · (branch)\n"
        "\n"
        " ask a question or describe a task ↵\n"
        "```\n",
    ),
    (
        "indented-full-kiro-snapshot",
        "kiro_cli",
        "• Here is my full terminal:\n"
        "    kiro_cli_dev · Auto · ● 92%    /path · (branch)\n"
        "\n"
        "     ask a question or describe a task ↵\n",
    ),
    (
        "prose-codex-composer-phrase-plus-quoted-bar",
        "codex",
        "• I typed into the row that says Ask Codex to do anything\n"
        "\n"
        "  and the bar read ~/x · main · gpt-5.6-sol high · Context 8% left · 5h 47% left\n",
    ),
    (
        "prose-kiro-composer-phrase-below-quoted-bar",
        "kiro_cli",
        "• the bar read kiro_cli_dev · Auto · ● 92%    /path · (branch)\n"
        "\n"
        "• then I clicked the ask a question or describe a task row\n",
    ),
]


@pytest.mark.parametrize("label,provider,pane", _R5_VERDICT_NEGATIVE)
def test_r5_full_snapshot_quotes_do_not_fire(label, provider, pane) -> None:
    """The codex EMPIRICAL-GATE-NO blocker: a quoted FULL snapshot (composer +
    blank + footer) must NOT be read as live chrome. On r3/r4 HEAD each of these
    fired CONTEXT_EXHAUSTED.footer_percent_status/high."""
    assert classify_condition(pane, provider) is None, f"{label}: quoted snapshot fired"


# The REALISTIC variant of each negative: the same quoted snapshot ABOVE, plus a
# genuine live composer + exhausted footer at the bottom of the pane. The live
# rows MUST fire exactly once, high confidence, from the live chrome — the quote
# above stays inert. (Anchoring must not cost genuine below/above reachability.)
def test_r5_codex_quote_then_live_exhausted_fires_once() -> None:
    pane = (
        "• Earlier my bar said:\n"
        "```text\n"
        "› Ask Codex to do anything\n"
        "\n"
        "  ~/x · main · gpt-5.6-sol high · Context 3% left · 5h 9% left\n"
        "```\n"
        "\n"
        "• back to work.\n"
        "\n"
        "› Ask Codex to do anything\n"
        "\n"
        "  ~/x · main · gpt-5.6-sol high · Context 6% left · 5h 47% left\n"
    )
    c = classify_condition(pane, "codex")
    assert c is not None and c.kind is ConditionKind.CONTEXT_EXHAUSTED
    assert c.subtype == "footer_percent_status" and c.confidence.value == "high"


def test_r5_kiro_quote_then_live_exhausted_fires_once() -> None:
    pane = (
        "• Earlier my bar said:\n"
        "```text\n"
        "kiro_cli_dev · Auto · ● 92%    /path · (branch)\n"
        "\n"
        " ask a question or describe a task ↵\n"
        "```\n"
        "\n"
        "• back to work.\n"
        "\n"
        "kiro_cli_dev · Auto · ● 93%                    /data/x · (cao/x)\n"
        "\n"
        " ask a question or describe a task ↵\n"
    )
    k = classify_condition(pane, "kiro_cli")
    assert k is not None and k.kind is ConditionKind.CONTEXT_EXHAUSTED
    assert k.subtype == "footer_percent_status" and k.confidence.value == "high"


# ── SHOULD (codex verdict): a mid-redraw capture that transiently omits the
# blank separator / footer must NOT produce a false CONTEXT_EXHAUSTED — a missed
# frame is recovered on the next poll. ────────────────────────────────────────
def test_r5_codex_midredraw_no_footer_is_not_exhausted() -> None:
    """A capture taken mid-redraw: the composer is drawn but the footer row has
    not been redrawn yet (no non-blank row below the composer). No live footer to
    read → NOT CONTEXT_EXHAUSTED (a missed frame is recovered on the next poll)."""
    pane = "  done.\n\n› Ask Codex to do anything\n\n"
    c = classify_condition(pane, "codex")
    assert c is None or c.kind is not ConditionKind.CONTEXT_EXHAUSTED


def test_r5_kiro_midredraw_no_footer_is_not_exhausted() -> None:
    """kiro twin: the composer is present but the status bar above it has not been
    redrawn (only prose above) → no live footer → not exhausted."""
    pane = "  still thinking about the change.\n\n ask a question or describe a task ↵\n"
    assert classify_condition(pane, "kiro_cli") is None


# ── r5 structural unit assertions (pin the anchors + bottom-of-viewport bound
# directly, independent of the full classify path). ───────────────────────────
def test_r5_composer_anchor_rejects_fenced_and_indented_rows() -> None:
    from cli_agent_orchestrator.providers.condition import (
        _CODEX_COMPOSER_ROW,
        _KIRO_COMPOSER_ROW,
    )

    # Genuine composer rows match.
    assert _CODEX_COMPOSER_ROW.match("› Ask Codex to do anything")
    assert _KIRO_COMPOSER_ROW.match(" ask a question or describe a task ↵")
    assert _KIRO_COMPOSER_ROW.match("›  ask a question or describe a task ↵")
    # A bullet/prose row that merely CONTAINS the phrase does not.
    assert not _CODEX_COMPOSER_ROW.match("• row that says Ask Codex to do anything")
    assert not _KIRO_COMPOSER_ROW.match("• clicked the ask a question or describe a task row")
    # An indented (>=2 leading spaces) paste of the kiro phrase does not anchor.
    assert not _KIRO_COMPOSER_ROW.match("     ask a question or describe a task ↵")


def test_r5_live_composer_index_enforces_bottom_of_viewport() -> None:
    from cli_agent_orchestrator.providers.condition import (
        _CODEX_COMPOSER_ROW,
        _live_composer_index,
    )

    # Composer at the bottom (0-1 non-blank rows below) is live.
    rows = ["• x", "", "› Ask Codex to do anything", ""]
    assert _live_composer_index(rows, _CODEX_COMPOSER_ROW) == 2
    # Composer with a closing ``` fence + footer below it is a quoted snapshot.
    rows2 = ["```text", "› Ask Codex to do anything", "", "  Context 8% left · 5h 9% left", "```"]
    assert _live_composer_index(rows2, _CODEX_COMPOSER_ROW) == -1


def test_r5_composer_with_transcript_below_beyond_k_is_not_live() -> None:
    """Isolates the bottom-of-viewport K bound (mutant: drop the K bound). A
    clean-anchored kiro composer with a footer-shaped row above it (blank gap) but
    THREE plain non-lead transcript rows BELOW it is not bottom chrome: real
    captures never put >1 non-blank row below the composer, and the kiro
    footer-above path has no footer-is-bottom guard to fall back on. Must be
    quiet. Without the K bound this fires a false CONTEXT_EXHAUSTED off the 92%."""
    pane = (
        "kiro_cli_dev · Auto · ● 92%    /path · (branch)\n"
        "\n"
        " ask a question or describe a task ↵\n"
        "  trailing one\n"
        "  trailing two\n"
        "  trailing three\n"
    )
    assert classify_condition(pane, "kiro_cli") is None


def test_r5_codex_footer_not_bottom_row_is_not_live() -> None:
    """Isolates the codex footer-is-bottom-chrome direction rule (mutant: drop the
    ``_nonblank_below(footer) == 0`` guard). The footer sits adjacent below the
    composer with the blank gap, but a plain (non-lead) transcript row follows it,
    so the footer is NOT the last non-blank row of the pane → a quoted snapshot,
    not the live bar. Must be quiet; without the guard it fires."""
    pane = (
        "› Ask Codex to do anything\n"
        "\n"
        "  ~/x · main · gpt-5.6-sol high · Context 8% left · 5h 47% left\n"
        "  trailing plain line\n"
    )
    assert classify_condition(pane, "codex") is None
