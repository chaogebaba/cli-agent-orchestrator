"""F530 (#386) replayable repro corpus — auto-responder no-fire panes.

Each fixture pair under ``fixtures/f530/`` is a REAL composited pane frame
captured at (or reconstructed byte-for-byte from) a moment the auto-responder
failed to fire on issue #386, plus a sidecar ``.yaml`` naming the rule that
should have fired:

- ``NN-<slug>.txt``   — one rendered screen row per line. Each frame is the
  pyte composite (``pyte.Screen.display``) of the terminal's raw pipe-pane
  byte log at ``~/.aws/cli-agent-orchestrator/logs/terminal/<terminal>.log``,
  rendered at cols=80 — i.e. the SAME kind of composited screen the runtime
  status monitor feeds to the auto-responder (see below). Trailing per-row
  padding is stripped for readability; the matcher collapses all whitespace
  runs (``normalize_screen``) so this is behaviour-preserving. Raw pane BYTES
  were never committed to git for #386 (the referenced
  ``probes/error-pane-samples/f530-rule-nomatch/pane.txt`` does not exist), so
  provenance is the issue-comment URL in each sidecar plus the terminal log.
- ``NN-<slug>.yaml``  — ``expected_rule`` (must exist in ``rules.yaml``),
  ``occurrence``/``terminal``/``provenance`` (the #386 comment URL), and an
  ``xfail`` flag (see below).
- ``rules.yaml``      — byte-identical snapshot of the live
  ``~/.aws/cli-agent-orchestrator/auto-answers/codex.yaml`` at corpus authoring
  time, so the corpus replays hermetically against the REAL rules.

WHY THE FULL PIPELINE, NOT ``Rule.matches`` ALONE
-------------------------------------------------
The #386 symptom is "the rule is textually present yet the matcher reports no
rule matched". That is NOT decided by ``Rule.matches`` alone: at runtime
``StatusMonitor._detect_screen_with_trust`` renders the pipe-pane byte stream
through pyte and hands ``pyte.Screen.display`` to ``AutoResponder.on_screen``,
which (in ``_on_screen``) does, per eval:

    region       = dialog_region(lines)                       # classifier tail
    match_region = dialog_region(lines, provider.chrome_...)  # rule-match tail
    status       = provider.get_status_from_screen(region.rows)
    for rule in rules:
        if rule.matches(match_region.normalized):
            if status is PROCESSING:  -> busy-veto (no fire)
            elif status is WAITING:   -> fire

So whether the whitelisted rule FIRES is the conjunction of (a) the rule
matching the CHROME-FILTERED region and (b) the frame classifying
``WAITING_USER_ANSWER`` (not ``PROCESSING``). This test replays exactly that
conjunction — ``would_fire`` below — against the real frames. It sends no keys
and mutates no state (``_fire`` and the threaded verify/retry are never
reached).

XFAIL SEMANTICS
---------------
A case whose real frame does NOT fire on the current tree carries ``xfail:
true`` (rendered ``xfail(strict=True, reason="F530 #386")``): the corpus stays
green while pinning the live defect, and the eventual fix flips it to XPASS
(strict) → red until the flag is removed. Cases that already fire are plain
PASS regression guards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.services.auto_responder import (
    Rule,
    _RuleStore,
    dialog_region,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "f530"
RULES_FILE = FIXTURE_DIR / "rules.yaml"
XFAIL_REASON = "F530 #386"


def _corpus_cases() -> list[Any]:
    """Collect (pane_file, expected_rule) params from sidecars; xfail per sidecar."""
    cases: list[Any] = []
    for pane_file in sorted(FIXTURE_DIR.glob("[0-9]*-*.txt")):
        sidecar = pane_file.with_suffix(".yaml")
        meta = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        marks = []
        if meta.get("xfail"):
            marks.append(pytest.mark.xfail(strict=True, reason=XFAIL_REASON))
        cases.append(pytest.param(pane_file, meta["expected_rule"], id=pane_file.stem, marks=marks))
    assert cases, "F530 corpus fixture dir is empty — fixtures were not committed"
    return cases


def _would_fire(rule: Rule, lines: list[str]) -> tuple[bool, str]:
    """Replay the auto-responder fire decision for ``rule`` on a composited frame.

    Mirrors ``AutoResponder._on_screen``'s per-eval decision for a STATIC frame:
    the rule must match the CHROME-FILTERED region AND the frame must classify
    ``WAITING_USER_ANSWER`` (a ``PROCESSING`` classification busy-vetoes a
    matched rule). Returns ``(would_fire, diagnostic)``. No keys, no state.
    """
    chrome = list(CodexProvider._CHROME_ROW_PATTERNS)
    region = dialog_region(lines)  # unfiltered tail — what the classifier sees
    match_region = dialog_region(lines, chrome)  # chrome-filtered — rule matching

    # Real classifier: construct a provider without running __init__ (which
    # shells out to tmux); emit_screen_signals only needs _initialized.
    provider = CodexProvider.__new__(CodexProvider)
    provider._initialized = True
    status = provider.get_status_from_screen(list(region.rows))

    matched = rule.matches(match_region.normalized)
    busy_vetoed = status == TerminalStatus.PROCESSING
    would_fire = matched and status == TerminalStatus.WAITING_USER_ANSWER and not busy_vetoed
    diag = (
        f"matched={matched} reject={rule.reject_reason(match_region.normalized)!r} "
        f"status={status} busy_veto={busy_vetoed}; "
        f"match-region tail={match_region.normalized[-260:]!r}"
    )
    return would_fire, diag


@pytest.fixture(scope="module")
def corpus_rules() -> dict[str, Rule]:
    """Real rules, loaded from the hermetic snapshot via the real loader."""
    rules = _RuleStore._load(RULES_FILE)
    assert rules, "rules.yaml snapshot failed to parse into any Rule"
    return {rule.name: rule for rule in rules}


@pytest.mark.parametrize(("pane_file", "expected_rule"), _corpus_cases())
def test_f530_corpus_expected_rule_fires(
    pane_file: Path, expected_rule: str, corpus_rules: dict[str, Rule]
) -> None:
    """The #386 pane must FIRE the rule that should have fired.

    Replays the real auto-responder fire decision (rule match on the
    chrome-filtered region AND a WAITING classification, not a PROCESSING
    busy-veto) — the exact conjunction behind the ``no rule matched`` push.
    xfail cases are frames that still do not fire on the current tree.
    """
    rule = corpus_rules.get(expected_rule)
    assert rule is not None, (
        f"sidecar expected_rule {expected_rule!r} is absent from rules.yaml — "
        "corpus and rules snapshot have drifted apart"
    )
    lines = pane_file.read_text(encoding="utf-8", errors="replace").splitlines()
    fired, diag = _would_fire(rule, lines)
    assert fired, f"{pane_file.name}: rule {expected_rule!r} would NOT fire — {diag}"


def test_f530_corpus_sidecars_are_wellformed(corpus_rules: dict[str, Rule]) -> None:
    """Every sidecar names a rule that exists in the snapshot and carries provenance."""
    for sidecar in sorted(FIXTURE_DIR.glob("[0-9]*-*.yaml")):
        meta = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        assert meta["expected_rule"] in corpus_rules, sidecar.name
        assert meta["occurrence"], sidecar.name
        assert meta["terminal"], sidecar.name
        assert meta["provenance"], sidecar.name
        assert isinstance(meta.get("xfail", False), bool), sidecar.name
