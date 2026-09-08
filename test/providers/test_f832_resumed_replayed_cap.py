"""F832 (#689) — CAPPED must ignore a cap line replayed from a resumed transcript.

On `codex resume <uuid>` (e.g. after an account swap) codex reprints the PRIOR
conversation, including an OLD "You've hit your usage limit" line, ABOVE the
"Resuming session…" boot marker. The pre-fix CAPPED classifier fired on that
replayed line — a false CAPPED on a terminal that is in fact logged in and
working.

The fix scopes the codex CAPPED scan to rows AFTER the resume boot marker. A cap
line above the marker is replayed history (no condition); a cap line below it
(the current incarnation genuinely hit the cap) still classifies CAPPED. A fresh,
never-resumed pane has no marker and is scanned whole (normal behaviour).
"""

from __future__ import annotations

from pathlib import Path

from cli_agent_orchestrator.providers.condition import ConditionKind, classify_condition

_COND = Path(__file__).parent / "fixtures" / "conditions"


def _load(name: str) -> str:
    return (_COND / f"{name}.txt").read_text(encoding="utf-8")


def test_replayed_cap_above_resume_marker_is_ignored() -> None:
    """The #689 bite: replayed cap ABOVE 'Resuming session…' → NO condition."""
    cond = classify_condition(_load("codex-resumed-replayed-cap-1"), "codex")
    assert cond is None, f"replayed cap wrongly classified: {cond!r}"


def test_live_cap_below_resume_marker_still_capped() -> None:
    """The counterpart: a cap BELOW the resume marker is the current
    incarnation's — must still classify CAPPED."""
    cond = classify_condition(_load("codex-resumed-live-cap-1"), "codex")
    assert cond is not None and cond.kind is ConditionKind.CAPPED
    assert cond.subtype == "usage_limit_hard"


def test_fresh_never_resumed_cap_still_capped() -> None:
    """No resume marker → whole-buffer scan (unchanged) → CAPPED. Guards against
    the fix over-scoping a normal, never-resumed capped pane."""
    fresh = "■ You've hit your usage limit. Try again at 4:39 AM.\n\n› Ask Codex to do anything\n"
    cond = classify_condition(fresh, "codex")
    assert cond is not None and cond.kind is ConditionKind.CAPPED


def test_existing_capped_fixtures_unaffected() -> None:
    """The real codex-capped-* corpus (no resume marker) still classifies CAPPED
    — the fix must not disturb the normal layout where the cap banner sits above
    the always-bottom composer prompt."""
    for name in ("codex-capped-1", "codex-capped-2"):
        cond = classify_condition(_load(name), "codex")
        assert cond is not None and cond.kind is ConditionKind.CAPPED, name
