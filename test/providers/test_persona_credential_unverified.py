"""F582 D18 (F575 #432, AC5) — persona seats ship ``persona_unverified``.

FINDING (build report Deviations): D18's fail-closed credential branch
(``E-PERSONA-UNAUTHENTICATED`` for a persona plane with no credential) requires
the credential KEY NAME confirmed against a live, known-authenticated persona
``.claude.json``. A keys-only probe (2026-08-30) of the live production plane
``~/.claude/.claude.json`` found NO ``oauthAccount`` key (8 top-level keys; only
``userID`` is auth-like). So the blueprint's cited marker is UNVERIFIED on this
build, and per D18's own rule the shipped state is ``persona_unverified``, never
the fail-closed code. This module pins that shipped behaviour and guards the
finding (``_PERSONA_CREDENTIAL_KEY_CONFIRMED is False``). The fail-closed branch
is BUILT but GATED so it is not dead code — the confirmed-key arms below prove
it fires correctly once a build flips the flag.
"""

import json

from cli_agent_orchestrator.providers.claude_code import (
    PERSONA_UNAUTHENTICATED,
    PERSONA_UNVERIFIED,
    _persona_credential_present,
    _PERSONA_CREDENTIAL_KEY,
    _PERSONA_CREDENTIAL_KEY_CONFIRMED,
    classify_persona_credential,
)


# ---- The finding: the credential key is NOT confirmed on this build ----------


def test_credential_key_is_unconfirmed_on_this_build():
    """The D18 discriminator (oauthAccount) is unverified — the finding."""
    assert _PERSONA_CREDENTIAL_KEY == "oauthAccount"
    assert _PERSONA_CREDENTIAL_KEY_CONFIRMED is False


# ---- Shipped state: unconfirmed key → persona_unverified, never fail-closed --


def test_unconfirmed_key_missing_credential_is_unverified_not_failclosed():
    # credential ABSENT, key unconfirmed → persona_unverified (NOT E-PERSONA-*)
    assert classify_persona_credential(False) == PERSONA_UNVERIFIED


def test_unconfirmed_key_present_credential_is_unverified():
    # even a "present" probe result is not trusted while the key is unconfirmed
    assert classify_persona_credential(True) == PERSONA_UNVERIFIED


def test_unconfirmed_key_never_yields_failclosed_code():
    for present in (True, False, None):
        assert classify_persona_credential(present) != PERSONA_UNAUTHENTICATED


# ---- Keys-only probe: absent / unparseable → None (never fail-closed) --------


def test_probe_returns_none_when_file_absent(tmp_path):
    assert _persona_credential_present(tmp_path / "nope.json") is None


def test_probe_returns_none_on_unparseable(tmp_path):
    p = tmp_path / ".claude.json"
    p.write_text("{ not json", encoding="utf-8")
    assert _persona_credential_present(p) is None


def test_probe_reads_only_key_presence(tmp_path):
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"oauthAccount": {"email": "x@y.z"}, "userID": "u"}), encoding="utf-8")
    assert _persona_credential_present(p) is True
    p.write_text(json.dumps({"userID": "u"}), encoding="utf-8")
    assert _persona_credential_present(p) is False


# ---- The gated branch is correct once a build CONFIRMS the key (not dead) ----


def test_confirmed_key_absent_credential_fails_closed():
    """When a build confirms the key name, an absent credential fails closed —
    proving the branch is gated, not dead."""
    assert classify_persona_credential(False, key_confirmed=True) == PERSONA_UNAUTHENTICATED


def test_confirmed_key_present_credential_no_marker():
    assert classify_persona_credential(True, key_confirmed=True) == ""


def test_confirmed_key_unreadable_plane_is_unverified_not_failclosed():
    # An unreadable plane is never fail-closed even once the key is confirmed.
    assert classify_persona_credential(None, key_confirmed=True) == PERSONA_UNVERIFIED
