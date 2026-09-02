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
from typing import Any

import pytest
import yaml

from cli_agent_orchestrator.services import auto_responder as ar

# F704 #559: overridable so the nameless-rule handling can be tested against a
# scratch fixture dir (must be set before pytest collects, _RULES is module-level).
AUTO_ANSWERS_DIR = Path(
    os.environ.get(
        "F597_AUTO_ANSWERS_DIR",
        os.path.expanduser("~/.aws/cli-agent-orchestrator/auto-answers"),
    )
)
SAMPLES_DIR = Path(__file__).parents[1] / "fixtures" / "auto_answers_samples"


def _rule_name(item: dict[str, Any]) -> str:
    name = item.get("name")
    return name if isinstance(name, str) else ""


def _rule_id(fname: str, idx: int, item: dict[str, Any]) -> str:
    """Parametrize id: the rule's name, or ``<file>:rule<N>`` for a nameless
    rule so a single bad entry cannot KeyError the whole corpus away."""
    return _rule_name(item) or f"{fname}:rule{idx}"


def _enabled_rules() -> list[tuple[str, int, dict[str, Any]]]:
    """(file basename, 0-based index within the file, rule) for every enabled rule."""
    out: list[tuple[str, int, dict[str, Any]]] = []
    for path in sorted(glob.glob(str(AUTO_ANSWERS_DIR / "*.yaml"))):
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
        except Exception:
            continue
        for idx, item in enumerate(raw):
            if isinstance(item, dict) and item.get("enabled", True):
                out.append((os.path.basename(path), idx, item))
    return out


_RULES = _enabled_rules()


@pytest.mark.skipif(not _RULES, reason="no live auto-answers yaml present (clean checkout)")
def test_every_rule_is_named() -> None:
    """Every enabled shipped rule must carry a non-empty ``name`` — a nameless
    rule is unmatchable (production keys rules by name) and breaks the corpus
    ids, so it must fail HERE as one clear listing, not a collection KeyError."""
    nameless = [(fname, idx, item) for fname, idx, item in _RULES if not _rule_name(item).strip()]
    assert not nameless, "every enabled rule must have a non-empty 'name'; offenders: " + "; ".join(
        f"{fname}:rule{idx} -> {str(item)[:60]}" for fname, idx, item in nameless
    )


@pytest.mark.skipif(not _RULES, reason="no live auto-answers yaml present (clean checkout)")
@pytest.mark.parametrize(
    "fname_and_rule",
    [
        pytest.param(
            (fname, item),
            id=_rule_id(fname, idx, item),
            marks=pytest.mark.skipif(
                not _rule_name(item).strip(),
                reason="nameless rule (failed test_every_rule_is_named; cannot key a sample)",
            ),
        )
        for fname, idx, item in _RULES
    ],
)
def test_enabled_shipped_rule_matches_its_sample(
    fname_and_rule: tuple[str, dict[str, Any]],
) -> None:
    """Each enabled shipped rule must still match a representative rendered
    screen under the two-domain canonical matcher (F597 #454 B2)."""
    fname, rule_item = fname_and_rule
    name = _rule_name(rule_item)
    assert name, f"nameless rule in {fname} (see test_every_rule_is_named)"
    sample = SAMPLES_DIR / f"{name}.txt"
    assert sample.exists(), (
        f"missing representative sample for enabled rule {name!r}: " f"add {sample}"
    )
    rule = ar.Rule(
        name=name,
        enabled=True,
        match_mode=rule_item.get("match_mode", "contains"),
        question=rule_item["question"],
        options=list(rule_item.get("options", []) or []),
        answer=rule_item.get("answer", "wait"),
    )
    lines = sample.read_text(encoding="utf-8").splitlines()
    region = ar.dialog_region(lines)
    assert rule.matches(region), (
        f"rule {name!r} ({rule.match_mode}) failed to match its sample; "
        f"reject={rule.reject_reason(region)!r}\n"
        f"full={region.normalized!r}\nlight={region.normalized_light!r}"
    )


@pytest.mark.skipif(not _RULES, reason="no live auto-answers yaml present (clean checkout)")
def test_every_enabled_rule_has_a_sample() -> None:
    """No enabled shipped rule may lack a sample (else a regression could hide)."""
    missing = [
        _rule_name(item)
        for _f, _i, item in _RULES
        if _rule_name(item) and not (SAMPLES_DIR / f"{_rule_name(item)}.txt").exists()
    ]
    assert not missing, f"enabled rules without a sample fixture: {missing}"


@pytest.mark.skipif(not _RULES, reason="no live auto-answers yaml present (clean checkout)")
def test_regressed_regex_rules_present_and_match() -> None:
    """Explicit guard for the exact three rules the gate flagged: they use regex
    with punctuation/glyph anchors and MUST match under the light domain."""
    by_name = {_rule_name(item): item for _f, _i, item in _RULES if _rule_name(item)}
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
