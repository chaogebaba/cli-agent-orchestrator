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
