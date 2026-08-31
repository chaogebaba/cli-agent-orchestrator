"""F642 follow-up (gate SHOULD): the ``GET /messages?claim=...`` write-gate.

``--claim`` MUTATES (it inserts a ``delivery_emission`` claim, §7/D3), so the
``list_messages_endpoint`` requires write/admin scope when ``claim`` is set and
returns 403 ``claim_requires_write`` otherwise. The gate had ZERO test coverage
— a reviewer disabled it in prod and the suite stayed green. These tests bind it:

  * a READ-scope token + ``claim`` → 403, and NO claim is performed (the service
    is never called), so a read-only caller cannot mutate via the read path;
  * a WRITE-scope token + ``claim`` → the gate passes and the endpoint calls
    through to ``list_messages(claim=...)``;
  * ADMIN likewise passes; and a plain read (no ``claim``) is unaffected by the
    gate under read scope.

Tested at the endpoint-function layer (calling ``list_messages_endpoint``
directly with an explicit ``_scopes`` list), with the service stubbed — so the
assertion is precisely "does the scope gate admit/deny", not DB behaviour.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from cli_agent_orchestrator.api.main import list_messages_endpoint
from cli_agent_orchestrator.security.auth import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE

_RECEIVER = "abcdef12"
_SENTINEL = {"items": [], "next_after_id": None, "has_more": False}


def _call(scopes, *, claim):
    """Invoke the endpoint directly with an explicit scope list."""
    return asyncio.run(
        list_messages_endpoint(to=_RECEIVER, claim=claim, _scopes=scopes)
    )


def test_read_scope_with_claim_is_403_and_does_not_claim():
    """READ scope + claim → 403 claim_requires_write; the service is NEVER called
    (no mutation leaks past the gate)."""
    with patch(
        "cli_agent_orchestrator.services.mailbox_service.list_messages"
    ) as list_messages:
        with pytest.raises(HTTPException) as exc:
            _call([SCOPE_READ], claim="hook")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "claim_requires_write"
    list_messages.assert_not_called()


def test_write_scope_with_claim_passes_gate_and_claims():
    """WRITE scope + claim → gate admits; the endpoint calls through with claim."""
    with patch(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        return_value=_SENTINEL,
    ) as list_messages:
        result = _call([SCOPE_WRITE], claim="hook")
    assert result == _SENTINEL
    _, kwargs = list_messages.call_args
    assert kwargs["claim"] == "hook"


def test_admin_scope_with_claim_passes_gate():
    """ADMIN scope + claim → gate admits (admin is a superset of write here)."""
    with patch(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        return_value=_SENTINEL,
    ) as list_messages:
        result = _call([SCOPE_ADMIN], claim="hook")
    assert result == _SENTINEL
    _, kwargs = list_messages.call_args
    assert kwargs["claim"] == "hook"


def test_read_scope_without_claim_is_unaffected():
    """A plain read (no claim) under READ scope is untouched by the write-gate:
    the endpoint calls through and passes NO claim kwarg."""
    with patch(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        return_value=_SENTINEL,
    ) as list_messages:
        result = _call([SCOPE_READ], claim=None)
    assert result == _SENTINEL
    _, kwargs = list_messages.call_args
    assert "claim" not in kwargs


def test_mutant_gate_removed_lets_read_scope_claim():
    """MUTANT witness: this is what the reviewer's disabled-gate build did — a
    READ token would claim. We assert the LIVE gate blocks it (the inverse of the
    mutant), so a regression that drops the gate flips this test.

    Concretely: under the live gate a READ+claim NEVER reaches the service; if the
    gate were removed it WOULD. We assert the service is not called (gate live).
    """
    with patch(
        "cli_agent_orchestrator.services.mailbox_service.list_messages",
        return_value=_SENTINEL,
    ) as list_messages:
        with pytest.raises(HTTPException):
            _call([SCOPE_READ], claim="hook")
    list_messages.assert_not_called()
