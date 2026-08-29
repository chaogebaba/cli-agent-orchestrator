"""F597 #454: canonical-form dialog matcher fixture corpus.

The pivot (user directive) replaced the box-drawing glyph-strip with a
canonical-form matcher: ``canonicalize`` NFKC-normalizes, lowercases, maps every
non-``[a-z0-9]`` character to a space, and collapses runs. It is applied to BOTH
the composited screen (``normalize_screen``) and each rule's ``question`` /
``options`` on load, so a plain-prose ``contains`` rule matches the same prompt
whether codex renders it as plain unwalled text (0.150-era) or inside a 58-col
rounded-border card whose question wraps across a ``│`` wall (0.151.0) — the
F530 #386 root cause.

Corpus:
  (a) codex_trust_0150_plain.txt        — 0.150-era plain unwalled prompt
  (b) codex_trust_0151_card.txt         — 0.151.0 bordered card, question wrapped
  (c) codex_trust_0151_card_anyway.txt  — same card, "Yes, continue anyway…" option

Mutant: identity-canonicalize (``canonicalize = lambda s: s``) → fixture (b) no
longer matches (the ``│`` walls and mid-phrase wrap break the ``contains`` anchor),
proving the fold is what makes the card match.
"""

from pathlib import Path

import pytest

from cli_agent_orchestrator.services import auto_responder as ar

FIXTURES = Path(__file__).parent / "fixtures" / "auto_responder"


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def _codex_rules_via_tmp() -> dict[str, ar.Rule]:
    # _RuleStore._load takes a path; write the seed to a temp file to parse it.
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(ar.SEED_RULES["codex.yaml"])
        path = Path(fh.name)
    try:
        return {r.name: r for r in ar._RuleStore._load(path)}
    finally:
        path.unlink(missing_ok=True)


@pytest.fixture()
def trust_rule() -> ar.Rule:
    return _codex_rules_via_tmp()["codex-trust-dir"]


@pytest.mark.parametrize(
    "fixture_name",
    [
        "codex_trust_0150_plain.txt",
        "codex_trust_0151_card.txt",
        "codex_trust_0151_card_anyway.txt",
    ],
)
def test_all_fixtures_match_plain_trust_rule(trust_rule: ar.Rule, fixture_name: str) -> None:
    """(a)/(b)/(c): every rendering matches the plain ``codex-trust-dir`` rule."""
    normalized = ar.normalize_screen(_fixture_lines(fixture_name))
    assert trust_rule.matches(normalized), f"{fixture_name} did not match: {normalized!r}"


def test_card_and_plain_canonicalize_to_same_question() -> None:
    """The bordered card and the plain prompt fold to the same question text —
    the wrap point and ``│`` walls are irrelevant after canonicalization."""
    plain = ar.normalize_screen(_fixture_lines("codex_trust_0150_plain.txt"))
    card = ar.normalize_screen(_fixture_lines("codex_trust_0151_card.txt"))
    anchor = ar.canonicalize("Do you trust the contents of this directory?")
    assert anchor in plain
    assert anchor in card


def test_regex_rule_survives_canonicalization() -> None:
    """The existing ``codex-usage-resets`` regex rule still matches after the
    fold: ``\\d+`` and word tokens survive; the pattern is applied
    case-insensitively to the lowercased canonical string."""
    rule = _codex_rules_via_tmp()["codex-usage-resets"]
    screen = ar.normalize_screen(
        [
            "You have 3 usage limit resets available. Run /usage to use one.",
            "› 1. Yes, continue",
            "  2. No, quit",
        ]
    )
    assert rule.matches(screen)


def test_mutant_identity_canonicalize_fails_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant: replace ``canonicalize`` with identity → fixture (b) no longer
    matches the plain rule (walls + mid-phrase wrap break the contains anchor),
    while the plain fixture (a) is unaffected. This proves the fold is load-
    bearing for the card class."""
    monkeypatch.setattr(ar, "canonicalize", lambda text: text)

    # Re-parse the rule UNDER the identity mutant so the rule's own question is
    # not folded either (mirrors a build where canonicalize never existed).
    mutant_rule = _codex_rules_via_tmp()["codex-trust-dir"]

    card = ar.normalize_screen(_fixture_lines("codex_trust_0151_card.txt"))
    assert not mutant_rule.matches(card), (
        "identity-canonicalize should NOT match the bordered card; got a match, "
        f"canonical={card!r}"
    )

    # Sanity: the plain fixture is unwalled, so the raw contains-anchor is intact
    # even under the identity mutant (proves the mutant assertion isolates the
    # card's walls/wrap, not the prompt wording).
    plain = ar.normalize_screen(_fixture_lines("codex_trust_0150_plain.txt"))
    assert mutant_rule.matches(plain)
