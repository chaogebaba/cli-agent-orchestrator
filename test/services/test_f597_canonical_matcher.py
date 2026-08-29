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


# ---------------------------------------------------------------------------
# B3: NFKC is load-bearing
# ---------------------------------------------------------------------------


def test_nfkc_folds_fullwidth_and_circled_digits() -> None:
    """B3: NFKC normalization maps fullwidth letters and circled digits to their
    ASCII forms BEFORE the [a-z0-9] fold, so a fullwidth-rendered prompt reduces
    to the same tokens as its ASCII form. Without NFKC these code points are not
    in [a-z0-9] and would be erased to spaces."""
    assert ar.canonicalize("ＡＢＣ①") == "abc1"
    # Light domain (regex) also NFKC-normalizes.
    assert ar.canonicalize_light("ＡＢＣ①") == "abc1"


def test_fullwidth_trust_card_matches_only_with_nfkc(monkeypatch) -> None:
    """B3: a fullwidth-glyph trust card matches the plain codex-trust-dir rule
    only because NFKC folds the fullwidth chars to ASCII. Drop NFKC (lower-only)
    and the same fixture no longer matches — proving NFKC is load-bearing."""
    rule = _codex_rules_via_tmp()["codex-trust-dir"]
    lines = _fixture_lines("codex_trust_fullwidth.txt")

    # With NFKC (production): fullwidth question folds to ASCII → matches.
    assert rule.matches(ar.dialog_region(lines)), (
        "fullwidth card should match with NFKC; got "
        f"{rule.reject_reason(ar.dialog_region(lines))!r}"
    )

    # MUTANT: drop NFKC from the full fold (lower + [a-z0-9] map only). The
    # fullwidth code points are not in [a-z0-9] → erased to spaces → the anchor
    # is destroyed → no match. Re-parse the rule under the mutant too.
    def _no_nfkc(text: str) -> str:
        folded = text.lower()
        folded = "".join(ch if ("a" <= ch <= "z" or "0" <= ch <= "9") else " " for ch in folded)
        return " ".join(folded.split())

    monkeypatch.setattr(ar, "canonicalize", _no_nfkc)
    mutant_rule = _codex_rules_via_tmp()["codex-trust-dir"]
    mutant_region = ar.dialog_region(lines)  # normalize_screen uses ar.canonicalize
    assert not mutant_rule.matches(mutant_region), (
        "without NFKC the fullwidth card must NOT match; canonical=" f"{mutant_region.normalized!r}"
    )


# ---------------------------------------------------------------------------
# B4: no-match region log is deduped per terminal/region
# ---------------------------------------------------------------------------


def test_log_no_match_region_dedupes_per_terminal_region(tmp_path, monkeypatch) -> None:
    """B4: two identical _log_no_match_region calls for the same terminal+region
    write exactly ONE decisions-log record; a fresh write is allowed after
    clear_terminal()."""
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    engine = ar.AutoResponder()
    canonical = "do you trust the contents of this directory 1 yes continue 2 no quit"

    engine._log_no_match_region("term-b4", canonical)
    engine._log_no_match_region("term-b4", canonical)  # identical → deduped

    log_path = tmp_path / "term-b4.decisions.log"
    dumps = [
        line
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if "reason=region_dump" in line
    ]
    assert len(dumps) == 1, f"expected exactly one region_dump, got {len(dumps)}: {dumps}"

    # After clear_terminal, the dedupe memory is purged → a fresh dump is written.
    engine.clear_terminal("term-b4")
    engine._log_no_match_region("term-b4", canonical)
    dumps_after = [
        line
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if "reason=region_dump" in line
    ]
    assert (
        len(dumps_after) == 2
    ), f"a fresh dump must be written after clear_terminal(); got {len(dumps_after)}"


def test_mutant_remove_dedupe_writes_two_records(tmp_path, monkeypatch) -> None:
    """MUTANT: neutralize the dedupe set → two identical calls write TWO records,
    proving the dedupe is load-bearing (gate B4 mutant)."""
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    engine = ar.AutoResponder()

    # Mutant: make the seen-set a throwaway so membership never persists.
    class _NoMemory(dict):
        def setdefault(self, *_a, **_k):
            return set()

    engine._logged_region_hashes = _NoMemory()
    canonical = "some persistent unmatched dialog region text"
    engine._log_no_match_region("term-mut", canonical)
    engine._log_no_match_region("term-mut", canonical)

    dumps = [
        line
        for line in (tmp_path / "term-mut.decisions.log").read_text().splitlines()
        if "reason=region_dump" in line
    ]
    assert len(dumps) == 2
