"""F597 #454 B2: shipped-rule corpus regression guard.

The gate found that the single-domain canonicalize() regressed three enabled
shipped regex rules — askuserquestion-fork-prompt (glyph anchors ☐|☑|✔),
codex-ratelimit-model-switch (needs `?`), codex-update-available (needs `!`,
version dots, `->`) — which matched raw at base but failed once canonicalize()
stripped the punctuation/glyphs their regexes need. The fix (two domains:
contains→full canonical, regex→LIGHT canonical with punctuation preserved) must
NOT rewrite the shipped rules.

The corpus is REPO-VERSIONED (F932 #784). Every rule is read from
``test/fixtures/auto_answers_corpus/<provider>.yaml`` and asserted to still match
its rendered sample under ``test/fixtures/auto_answers_samples/`` via
``dialog_region``, exactly as production evaluates a screen.

Why versioned, and not read from the live yaml as this file used to do: the
``~/.aws/cli-agent-orchestrator/auto-answers/*.yaml`` files are OPERATOR STATE.
``cao install`` seeds them and a human edits them afterwards; nothing versions
them in either repo. Enumerating them made the suite's colour a property of the
host — the same commit was green on grok-box-003, red on grok-box-009
(``codex-hooks-list-close``) and red on a third set on the laptop — so a
"new vs pre-existing failure" tally could not be compared across machines at
all. Every assertion here is now repo-vs-repo and reads the same on any host.

The live directory has exactly one remaining role:
``test_live_rule_drift_is_reported_not_asserted`` REPORTS rules installed on
this machine that the corpus does not cover. It warns; it never fails. The
enforceable half of that intent — no SHIPPED rule may lack a sample — is
hermetic and lives in ``test_seed_rule_is_in_the_corpus_and_matches_its_sample``,
which reads ``auto_responder.SEED_RULES``, the bytes CAO actually ships.
"""

from __future__ import annotations

import glob
import os
import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml

from cli_agent_orchestrator.services import auto_responder as ar

# F704 #559: overridable so the nameless-rule handling can be tested against a
# scratch fixture dir (must be set before pytest collects, _LIVE is module-level).
AUTO_ANSWERS_DIR = Path(
    os.environ.get(
        "F597_AUTO_ANSWERS_DIR",
        os.path.expanduser("~/.aws/cli-agent-orchestrator/auto-answers"),
    )
)
SAMPLES_DIR = Path(__file__).parents[1] / "fixtures" / "auto_answers_samples"
CORPUS_DIR = Path(__file__).parents[1] / "fixtures" / "auto_answers_corpus"

#: Not a rule file. It is a rule-name -> [extra sample filenames] mapping, so the
#: corpus loader must skip it by name; left in the same directory because it is
#: part of the corpus's definition and drifts with it.
EXTRA_SAMPLES_FILE = "extra-samples.yaml"


def _rule_name(item: dict[str, Any]) -> str:
    name = item.get("name")
    return name if isinstance(name, str) else ""


def _rule_id(fname: str, idx: int, item: dict[str, Any]) -> str:
    """Parametrize id: the rule's name, or ``<file>:rule<N>`` for a nameless
    rule so a single bad entry cannot KeyError the whole corpus away."""
    if _is_malformed_doc(item):
        return f"{fname}:doc"
    return _rule_name(item) or f"{fname}:rule{idx}"


def _is_malformed_doc(item: dict[str, Any]) -> bool:
    return "__malformed_document__" in item


def _load_rules(
    rules_dir: Path | str | None = None, *, enabled_only: bool = True
) -> list[tuple[str, int, dict[str, Any]]]:
    """(file basename, 0-based index within the file, rule) for every rule.

    Collection must NEVER raise, whatever the yaml decodes to: a truthy
    non-list top level (scalar, mapping, ...) is not iterable/enumerable
    safely, so each such file yields ONE sentinel entry
    (``__malformed_document__``) that ``test_every_rule_is_named`` reports as
    a named failure (``<file>:doc``) instead of a collection TypeError.
    A null/empty file decodes to no rules (skips), as before.
    """
    out: list[tuple[str, int, dict[str, Any]]] = []
    base = Path(rules_dir) if rules_dir else AUTO_ANSWERS_DIR
    for path in sorted(glob.glob(str(base / "*.yaml"))):
        fname = os.path.basename(path)
        if fname == EXTRA_SAMPLES_FILE:
            continue
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
        except Exception:
            continue
        if not isinstance(raw, list):
            out.append(
                (
                    fname,
                    -1,
                    {
                        "__malformed_document__": f"top-level {type(raw).__name__}, expected a list of rules"
                    },
                )
            )
            continue
        for idx, item in enumerate(raw):
            if isinstance(item, dict) and (not enabled_only or item.get("enabled", True)):
                out.append((fname, idx, item))
    return out


def _enabled_rules(rules_dir: Path | str | None = None) -> list[tuple[str, int, dict[str, Any]]]:
    """Enabled-only view of :func:`_load_rules` (the historical helper name)."""
    return _load_rules(rules_dir, enabled_only=True)


def _extra_samples() -> dict[str, list[str]]:
    raw = yaml.safe_load((CORPUS_DIR / EXTRA_SAMPLES_FILE).read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict), f"{EXTRA_SAMPLES_FILE} must be a rule-name -> [file] mapping"
    return {k: list(v or []) for k, v in raw.items()}


def _samples_for(name: str) -> list[Path]:
    """Primary sample ``<rule>.txt`` first, then any declared extras."""
    return [SAMPLES_DIR / f"{name}.txt"] + [
        SAMPLES_DIR / extra for extra in _extra_samples().get(name, [])
    ]


def _as_rule(item: dict[str, Any]) -> ar.Rule:
    return ar.Rule(
        name=_rule_name(item),
        enabled=True,
        match_mode=item.get("match_mode", "contains"),
        question=item["question"],
        options=list(item.get("options", []) or []),
        answer=item.get("answer", "wait"),
    )


#: The whole corpus, enabled and disabled alike: ``matches()`` is independent of
#: ``enabled``, and a canonicalization regression does not care whether a rule is
#: currently switched on. Versioned, so this list is identical on every host.
_CORPUS = _load_rules(CORPUS_DIR, enabled_only=False)
#: The machine's own rules. Drift reporting ONLY — never assert against these.
_LIVE = _enabled_rules()

#: (rule, sample) pairs, one test each. The id is the sample's stem, which is the
#: rule name for every rule that has a single screen.
_PAIRS = [
    pytest.param((item, sample), id=sample.stem)
    for _f, _i, item in _CORPUS
    for sample in _samples_for(_rule_name(item))
]


def test_corpus_is_present_and_non_empty() -> None:
    """The corpus is versioned, so an empty one means it was lost, not that this
    host has no rules — the failure mode the live-yaml skipif used to hide."""
    assert _CORPUS, f"no rules found under {CORPUS_DIR}"


def test_every_rule_is_named() -> None:
    """Every corpus rule must carry a non-empty ``name`` — a nameless rule is
    unmatchable (production keys rules by name) and breaks the corpus ids, so it
    must fail HERE as one clear listing, not a collection KeyError."""
    nameless = [(fname, idx, item) for fname, idx, item in _CORPUS if not _rule_name(item).strip()]
    assert not nameless, "every rule must have a non-empty 'name'; offenders: " + "; ".join(
        f"{_rule_id(fname, idx, item)} -> {str(item)[:60]}" for fname, idx, item in nameless
    )


SHAPES_DIR = Path(__file__).parents[1] / "fixtures" / "auto_answers_corpus_shapes"

# F704 #559 round 2: every non-list top-level yaml shape must be survived by
# the loader — never a collection TypeError; each truthy non-list document
# (scalar, mapping, ...) becomes ONE named failing test id <file>:doc via
# test_every_rule_is_named; null/empty files decode to no rules (skip).
_NON_LIST_CASES = [
    pytest.param("scalar", "int", id="scalar"),
    pytest.param("mapping", "dict", id="mapping"),
]


@pytest.mark.parametrize("fixture,decoded_type", _NON_LIST_CASES)
def test_non_list_yaml_yields_one_named_failure_not_crash(fixture: str, decoded_type: str) -> None:
    """A truthy non-list top level must produce exactly one malformed sentinel
    entry that names the file — not a collection crash."""
    rules = _enabled_rules(SHAPES_DIR / fixture)
    assert len(rules) == 1, f"expected exactly one sentinel entry for {fixture}, got {rules!r}"
    fname, idx, item = rules[0]
    assert _is_malformed_doc(item)
    assert decoded_type in str(item)
    assert fname == "rules.yaml"
    assert _rule_id(fname, idx, item) == f"{fname}:doc"


@pytest.mark.parametrize("fixture", ["null", "empty"], ids=["null", "empty"])
def test_null_or_empty_yaml_decodes_to_no_rules(fixture: str) -> None:
    """Null/empty files stay the historical behaviour: zero rules, tests skip."""
    assert _enabled_rules(SHAPES_DIR / fixture) == []


def test_nameless_rule_still_yields_named_entry() -> None:
    """Existing nameless-rule behaviour is kept: one entry, no name, id
    <file>:rule0 (fails test_every_rule_is_named, skipped elsewhere)."""
    rules = _enabled_rules(SHAPES_DIR / "nameless")
    assert len(rules) == 1
    fname, idx, item = rules[0]
    assert not _is_malformed_doc(item)
    assert _rule_name(item) == ""
    assert _rule_id(fname, idx, item) == f"{fname}:rule0"


@pytest.mark.parametrize("rule_and_sample", _PAIRS)
def test_enabled_shipped_rule_matches_its_sample(
    rule_and_sample: tuple[dict[str, Any], Path],
) -> None:
    """Each corpus rule must still match a representative rendered screen under
    the two-domain canonical matcher (F597 #454 B2)."""
    rule_item, sample = rule_and_sample
    name = _rule_name(rule_item)
    assert name, "nameless rule (see test_every_rule_is_named)"
    assert sample.exists(), f"missing representative sample for rule {name!r}: add {sample}"
    rule = _as_rule(rule_item)
    region = ar.dialog_region(sample.read_text(encoding="utf-8").splitlines())
    assert rule.matches(region), (
        f"rule {name!r} ({rule.match_mode}) failed to match {sample.name}; "
        f"reject={rule.reject_reason(region)!r}\n"
        f"full={region.normalized!r}\nlight={region.normalized_light!r}"
    )


def test_every_enabled_rule_has_a_sample() -> None:
    """No corpus rule may lack its primary sample (else a regression could hide).

    Hermetic since F932 #784: this ranges over the versioned corpus, not over
    whatever rule set this machine happens to have installed.
    """
    missing = [
        _rule_name(item)
        for _f, _i, item in _CORPUS
        if _rule_name(item) and not (SAMPLES_DIR / f"{_rule_name(item)}.txt").exists()
    ]
    assert not missing, f"corpus rules without a sample fixture: {missing}"


def test_every_sample_belongs_to_a_corpus_rule() -> None:
    """The reverse direction: a sample no rule claims is dead weight that no
    test exercises. Declare it in extra-samples.yaml or delete it."""
    claimed = {p.name for _f, _i, item in _CORPUS for p in _samples_for(_rule_name(item))}
    orphans = sorted(p.name for p in SAMPLES_DIR.glob("*.txt") if p.name not in claimed)
    assert not orphans, (
        f"sample fixtures claimed by no corpus rule: {orphans} — add the rule to "
        f"{CORPUS_DIR.name}/, list the file under {EXTRA_SAMPLES_FILE}, or delete it"
    )


def test_declared_extra_samples_exist_and_name_a_corpus_rule() -> None:
    """extra-samples.yaml must not accumulate entries for deleted rules/files."""
    names = {_rule_name(item) for _f, _i, item in _CORPUS}
    for rule_name, files in _extra_samples().items():
        assert rule_name in names, f"{EXTRA_SAMPLES_FILE} names unknown rule {rule_name!r}"
        for fname in files:
            assert (
                SAMPLES_DIR / fname
            ).exists(), f"{EXTRA_SAMPLES_FILE}: {rule_name!r} lists missing sample {fname!r}"


def _seed_rule_params() -> list[Any]:
    out = []
    for fname, text in sorted(ar.SEED_RULES.items()):
        for item in yaml.safe_load(text) or []:
            if isinstance(item, dict) and item.get("name") and item.get("enabled", True):
                out.append(pytest.param((fname, item), id=item["name"]))
    return out


@pytest.mark.parametrize("seed", _seed_rule_params())
def test_seed_rule_is_in_the_corpus_and_matches_its_sample(
    seed: tuple[str, dict[str, Any]],
) -> None:
    """The enforceable half of "no shipped rule may lack a sample".

    ``SEED_RULES`` is what CAO actually writes into a fresh
    ``~/.aws/cli-agent-orchestrator/auto-answers/<provider>.yaml``, so it is the
    only rule set the fork ships and the only one it can be held to. Each seeded
    rule must be covered by the corpus AND match its sample as shipped — the
    live file may have been hand-edited since, which is precisely why the live
    file cannot carry this assertion (F932 #784).
    """
    fname, item = seed
    name = item["name"]
    corpus = {_rule_name(it) for _f, _i, it in _CORPUS}
    assert name in corpus, (
        f"SEED_RULES[{fname!r}] ships rule {name!r} but the versioned corpus does not "
        f"cover it; add it to {CORPUS_DIR.name}/{fname} with a sample"
    )
    sample = SAMPLES_DIR / f"{name}.txt"
    assert sample.exists(), f"shipped rule {name!r} has no sample: add {sample}"
    rule = _as_rule(item)
    region = ar.dialog_region(sample.read_text(encoding="utf-8").splitlines())
    assert rule.matches(region), (
        f"shipped rule {name!r} regressed against its sample: " f"{rule.reject_reason(region)!r}"
    )


def test_regressed_regex_rules_present_and_match() -> None:
    """Explicit guard for the exact three rules the gate flagged: they use regex
    with punctuation/glyph anchors and MUST match under the light domain.

    These are asserted PRESENT, not skipped-if-absent: the corpus is versioned,
    so deleting one of them is a change to this repo, not a property of the host.
    """
    by_name = {_rule_name(item): item for _f, _i, item in _CORPUS if _rule_name(item)}
    for name in (
        "askuserquestion-fork-prompt",
        "codex-ratelimit-model-switch",
        "codex-update-available",
    ):
        assert name in by_name, f"{name} must stay in the versioned corpus"
        rule = _as_rule(by_name[name])
        region = ar.dialog_region((SAMPLES_DIR / f"{name}.txt").read_text().splitlines())
        assert rule.matches(region), f"{name} regressed: {rule.reject_reason(region)!r}"


@pytest.mark.skipif(not _LIVE, reason="no live auto-answers yaml present (clean checkout)")
def test_live_rule_drift_is_reported_not_asserted() -> None:
    """Report, never fail: enabled rules installed on THIS machine that the
    versioned corpus does not cover.

    This is the only place the live directory is read, and it deliberately makes
    no assertion about it. Operator state cannot decide a commit's colour
    (F932 #784) — a rule a human added to their own home an hour ago is not a
    regression in this repo. The gap is still worth surfacing, because a rule
    with no sample is a rule no regression guard covers; the fix is to add the
    rule and a rendered sample to the corpus, not to make this test red.
    """
    covered = {_rule_name(item) for _f, _i, item in _CORPUS}
    drift = sorted(
        f"{fname}:{_rule_name(item)}"
        for fname, _i, item in _LIVE
        if _rule_name(item) and _rule_name(item) not in covered
    )
    if drift:
        # The "[auto-answers drift]" prefix is load-bearing: pyproject's
        # filterwarnings turns warnings into errors by default, and this one is
        # downgraded to "default" by that exact prefix (precedent:
        # "[tier-budget WARN]"). Without it the report would become a failure —
        # the host-dependence this whole rewrite removed.
        warnings.warn(
            "[auto-answers drift] live auto-answers rules not covered by the "
            f"versioned corpus ({len(drift)}): {drift} — add each rule to "
            "test/fixtures/auto_answers_corpus/ with a rendered sample",
            UserWarning,
            stacklevel=1,
        )
    assert True  # reporting only, by design
