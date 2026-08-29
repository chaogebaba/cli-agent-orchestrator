"""F597 #454 B2: shipped-rule corpus regression guard.

The gate found that the single-domain canonicalize() regressed three enabled
shipped regex rules — askuserquestion-fork-prompt (glyph anchors ☐|☑|✔),
codex-ratelimit-model-switch (needs `?`), codex-update-available (needs `!`,
version dots, `->`) — which matched raw at base but failed once canonicalize()
stripped the punctuation/glyphs their regexes need. The fix (two domains:
contains→full canonical, regex→LIGHT canonical with punctuation preserved) must
NOT rewrite the shipped rules.

This test loads EVERY enabled rule from the live
`~/.aws/cli-agent-orchestrator/auto-answers/*.yaml` files READ-ONLY and asserts
each still matches a representative rendered sample under the shipped matcher
(via dialog_region, exactly as production evaluates a screen). Samples live in
`test/fixtures/auto_answers_samples/<rule-name>.txt`.

If this file is run in an environment without the user's auto-answers yaml (e.g.
a clean CI checkout), it skips — the seed rules are covered by
test_auto_responder_seed_rules.py and test_f597_canonical_matcher.py.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import pytest
import yaml

from cli_agent_orchestrator.services import auto_responder as ar

AUTO_ANSWERS_DIR = Path(os.path.expanduser("~/.aws/cli-agent-orchestrator/auto-answers"))
SAMPLES_DIR = Path(__file__).parents[1] / "fixtures" / "auto_answers_samples"


def _enabled_rules() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for path in sorted(glob.glob(str(AUTO_ANSWERS_DIR / "*.yaml"))):
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
        except Exception:
            continue
        for item in raw:
            if isinstance(item, dict) and item.get("enabled", True):
                out.append((os.path.basename(path), item))
    return out


_RULES = _enabled_rules()


@pytest.mark.skipif(not _RULES, reason="no live auto-answers yaml present (clean checkout)")
@pytest.mark.parametrize(
    "rule_item",
    [pytest.param(item, id=item["name"]) for _f, item in _RULES],
)
def test_enabled_shipped_rule_matches_its_sample(rule_item: dict) -> None:
    """Each enabled shipped rule must still match a representative rendered
    screen under the two-domain canonical matcher (F597 #454 B2)."""
    sample = SAMPLES_DIR / f"{rule_item['name']}.txt"
    assert sample.exists(), (
        f"missing representative sample for enabled rule {rule_item['name']!r}: " f"add {sample}"
    )
    rule = ar.Rule(
        name=rule_item["name"],
        enabled=True,
        match_mode=rule_item.get("match_mode", "contains"),
        question=rule_item["question"],
        options=list(rule_item.get("options", []) or []),
        answer=rule_item.get("answer", "wait"),
    )
    lines = sample.read_text(encoding="utf-8").splitlines()
    region = ar.dialog_region(lines)
    assert rule.matches(region), (
        f"rule {rule_item['name']!r} ({rule.match_mode}) failed to match its sample; "
        f"reject={rule.reject_reason(region)!r}\n"
        f"full={region.normalized!r}\nlight={region.normalized_light!r}"
    )


@pytest.mark.skipif(not _RULES, reason="no live auto-answers yaml present (clean checkout)")
def test_every_enabled_rule_has_a_sample() -> None:
    """No enabled shipped rule may lack a sample (else a regression could hide)."""
    missing = [
        item["name"] for _f, item in _RULES if not (SAMPLES_DIR / f"{item['name']}.txt").exists()
    ]
    assert not missing, f"enabled rules without a sample fixture: {missing}"


@pytest.mark.skipif(not _RULES, reason="no live auto-answers yaml present (clean checkout)")
def test_regressed_regex_rules_present_and_match():
    """Explicit guard for the exact three rules the gate flagged: they use regex
    with punctuation/glyph anchors and MUST match under the light domain."""
    by_name = {item["name"]: item for _f, item in _RULES}
    for name in (
        "askuserquestion-fork-prompt",
        "codex-ratelimit-model-switch",
        "codex-update-available",
    ):
        if name not in by_name:
            pytest.skip(f"{name} not present in this environment's yaml")
        item = by_name[name]
        rule = ar.Rule(
            name,
            True,
            item.get("match_mode", "contains"),
            item["question"],
            list(item.get("options", []) or []),
            item.get("answer", "wait"),
        )
        region = ar.dialog_region((SAMPLES_DIR / f"{name}.txt").read_text().splitlines())
        assert rule.matches(region), f"{name} regressed: {rule.reject_reason(region)!r}"
