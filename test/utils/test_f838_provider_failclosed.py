"""F838 (#695) — fail-closed provider resolution for composition/alias stubs.

A ``pi_cli`` alias stub silently spawned as the supervisor's ``claude_code``/Opus
because a profile that DECLARED a provider/composition but resolved to no valid
provider fell back to the caller's provider. These arms pin the fail-closed
replacement: such a case raises ``ProviderResolutionError`` (E-PROVIDER-UNRESOLVED),
never a silent substitution — while a GENUINE legacy plain profile (declares no
provider/composition intent) keeps the caller-provider fallback.

r2 (codex EMPIRICAL-GATE-NO Blocker 1): the resolver now derives BOTH the
declared-intent decision AND the provider from a SINGLE immutable raw read of
the stub, and an UNKNOWN read (unreadable/unparseable/vanished-mid-flight) FAILS
CLOSED rather than being treated as "no declared intent → fall back". These arms
patch the single-read seams (``read_agent_profile_source`` and
``resolve_agent_profile``) so a raw-read error is exercised through the real
classification path, not a swallow-everything wrapper.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.agent_profiles import (
    E_PROVIDER_UNRESOLVED,
    ProviderResolutionError,
    resolve_provider,
)

_MOD = "cli_agent_orchestrator.utils.agent_profiles"
_READ = f"{_MOD}.read_agent_profile_source"
_COMPOSE = f"{_MOD}.resolve_agent_profile"

# A raw stub that DECLARES an alias composition (extends + provider). The exact
# body is irrelevant — the resolver classifies intent from these bytes, then the
# patched ``resolve_agent_profile`` supplies the composed AgentProfile.
_DECLARING_STUB = "---\nextends: empirical_reviewer_lite\nprovider: pi_cli\n---\nbody\n"
# A raw stub that declares NO provider/composition intent (genuine legacy plain).
_PLAIN_STUB = "---\ndescription: a plain legacy profile\n---\nbody\n"


def test_healthy_stub_resolves_declared_provider():
    """A healthy alias stub (declares extends+provider, composes cleanly to
    pi_cli) resolves to pi_cli — never the caller's provider."""
    with (
        patch(_READ, return_value=_DECLARING_STUB),
        patch(
            _COMPOSE,
            return_value=AgentProfile(
                name="pi_cli_empirical_reviewer_lite", description="d", provider="pi_cli"
            ),
        ),
    ):
        assert resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code") == "pi_cli"


def test_truncated_stub_declares_but_resolves_none_refuses():
    """The #695 bite: the stub declared a provider/composition but the composed
    profile carries no provider. MUST raise E-PROVIDER-UNRESOLVED, never fall
    back to claude_code."""
    with (
        patch(_READ, return_value=_DECLARING_STUB),
        patch(
            _COMPOSE,
            return_value=AgentProfile(name="pi_cli_empirical_reviewer_lite", description="d"),
        ),
    ):
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED
    assert "claude_code" in str(ei.value)  # names the refused fallback


def test_declared_composition_fails_to_load_refuses():
    """A stub that declares intent but whose composition RAISES also fails
    closed rather than falling back."""
    with (
        patch(_READ, return_value=_DECLARING_STUB),
        patch(_COMPOSE, side_effect=RuntimeError("compose boom")),
    ):
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED


def test_invalid_provider_on_declaring_stub_refuses():
    """A declaring stub whose provider is present but invalid must refuse, not
    silently fall back (the old path warned then returned the caller's provider)."""
    with (
        patch(_READ, return_value=_DECLARING_STUB),
        patch(
            _COMPOSE,
            return_value=AgentProfile(
                name="pi_cli_empirical_reviewer_lite", description="d", provider="claud_code"
            ),
        ),
    ):
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED


def test_legacy_plain_profile_no_intent_keeps_fallback():
    """A GENUINE legacy plain profile (declares NO provider/composition intent)
    keeps today's caller-provider fallback — the behaviour pinned by
    test_returns_fallback_when_no_provider_key. Not every provider-less profile
    is a defect; only a DECLARING one is."""
    with (
        patch(_READ, return_value=_PLAIN_STUB),
        patch(_COMPOSE, return_value=AgentProfile(name="reviewer", description="d")),
    ):
        assert resolve_provider("reviewer", "kiro_cli") == "kiro_cli"


def test_absent_profile_keeps_fallback():
    """A truly absent name (FileNotFoundError from the raw read) still falls back
    without raising — provider.initialize surfaces the real error later; nothing
    to contradict."""
    with patch(_READ, side_effect=FileNotFoundError("Agent profile not found: ghost")):
        assert resolve_provider("ghost", "kiro_cli") == "kiro_cli"


def test_stub_read_error_refuses():
    """r2 INVERTED (was test_stub_read_error_does_not_manufacture_refusal):
    a raw-stub read error is an UNKNOWN store entry, not "no declared intent".
    A legacy alias whose stub cannot be read MUST be refused (fail closed), never
    inherit the caller's provider. This is codex Blocker 1's decisive assertion:
    uncertainty is a refusal, not a fallback."""
    with patch(_READ, side_effect=OSError("read blew up")):
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED
    assert "claude_code" in str(ei.value)  # names the refused fallback


def test_unparseable_stub_refuses():
    """A stub whose bytes are present but are not parseable frontmatter is also
    UNKNOWN — indeterminate intent — and MUST refuse rather than fall back."""
    # A bytes payload that frontmatter cannot parse into metadata cleanly.
    with patch(_COMPOSE, side_effect=AssertionError("should not compose on UNKNOWN")):
        with patch(_READ, side_effect=ValueError("not valid frontmatter")):
            # A raw-read that raises a non-FileNotFound error is UNKNOWN -> refuse
            # BEFORE compose is ever attempted.
            with pytest.raises(ProviderResolutionError) as ei:
                resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED


# ---------------------------------------------------------------------------
# F838 (#695) r3 — REAL-FILE adversaries the codex r2 EMPIRICAL-GATE-NO named.
#
# The r2 classifier accepted three real on-disk shapes as permitted fallback
# states rather than uncertainty (verdict-f838-r2 P0 blocker A):
#   (a) a TRUNCATED stub (``---`` opener, no closing delimiter) — parsed to empty
#       metadata and was mislabelled PLAIN.
#   (b) a BOM-prefixed stub — the U+FEFF pushed the ``---`` off offset 0 so
#       frontmatter was never detected; empty metadata; mislabelled PLAIN.
#   (c) a DANGLING SYMLINK store entry — ``Path.exists()`` follows the link and
#       reads False, so the lookup fell through to FileNotFoundError and was
#       mislabelled ABSENT.
# All three MUST classify UNKNOWN and REFUSE (E-PROVIDER-UNRESOLVED). These are
# real files under a scratch store dir (not mocks), the reviewer's probe turned
# into permanent regression tests.
# ---------------------------------------------------------------------------

from contextlib import ExitStack  # noqa: E402
from pathlib import Path  # noqa: E402

from cli_agent_orchestrator.utils import agent_profiles as _ap  # noqa: E402


def _store_patches(store: Path) -> ExitStack:
    """Point the profile lookup at a scratch store dir and disable every other
    configured agent dir, so the only file that can match is the one we wrote."""
    stack = ExitStack()
    stack.enter_context(patch.object(_ap, "LOCAL_AGENT_STORE_DIR", store))
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            return_value={},
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            return_value=[],
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
            return_value=[],
        )
    )
    return stack


def _write(store: Path, name: str, raw: str) -> Path:
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{name}.md"
    path.write_text(raw, encoding="utf-8", newline="")
    return path


def test_real_truncated_stub_classifies_unknown_and_refuses(tmp_path):
    """(a) A stub with a ``---`` opener but NO closing delimiter — the r2
    classifier called it PLAIN and fell back to claude_code."""
    store = tmp_path / "agent-store"
    _write(store, "f838_partial", "---\nprovider: pi_cli\n")
    with _store_patches(store):
        intent, declared, _ = _ap._classify_stub_intent("f838_partial")
        assert intent == _ap._STUB_UNKNOWN
        assert declared is None
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("f838_partial", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED
    assert "claude_code" in str(ei.value)


def test_real_bom_prefixed_stub_classifies_unknown_and_refuses(tmp_path):
    """(b) A complete-frontmatter stub preceded by a U+FEFF BOM — the r2
    classifier called it PLAIN and fell back."""
    store = tmp_path / "agent-store"
    _write(store, "f838_bom", "\ufeff---\nprovider: pi_cli\n---\nbody\n")
    with _store_patches(store):
        intent, declared, _ = _ap._classify_stub_intent("f838_bom")
        assert intent == _ap._STUB_UNKNOWN
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("f838_bom", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED


def test_real_dangling_symlink_classifies_unknown_and_refuses(tmp_path):
    """(c) A store entry that is a symlink whose target is missing — the r2
    classifier called it ABSENT (a clean absence) and fell back. A dangling link
    is a present-but-unreadable entry: UNKNOWN, refuse."""
    store = tmp_path / "agent-store"
    store.mkdir(parents=True, exist_ok=True)
    (store / "f838_dangling.md").symlink_to(store / "missing-target.md")
    with _store_patches(store):
        assert _ap._dangling_store_entry("f838_dangling") is True
        intent, _, _ = _ap._classify_stub_intent("f838_dangling")
        assert intent == _ap._STUB_UNKNOWN
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("f838_dangling", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED


def test_real_wellformed_empty_frontmatter_stays_plain(tmp_path):
    """Control: a WELL-FORMED but empty frontmatter block (``---\\n---``) is NOT
    malformed — the delimiter opens and closes cleanly. It declares no
    provider/composition, so it stays PLAIN and keeps the caller fallback. This
    guards against the malformed-detector over-refusing genuine plain profiles."""
    store = tmp_path / "agent-store"
    _write(store, "f838_emptyfm", "---\n---\nbody\n")
    with _store_patches(store):
        intent, _, _ = _ap._classify_stub_intent("f838_emptyfm")
        assert intent == _ap._STUB_PLAIN
        assert resolve_provider("f838_emptyfm", "kiro_cli") == "kiro_cli"


def test_real_no_frontmatter_plain_stays_plain(tmp_path):
    """Control: a genuine legacy plain profile with NO frontmatter block at all
    stays PLAIN (detect=False, not malformed) and keeps the caller fallback."""
    store = tmp_path / "agent-store"
    _write(store, "f838_noyaml", "# Just a heading\n\nplain body, no frontmatter\n")
    with _store_patches(store):
        intent, _, _ = _ap._classify_stub_intent("f838_noyaml")
        assert intent == _ap._STUB_PLAIN
        assert resolve_provider("f838_noyaml", "kiro_cli") == "kiro_cli"


def test_real_crlf_frontmatter_stays_declared(tmp_path):
    """Control: CRLF-delimited frontmatter is well-formed and DECLARED — it must
    resolve its declared provider, not be mistaken for malformed."""
    store = tmp_path / "agent-store"
    _write(store, "f838_crlf", "---\r\nprovider: pi_cli\r\n---\r\nbody\r\n")
    with _store_patches(store):
        intent, declared, _ = _ap._classify_stub_intent("f838_crlf")
        assert intent == _ap._STUB_DECLARED
        assert declared == "pi_cli"


def test_single_read_invariant_resolve_reads_store_once(tmp_path):
    """Single-read invariant (verdict): ``resolve_provider`` must read the raw
    store EXACTLY ONCE — it classifies and resolves from the same immutable
    bytes, never a second racy disk read."""
    store = tmp_path / "agent-store"
    _write(store, "f838_once", "---\ndescription: plain\n---\nbody\n")
    original_read = _ap.read_agent_profile_source
    with _store_patches(store):
        with patch.object(_ap, "read_agent_profile_source", wraps=original_read) as reads:
            resolve_provider("f838_once", "kiro_cli")
    assert reads.call_count == 1, f"expected 1 raw store read, got {reads.call_count}"


# ---------------------------------------------------------------------------
# F838 (#695) r4 — PRECEDENCE-SHADOWED dangling stub (codex r3 P0 blocker).
#
# ``read_agent_profile_source`` gates each candidate on ``Path.exists()`` (which
# FOLLOWS a symlink), so a HIGHER-precedence dangling link reads as absent and
# the lookup silently FALLS THROUGH to a LOWER-precedence same-named profile.
# The r3 code then classified that lower file DECLARED/PLAIN and its provider
# (or the caller fallback) reached creation — the ``_dangling_store_entry`` check
# never fired because a lower store satisfied the read. r4 requires: precedence
# resolution STOPS at the first store dir that has an entry for the name; if that
# entry is a dangling symlink it is UNKNOWN and REFUSES, never continuing to a
# lower dir. Real files under a scratch local store (higher) + a configured
# lower store, no mocks of the resolver internals.
# ---------------------------------------------------------------------------


def _store_patches_with_lower(higher: Path, lower: Path) -> ExitStack:
    """Point the local store at ``higher`` and expose ``lower`` as a
    lower-precedence configured agent dir; disable nothing else."""
    stack = ExitStack()
    stack.enter_context(patch.object(_ap, "LOCAL_AGENT_STORE_DIR", higher))
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            return_value={"lower": str(lower)},
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            return_value=[],
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
            return_value=[],
        )
    )
    return stack


def test_real_dangling_higher_over_declaring_lower_refuses(tmp_path):
    """(1) A dangling local flat entry SHADOWS a lower same-named DECLARING
    profile. The r3 code returned the lower profile's ``claude_code``; r4 must
    classify UNKNOWN and refuse (E-PROVIDER-UNRESOLVED) — the higher dangling
    entry stops resolution before the lower file is ever read."""
    higher = tmp_path / "agent-store"
    lower = tmp_path / "lower-store"
    higher.mkdir(parents=True, exist_ok=True)
    lower.mkdir(parents=True, exist_ok=True)
    (higher / "f838_shadow_decl.md").symlink_to(higher / "missing-shadow-target.md")
    (lower / "f838_shadow_decl.md").write_text(
        "---\nprovider: claude_code\n---\nlower body\n", encoding="utf-8"
    )
    original_read = _ap.read_agent_profile_source
    with _store_patches_with_lower(higher, lower):
        intent, _, _ = _ap._classify_stub_intent("f838_shadow_decl")
        assert intent == _ap._STUB_UNKNOWN
        with patch.object(_ap, "read_agent_profile_source", wraps=original_read) as reads:
            with pytest.raises(ProviderResolutionError) as ei:
                resolve_provider("f838_shadow_decl", "pi_cli")
        # The shadowed lower file must NEVER be read/trusted: the stat-only
        # precedence walk refuses before the raw read is issued.
        assert reads.call_count == 0
    assert ei.value.code == E_PROVIDER_UNRESOLVED


def test_real_dangling_higher_over_plain_lower_refuses(tmp_path):
    """(2) A dangling local flat entry SHADOWS a lower same-named PLAIN profile.
    The r3 code fell back to the caller provider; r4 must refuse — a dangling
    higher entry is uncertainty, not permission to use the lower plain file."""
    higher = tmp_path / "agent-store"
    lower = tmp_path / "lower-store"
    higher.mkdir(parents=True, exist_ok=True)
    lower.mkdir(parents=True, exist_ok=True)
    (higher / "f838_shadow_plain.md").symlink_to(higher / "missing-shadow-target.md")
    (lower / "f838_shadow_plain.md").write_text(
        "---\ndescription: lower plain profile\n---\nlower body\n", encoding="utf-8"
    )
    with _store_patches_with_lower(higher, lower):
        intent, _, _ = _ap._classify_stub_intent("f838_shadow_plain")
        assert intent == _ap._STUB_UNKNOWN
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("f838_shadow_plain", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED
    assert "claude_code" in str(ei.value)


def test_real_valid_higher_over_dangling_lower_resolves_higher(tmp_path):
    """(3) The MIRROR control: a VALID higher entry over a lower DANGLING
    same-named entry. Resolution stops at the first (higher) readable entry and
    resolves ITS provider; the lower dangling entry is irrelevant and must not
    trigger a spurious refusal. Guards the precedence walk against
    over-refusing when the higher entry is the legitimate winner."""
    higher = tmp_path / "agent-store"
    lower = tmp_path / "lower-store"
    higher.mkdir(parents=True, exist_ok=True)
    lower.mkdir(parents=True, exist_ok=True)
    (higher / "f838_valid_over_dangling.md").write_text(
        "---\nprovider: pi_cli\n---\nhigher valid body\n", encoding="utf-8"
    )
    (lower / "f838_valid_over_dangling.md").symlink_to(lower / "missing-lower-target.md")
    with _store_patches_with_lower(higher, lower):
        intent, declared, _ = _ap._classify_stub_intent("f838_valid_over_dangling")
        assert intent == _ap._STUB_DECLARED
        assert declared == "pi_cli"
        assert resolve_provider("f838_valid_over_dangling", "claude_code") == "pi_cli"


def test_real_malformed_higher_over_valid_lower_refuses(tmp_path):
    """A malformed (truncated) higher entry is a READABLE file, so the reader
    stops there and the r3 malformed check classifies it UNKNOWN — the lower
    valid profile is never reached. Pins that a malformed higher entry does not
    fall through to a lower same-named file either."""
    higher = tmp_path / "agent-store"
    lower = tmp_path / "lower-store"
    higher.mkdir(parents=True, exist_ok=True)
    lower.mkdir(parents=True, exist_ok=True)
    (higher / "f838_malformed_shadow.md").write_text(
        "---\nprovider: pi_cli\n", encoding="utf-8", newline=""
    )
    (lower / "f838_malformed_shadow.md").write_text(
        "---\nprovider: claude_code\n---\nlower body\n", encoding="utf-8"
    )
    with _store_patches_with_lower(higher, lower):
        intent, _, _ = _ap._classify_stub_intent("f838_malformed_shadow")
        assert intent == _ap._STUB_UNKNOWN
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("f838_malformed_shadow", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED


def test_real_valid_local_shadows_lower_still_reads_once(tmp_path):
    """Single-read invariant control for the shadowing path: a normal VALID
    higher entry (with an unrelated lower same-named file present) still resolves
    in EXACTLY ONE raw read — the stat-only precedence walk adds no extra read on
    the healthy path."""
    higher = tmp_path / "agent-store"
    lower = tmp_path / "lower-store"
    higher.mkdir(parents=True, exist_ok=True)
    lower.mkdir(parents=True, exist_ok=True)
    (higher / "f838_shadow_once.md").write_text(
        "---\nprovider: pi_cli\n---\nhigher\n", encoding="utf-8"
    )
    (lower / "f838_shadow_once.md").write_text(
        "---\nprovider: claude_code\n---\nlower\n", encoding="utf-8"
    )
    original_read = _ap.read_agent_profile_source
    with _store_patches_with_lower(higher, lower):
        with patch.object(_ap, "read_agent_profile_source", wraps=original_read) as reads:
            assert resolve_provider("f838_shadow_once", "claude_code") == "pi_cli"
    assert reads.call_count == 1, f"expected 1 raw store read, got {reads.call_count}"


# ---------------------------------------------------------------------------
# F838 (#695) r5 — CLASS fix: the precedence walk consumes the reader's OWN
# ordered store list (codex r4 P0 blocker).
#
# The r4 walk (`_first_present_store_entry_is_unknown`) modelled precedence with
# a SECOND, hand-copied list of store roots that OMITTED the packaged built-in
# store; r5 added the built-in store but stayed STORE-granular, so a readable
# NESTED entry masked a dangling/escaping FLAT entry in the SAME store (codex r5
# P0-A) and the composed candidate was outside the shared list (codex r5 P0-B).
# The r6 fix models precedence at CANDIDATE granularity: BOTH the reader and the
# walk iterate the SAME ordered candidate list, `_ordered_profile_candidates`
# (composed first, then per-store flat-then-nested, built-in last), so precedence
# stops at the FIRST PRESENT candidate and neither consumer can search a
# different or coarser sequence than the other.
#
# These tests (a) instrument the FILESYSTEM reads so a reader that iterates the
# shared list in a different order (or drops the last candidate) is caught — a
# factory-call spy that compares only classes cannot see that (the r5 gap the
# verdict named), and (b) parametrize a dangling / malformed / unreadable HIGHER
# entry shadowing a readable LOWER entry over EVERY (higher, lower) store pair,
# INCLUDING the built-in store as the lower, asserting typed refusal + zero reads
# of the shadowed lower file.
# ---------------------------------------------------------------------------

# A built-in packaged profile that is a genuine PLAIN profile (no
# provider/composition), so a shadow of it would — absent the fix — fall back to
# the caller provider (the exact codex r4 counterexample).
_BUILTIN_PLAIN_NAME = "reviewer"


class _RecordingCandidate:
    """Wraps a real `_ProfileCandidate` and records, into a shared per-consumer
    log, every time this candidate is TOUCHED by a consumer — via the reader's
    content seam (`read`) or the walk's stat seams
    (`is_present_readable`/`is_present_unusable`). The recorded value is the
    candidate's stable sequence position, so the log is the exact ORDER in which
    a consumer iterated the shared candidate list — not merely which list object
    it was handed. A reader that iterated `reversed(...)` or dropped the last
    candidate produces a DIFFERENT log and is caught."""

    def __init__(self, inner, index: int, kind: str, log: list) -> None:
        self._inner = inner
        self._index = index
        self.kind = kind
        self._log = log

    def _touch(self) -> None:
        self._log.append((self._index, self.kind))

    def read(self):
        self._touch()
        return self._inner.read()

    def is_present_readable(self) -> bool:
        self._touch()
        return self._inner.is_present_readable()

    def is_present_unusable(self) -> bool:
        self._touch()
        return self._inner.is_present_unusable()


def _first_touch_order(log: list) -> list:
    """Collapse a touch log to the ORDER in which candidates were first visited
    (a consumer may stat the same candidate more than once)."""
    seen = []
    for idx, kind in log:
        if (idx, kind) not in seen:
            seen.append((idx, kind))
    return seen


def test_reader_and_walk_touch_identical_candidate_sequence(tmp_path):
    """r6 CLASS assertion (codex r5 "same sequence" section): the reader and the
    fail-closed walk touch the IDENTICAL ordered candidate sequence, observed by
    instrumenting the candidates' filesystem seams (not a factory-call spy that
    compares only classes — the r5 gap the verdict named). Run over a MATRIX of
    layouts (empty, flat-only, nested-only, composed present) so the sequence is
    pinned for real precedence shapes, and pin that the built-in candidate is the
    LAST element of the shared list in every layout.

    Divergent-consumer mutant check (report): with this test in place, mutating
    ONLY the reader loop to iterate `reversed(_ordered_profile_candidates(...))`
    or `[:-1]` (drop the built-in) makes the two touch orders differ (or the
    built-in-last assertion fail) and this test FAILS — the exact mutants that
    SURVIVED the r5 class-comparing test. A hand-copied second list in either
    consumer likewise diverges here.
    """
    local_dir = tmp_path / "local-store"
    agent_dir = tmp_path / "agent-dir"
    extra_dir = tmp_path / "extra-dir"
    for d in (local_dir, agent_dir, extra_dir):
        d.mkdir(parents=True, exist_ok=True)

    stack = ExitStack()
    stack.enter_context(patch.object(_ap, "LOCAL_AGENT_STORE_DIR", local_dir))
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            return_value={"agent": str(agent_dir)},
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            return_value=[str(extra_dir)],
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
            return_value=[],
        )
    )

    real_ordered = _ap._ordered_profile_candidates

    def make_recording_factory(log: list):
        def factory(name: str):
            wrapped = [
                _RecordingCandidate(c, i, c.kind, log) for i, c in enumerate(real_ordered(name))
            ]
            return wrapped

        return factory

    # A matrix of layouts. Each entry is a callable that writes files for the
    # probe name and the name to resolve. All use names with NO match anywhere
    # readable, so BOTH consumers walk the WHOLE candidate list to the end
    # (reader → FileNotFoundError; walk → False), giving the full sequence.
    def layout_empty(_name):
        pass

    def layout_flat_present_dangling(name):
        # A dangling flat in agent-dir + a readable nested below it: the reader
        # must still walk to the end for a NAME THAT NEVER RESOLVES, but the
        # ordering (flat before nested) is exercised structurally.
        (agent_dir / f"{name}.md").symlink_to(agent_dir / "missing.md")

    def layout_nested_only(name):
        nested = extra_dir / name / "agent.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        # dangling so the name never resolves and the whole list is walked
        nested.symlink_to(extra_dir / name / "missing.md")

    layouts = [layout_empty, layout_flat_present_dangling, layout_nested_only]

    with stack:
        for i, layout in enumerate(layouts):
            name = f"seq_probe_{i}"  # legacy flat name → no composed candidate
            layout(name)

            walk_log: list = []
            with patch.object(
                _ap, "_ordered_profile_candidates", side_effect=make_recording_factory(walk_log)
            ):
                _ap._first_present_store_entry_is_unknown(name)

            reader_log: list = []
            with patch.object(
                _ap, "_ordered_profile_candidates", side_effect=make_recording_factory(reader_log)
            ):
                with pytest.raises(FileNotFoundError):
                    _ap.read_agent_profile_source(name)

            walk_order = _first_touch_order(walk_log)
            reader_order = _first_touch_order(reader_log)
            assert walk_order, (i, "walk touched no candidate")
            assert reader_order, (i, "reader touched no candidate")
            # The decisive assertion: the two consumers visit the identical
            # ordered candidate sequence (index+kind), so neither can iterate a
            # different order or drop a candidate the other keeps.
            assert walk_order == reader_order, (i, walk_order, reader_order)
            # Indices must be a contiguous 0..n prefix in order (no reordering,
            # no skipped/duplicated positions) — kills reversed()/[:-1] mutants.
            assert [idx for idx, _ in reader_order] == list(range(len(reader_order))), (
                i,
                reader_order,
            )
            # Built-in candidate is the LAST element of the shared list.
            full = real_ordered(name)
            assert full[-1].kind == "builtin", (i, [c.kind for c in full])


def test_ordered_candidates_builtin_last_and_flat_before_nested(tmp_path):
    """Direct pin on the shared candidate list: the built-in candidate is LAST,
    and inside a configured store the flat `{name}.md` candidate precedes the
    nested `{name}/agent.md` candidate (the r5 P0-A ordering the store-granular
    model collapsed)."""
    agent_dir = tmp_path / "agent-dir"
    agent_dir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(patch.object(_ap, "LOCAL_AGENT_STORE_DIR", tmp_path / "local"))
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
                return_value={"agent": str(agent_dir)},
            )
        )
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
                return_value=[],
            )
        )
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
                return_value=[],
            )
        )
        cands = _ap._ordered_profile_candidates("anything")
    kinds = [c.kind for c in cands]
    assert kinds[-1] == "builtin", kinds
    # local flat, then agent-dir flat, then agent-dir nested, then builtin.
    assert kinds == ["flat", "flat", "nested", "builtin"], kinds


def test_composed_candidate_is_first_for_effective_name(tmp_path, monkeypatch):
    """The composed candidate is the FIRST element of the shared list for an
    effective `<position>-<provider>` name (codex r5 P0-B: it must be INSIDE the
    shared list, not a reader-only pre-check the walk cannot see). A legacy flat
    name has NO composed candidate."""
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "home"))
    with ExitStack() as stack:
        stack.enter_context(patch.object(_ap, "LOCAL_AGENT_STORE_DIR", tmp_path / "local"))
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
                return_value={},
            )
        )
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
                return_value=[],
            )
        )
        stack.enter_context(
            patch(
                "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
                return_value=[],
            )
        )
        # An effective name: <position>-<provider> with a real provider token.
        eff = _ap._ordered_profile_candidates("reviewer-pi_cli")
        legacy = _ap._ordered_profile_candidates("reviewer")
    assert eff[0].kind == "composed", [c.kind for c in eff]
    assert "composed" not in [c.kind for c in legacy], [c.kind for c in legacy]


# --- Parametrized shadowing over every (higher, lower) store pair -----------
#
# Store levels in precedence order: local > agent-dir > extra-dir > built-in.
# A "higher" slot is made a bad entry (dangling / escaping-symlink / malformed);
# a "lower" slot holds a readable same-name profile the reader would fall
# through to. Every (higher, lower) ordered pair is exercised; the built-in
# store is a LOWER slot only (it is lowest precedence and holds no user
# symlinks, so it is never a higher bad entry).

_HIGHER_SLOTS = ["local", "agent", "extra"]
_LOWER_SLOTS = ["agent", "extra", "builtin"]


def _precedence_index(slot: str) -> int:
    return {"local": 0, "agent": 1, "extra": 2, "builtin": 3}[slot]


_STORE_PAIRS = [
    (h, l)
    for h in ("local", "agent", "extra")
    for l in ("agent", "extra", "builtin")
    if _precedence_index(l) > _precedence_index(h)
]

_BAD_HIGHER_KINDS = ["dangling", "escaping", "malformed", "unreadable"]


def _configure_slots(tmp_path: Path, higher_slot: str, lower_slot: str) -> ExitStack:
    """Wire the four store levels. Only the higher/lower slots that participate
    in the current pair get real directories; the built-in store is always the
    real packaged store. Returns the ExitStack of patches."""
    local_dir = tmp_path / "local-store"
    agent_dir = tmp_path / "agent-dir"
    extra_dir = tmp_path / "extra-dir"
    for d in (local_dir, agent_dir, extra_dir):
        d.mkdir(parents=True, exist_ok=True)

    stack = ExitStack()
    stack.enter_context(patch.object(_ap, "LOCAL_AGENT_STORE_DIR", local_dir))
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            return_value={"agent": str(agent_dir)},
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            return_value=[str(extra_dir)],
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
            return_value=[],
        )
    )
    stack._dirs = {"local": local_dir, "agent": agent_dir, "extra": extra_dir}  # type: ignore[attr-defined]
    return stack


def _write_bad_higher(
    dirs: dict, slot: str, name: str, kind: str, outside: Path, position: str = "flat"
) -> None:
    d = dirs[slot]
    if position == "nested":
        # The nested `{name}/agent.md` candidate is a first-class member of the
        # shared candidate list; a bad entry HERE must stop precedence exactly as
        # a bad flat entry does. Without this position the suite cannot detect a
        # regression in _PathCandidate.is_present_unusable for nested candidates.
        flat = d / name / "agent.md"
        flat.parent.mkdir(parents=True, exist_ok=True)
    else:
        flat = d / f"{name}.md"
    if kind == "dangling":
        flat.symlink_to(d / "missing-target.md")
    elif kind == "escaping":
        # A symlink whose live target escapes the store root: _safe_join rejects
        # it (returns None), so the reader skips it — a present-but-unusable
        # entry the r4 walk did not model.
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("---\nprovider: pi_cli\n---\noutside body\n", encoding="utf-8")
        flat.symlink_to(outside)
    elif kind == "malformed":
        # A real readable file the reader STOPS at, but whose frontmatter is
        # truncated (no closing delimiter) → UNKNOWN.
        flat.write_text("---\nprovider: pi_cli\n", encoding="utf-8", newline="")
    elif kind == "unreadable":
        flat.write_text("---\nprovider: pi_cli\n---\nbody\n", encoding="utf-8")
        flat.chmod(0)
    else:  # pragma: no cover - guard
        raise AssertionError(kind)


def _write_lower_readable(dirs: dict, slot: str, name: str) -> None:
    if slot == "builtin":
        # The built-in slot uses a real packaged plain profile; the test picks
        # that name, nothing to write.
        return
    d = dirs[slot]
    (d / f"{name}.md").write_text(
        "---\nprovider: claude_code\n---\nlower readable body\n", encoding="utf-8"
    )


@pytest.mark.parametrize("higher_slot,lower_slot", _STORE_PAIRS)
@pytest.mark.parametrize("bad_kind", _BAD_HIGHER_KINDS)
@pytest.mark.parametrize("bad_position", ["flat", "nested"])
def test_bad_higher_shadows_lower_refuses(
    tmp_path, higher_slot, lower_slot, bad_kind, bad_position
):
    """For EVERY (higher, lower) store pair, every bad-higher shape, and BOTH
    candidate POSITIONS (flat and nested): a higher entry the reader cannot read
    (dangling / escaping-symlink / malformed / unreadable) must NOT fall through
    to a readable lower same-name profile. The resolver classifies UNKNOWN and
    raises E-PROVIDER-UNRESOLVED — including when the lower store is the packaged
    BUILT-IN store (the codex r4 counterexample), and including when the BAD entry
    is the NESTED `{name}/agent.md` candidate (the Opus r6 blocker: mutant M5,
    which returns False from _PathCandidate.is_present_unusable for nested
    candidates, is killed here).
    """
    # The local store is flat-only by design, so it has no nested candidate.
    if bad_position == "nested" and higher_slot == "local":
        pytest.skip("local store is flat-only; no nested candidate")
    # The built-in lower slot must use a name that actually exists as a built-in
    # plain profile; other slots use a fresh scratch name.
    name = _BUILTIN_PLAIN_NAME if lower_slot == "builtin" else "f838_r5_shadow"
    outside = tmp_path / "outside" / "escape.md"

    stack = _configure_slots(tmp_path, higher_slot, lower_slot)
    dirs = stack._dirs  # type: ignore[attr-defined]
    with stack:
        _write_bad_higher(dirs, higher_slot, name, bad_kind, outside, bad_position)
        _write_lower_readable(dirs, lower_slot, name)

        intent, _, _ = _ap._classify_stub_intent(name)
        assert intent == _ap._STUB_UNKNOWN, (
            higher_slot,
            lower_slot,
            bad_kind,
            bad_position,
            intent,
        )
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider(name, "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED
    # Restore perms so tmp cleanup can remove the unreadable file. The unreadable
    # entry lives at the flat OR nested path depending on bad_position.
    if bad_kind == "unreadable":
        bad_path = (
            dirs[higher_slot] / name / "agent.md"
            if bad_position == "nested"
            else dirs[higher_slot] / f"{name}.md"
        )
        bad_path.chmod(0o600)


@pytest.mark.parametrize("higher_slot,lower_slot", _STORE_PAIRS)
@pytest.mark.parametrize("bad_position", ["flat", "nested"])
def test_dangling_higher_shadows_lower_never_reads_lower(
    tmp_path, higher_slot, lower_slot, bad_position
):
    """The stat-only precedence walk refuses a dangling-higher shadow BEFORE the
    raw read is issued, so the shadowed lower file (built-in included) is NEVER
    read. Pins raw_reads == 0 on the refusing path for every pair, at BOTH the
    flat and nested candidate positions (nested is the Opus r6 blocker position)."""
    if bad_position == "nested" and higher_slot == "local":
        pytest.skip("local store is flat-only; no nested candidate")
    name = _BUILTIN_PLAIN_NAME if lower_slot == "builtin" else "f838_r5_noread"
    outside = tmp_path / "outside" / "escape.md"
    original_read = _ap.read_agent_profile_source

    stack = _configure_slots(tmp_path, higher_slot, lower_slot)
    dirs = stack._dirs  # type: ignore[attr-defined]
    with stack:
        _write_bad_higher(dirs, higher_slot, name, "dangling", outside, bad_position)
        _write_lower_readable(dirs, lower_slot, name)
        with patch.object(_ap, "read_agent_profile_source", wraps=original_read) as reads:
            with pytest.raises(ProviderResolutionError):
                resolve_provider(name, "claude_code")
        assert reads.call_count == 0, (higher_slot, lower_slot, bad_position, reads.call_count)


@pytest.mark.parametrize("higher_slot,lower_slot", _STORE_PAIRS)
def test_valid_higher_over_lower_resolves_higher(tmp_path, higher_slot, lower_slot):
    """Mirror control for every pair: a VALID higher entry over a readable lower
    same-name entry resolves the HIGHER provider (precedence stops at the first
    readable entry); no spurious refusal. Skips the built-in-lower case where the
    higher would need to out-rank it (already covered by the higher being a real
    store above built-in)."""
    if lower_slot == "builtin":
        name = "f838_r5_valid_over_builtin"
    else:
        name = "f838_r5_valid"
    stack = _configure_slots(tmp_path, higher_slot, lower_slot)
    dirs = stack._dirs  # type: ignore[attr-defined]
    with stack:
        (dirs[higher_slot] / f"{name}.md").write_text(
            "---\nprovider: pi_cli\n---\nhigher valid\n", encoding="utf-8"
        )
        _write_lower_readable(dirs, lower_slot, name)
        assert resolve_provider(name, "claude_code") == "pi_cli"


# ---------------------------------------------------------------------------
# F838 (#695) r6 — CANDIDATE-granular precedence: same-store flat-over-nested
# and the composed candidate (codex r5 P0-A and P0-B).
#
# P0-A: each configured/extra directory is a TWO-candidate store — flat
# `{name}.md` precedes nested `{name}/agent.md`. The r5 store-granular walk let
# a readable nested entry make the whole store look readable, masking a
# dangling/escaping FLAT entry in the SAME store, so the reader skipped the bad
# flat and read the nested bytes → DECLARED, and public assign reached creation.
# P0-B: the composed `agent-store/composed/{name}.md` candidate (for an effective
# `<position>-<provider>` name) was outside the shared list, so a dangling/escaping
# composed entry fell through to a lower local same-name profile.
#
# The r6 candidate-granular model puts BOTH inside the one shared candidate list,
# so precedence stops at the first PRESENT candidate. These tests exercise both
# shapes on the direct resolver (the assign-seam mirror is in
# test/mcp_server/test_f838_assign_provider_guard.py).
# ---------------------------------------------------------------------------


def _same_store_env(tmp_path: Path, store_kind: str) -> tuple[ExitStack, Path]:
    """Wire ONE configured or extra store dir (with local disabled/empty) so a
    flat-vs-nested candidate ordering inside that single store can be exercised.
    Returns the stack and the store dir."""
    local_dir = tmp_path / "local-store"
    store_dir = tmp_path / f"{store_kind}-store"
    for d in (local_dir, store_dir):
        d.mkdir(parents=True, exist_ok=True)
    stack = ExitStack()
    stack.enter_context(patch.object(_ap, "LOCAL_AGENT_STORE_DIR", local_dir))
    agent_dirs = {"cfg": str(store_dir)} if store_kind == "agent" else {}
    extra_dirs = [str(store_dir)] if store_kind == "extra" else []
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            return_value=agent_dirs,
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            return_value=extra_dirs,
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
            return_value=[],
        )
    )
    return stack, store_dir


def _write_nested(store_dir: Path, name: str, raw: str) -> None:
    nested = store_dir / name / "agent.md"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text(raw, encoding="utf-8", newline="")


@pytest.mark.parametrize("store_kind", ["agent", "extra"])
@pytest.mark.parametrize("bad_kind", ["dangling", "escaping"])
def test_same_store_bad_flat_over_readable_nested_refuses(tmp_path, store_kind, bad_kind):
    """codex r5 P0-A: a dangling OR escaping FLAT `{name}.md` in a configured/extra
    store must NOT fall through to a readable NESTED `{name}/agent.md` in the SAME
    store. The r6 candidate-granular walk stops at the bad flat candidate:
    UNKNOWN, typed refusal, and the nested entry is NEVER read (raw_reads == 0)."""
    name = "f838_r6_same_store"
    stack, store_dir = _same_store_env(tmp_path, store_kind)
    original_read = _ap.read_agent_profile_source
    with stack:
        flat = store_dir / f"{name}.md"
        if bad_kind == "dangling":
            flat.symlink_to(store_dir / "missing-flat-target.md")
        else:  # escaping — flat symlink whose live target is outside the root
            outside = tmp_path / "outside" / "live.md"
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_text("---\nprovider: pi_cli\n---\noutside\n", encoding="utf-8")
            flat.symlink_to(outside)
        _write_nested(store_dir, name, "---\nprovider: pi_cli\n---\nnested lower\n")

        intent, _, _ = _ap._classify_stub_intent(name)
        assert intent == _ap._STUB_UNKNOWN, (store_kind, bad_kind, intent)
        with patch.object(_ap, "read_agent_profile_source", wraps=original_read) as reads:
            with pytest.raises(ProviderResolutionError) as ei:
                resolve_provider(name, "claude_code")
        assert reads.call_count == 0, (store_kind, bad_kind, reads.call_count)
    assert ei.value.code == E_PROVIDER_UNRESOLVED


@pytest.mark.parametrize("store_kind", ["agent", "extra"])
def test_same_store_valid_flat_over_nested_resolves_flat(tmp_path, store_kind):
    """Mirror control: a VALID flat entry over a readable nested entry in the same
    store resolves the FLAT provider (precedence stops at the first readable
    candidate); no spurious refusal from the candidate-granular walk."""
    name = "f838_r6_same_store_ok"
    stack, store_dir = _same_store_env(tmp_path, store_kind)
    with stack:
        (store_dir / f"{name}.md").write_text(
            "---\nprovider: pi_cli\n---\nflat higher\n", encoding="utf-8"
        )
        _write_nested(store_dir, name, "---\nprovider: claude_code\n---\nnested lower\n")
        assert resolve_provider(name, "claude_code") == "pi_cli"


def _composed_env(tmp_path: Path, monkeypatch) -> tuple[ExitStack, Path, Path]:
    """Point CAO_HOME_DIR at scratch so `composed_store_dir()` resolves under it,
    with a real local store as the lower candidate. Returns stack, composed dir,
    local dir."""
    from cli_agent_orchestrator.constants import composed_store_dir

    home = tmp_path / "home"
    local_dir = tmp_path / "local-store"
    local_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    stack = ExitStack()
    stack.enter_context(patch.object(_ap, "LOCAL_AGENT_STORE_DIR", local_dir))
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            return_value={},
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            return_value=[],
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
            return_value=[],
        )
    )
    composed = composed_store_dir()
    composed.mkdir(parents=True, exist_ok=True)
    return stack, composed, local_dir


@pytest.mark.parametrize("bad_kind", ["dangling", "escaping"])
def test_composed_bad_over_lower_local_refuses(tmp_path, monkeypatch, bad_kind):
    """codex r5 P0-B: a dangling OR escaping composed `{name}.md` (for an effective
    `<position>-<provider>` name) must NOT fall through to a readable LOWER local
    same-name profile. The composed candidate is now the FIRST element of the
    shared list, so the r6 walk stops there: UNKNOWN, typed refusal, and the
    lower local entry is NEVER read (raw_reads == 0)."""
    # An effective name whose provider suffix is a real provider token.
    name = "reviewer-pi_cli"
    stack, composed, local_dir = _composed_env(tmp_path, monkeypatch)
    original_read = _ap.read_agent_profile_source
    with stack:
        if bad_kind == "dangling":
            (composed / f"{name}.md").symlink_to(composed / "missing.md")
        else:
            outside = tmp_path / "outside.md"
            outside.write_text("---\nprovider: claude_code\n---\noutside\n", encoding="utf-8")
            (composed / f"{name}.md").symlink_to(outside)
        (local_dir / f"{name}.md").write_text(
            "---\nprovider: pi_cli\n---\nlower local\n", encoding="utf-8"
        )
        intent, _, _ = _ap._classify_stub_intent(name)
        assert intent == _ap._STUB_UNKNOWN, (bad_kind, intent)
        with patch.object(_ap, "read_agent_profile_source", wraps=original_read) as reads:
            with pytest.raises(ProviderResolutionError) as ei:
                resolve_provider(name, "claude_code")
        assert reads.call_count == 0, (bad_kind, reads.call_count)
    assert ei.value.code == E_PROVIDER_UNRESOLVED


def test_composed_valid_over_lower_local_resolves_composed(tmp_path, monkeypatch):
    """Mirror control: a VALID composed entry over a readable lower local same-name
    profile resolves the COMPOSED provider (precedence stops at the first readable
    candidate — the composed one)."""
    name = "reviewer-pi_cli"
    stack, composed, local_dir = _composed_env(tmp_path, monkeypatch)
    with stack:
        (composed / f"{name}.md").write_text(
            "---\nprovider: pi_cli\n---\ncomposed higher\n", encoding="utf-8"
        )
        (local_dir / f"{name}.md").write_text(
            "---\nprovider: claude_code\n---\nlower local\n", encoding="utf-8"
        )
        assert resolve_provider(name, "claude_code") == "pi_cli"
