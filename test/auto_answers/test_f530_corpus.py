"""F530 (#386) replayable repro corpus — auto-responder no-fire panes.

Each fixture pair under ``fixtures/f530/`` is a REAL captured pane at the
moment the auto-responder reported "no rule matched" (issue #386), plus a
sidecar ``.yaml`` naming the rule that should have fired:

- ``NN-<slug>.txt``   — verbatim rendered pane rows (one screen row per line),
  extracted from the incident terminal log at the byte prefix where the
  dialog is fully drawn and the startup chrome has settled (pyte render at
  the pane geometry; provenance in each sidecar).
- ``NN-<slug>.yaml``  — ``expected_rule`` (must exist in ``rules.yaml``),
  provenance (issue occurrence, terminal id, timestamp), and ``xfail`` flag.
- ``rules.yaml``      — byte-identical snapshot of the live
  ``~/.aws/cli-agent-orchestrator/auto-answers/codex.yaml`` at corpus
  authoring time, so the corpus replays hermetically against the REAL rules.

The test runs the REAL matcher pipeline slice that decides rule matching in
``AutoResponder._on_screen``: ``dialog_region(lines, codex chrome patterns)``
then ``Rule.matches(region.normalized)`` — exactly the code path behind
``no rule matched``. No keys are sent; nothing is mutated.

Cases that still fail on current main carry ``xfail: true`` in their sidecar
(rendered as ``xfail(strict=True, reason="F530 #386")``) so the suite stays
green while pinning the defect: when the fix lands, strict-xfail flips them
to XPASS and the suite goes red until the flags are removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.services.auto_responder import _RuleStore, dialog_region

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "f530"
RULES_FILE = FIXTURE_DIR / "rules.yaml"
XFAIL_REASON = "F530 #386"


def _corpus_cases() -> list:
    """Collect (pane_file, expected_rule) params from sidecars; xfail per sidecar."""
    cases = []
    for pane_file in sorted(FIXTURE_DIR.glob("[0-9]*-*.txt")):
        sidecar = pane_file.with_suffix(".yaml")
        meta = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        marks = []
        if meta.get("xfail"):
            marks.append(pytest.mark.xfail(strict=True, reason=XFAIL_REASON))
        cases.append(
            pytest.param(pane_file, meta["expected_rule"], id=pane_file.stem, marks=marks)
        )
    assert cases, "F530 corpus fixture dir is empty — fixtures were not committed"
    return cases


@pytest.fixture(scope="module")
def corpus_rules():
    """Real rules, loaded from the hermetic snapshot via the real loader."""
    rules = _RuleStore._load(RULES_FILE)
    assert rules, "rules.yaml snapshot failed to parse into any Rule"
    return {rule.name: rule for rule in rules}


@pytest.mark.parametrize(("pane_file", "expected_rule"), _corpus_cases())
def test_f530_corpus_expected_rule_matches(pane_file, expected_rule, corpus_rules):
    """The #386-no-fire pane must match the rule that should have fired.

    Replay: real rules snapshot + real codex chrome patterns + real
    dialog_region/Rule.matches — the exact matching path whose failure
    produces the ``no rule matched`` push.
    """
    rule = corpus_rules.get(expected_rule)
    assert rule is not None, (
        f"sidecar expected_rule {expected_rule!r} is absent from rules.yaml — "
        "corpus and rules snapshot have drifted apart"
    )
    lines = pane_file.read_text(encoding="utf-8", errors="replace").splitlines()
    region = dialog_region(lines, list(CodexProvider._CHROME_ROW_PATTERNS))
    assert rule.matches(region.normalized), (
        f"{pane_file.name}: rule {expected_rule!r} does not match the captured "
        f"pane (reject_reason={rule.reject_reason(region.normalized)!r}); "
        f"match region normalized tail: {region.normalized[-300:]!r}"
    )


def test_f530_corpus_sidecars_are_wellformed(corpus_rules):
    """Every sidecar names a rule that exists in the snapshot and carries provenance."""
    for sidecar in sorted(FIXTURE_DIR.glob("[0-9]*-*.yaml")):
        meta = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        assert meta["expected_rule"] in corpus_rules, sidecar.name
        assert meta["occurrence"], sidecar.name
        assert meta["terminal"], sidecar.name
        assert isinstance(meta.get("xfail", False), bool), sidecar.name
