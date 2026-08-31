"""F642 deferred follow-up leg (blueprint §7 / D3 / AC18): the `cao messages
list --claim hook` flag wired through the service and CLI.

The storage-layer claim primitive (``hook_claim_ids``) is already covered by
test/clients/test_f642_delivery_ledger.py (test_ac18_*). This file covers the
DEFERRED LEG the blueprint §7 names as remaining coordination: the
``list_messages(..., claim=…)`` service integration and the
``cao messages list --claim`` CLI flag that lets the ROOT-repo drain hook turn
its READ into a CLAIM in one call — so it prints nothing for an id another
carrier already carried (#488's first complaint), while still seeing ids it won.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    claim_emission,
    create_inbox_message,
    create_terminal,
)
from cli_agent_orchestrator.clients.delivery_ledger import Carrier
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.mailbox_service import MailboxDomainError, list_messages


@pytest.fixture
def db_env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    # mailbox_service imports SessionLocal by name at call time from database,
    # but to be safe patch the symbol it uses too.
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions, raising=False)
    database.clear_terminal_metadata_cache()
    create_terminal("sup", "cao-t", "w-sup", "claude_code")
    create_terminal("wrk", "cao-t", "w-wrk", "claude_code")
    return sessions


def _ids(result) -> list[int]:
    return [int(item["id"]) for item in result["items"]]


# ── service integration: claim filters the page to won ids ───────────────────


def test_list_claim_hook_filters_natively_claimed_id(db_env):
    """AC18 at the service layer: an id already claimed natively is omitted from
    the --claim hook listing; an unclaimed id is won and returned."""
    claimed = create_inbox_message("sup", "wrk", "already-native")
    unclaimed = create_inbox_message("sup", "wrk", "fresh")
    with db_env() as db:
        assert claim_emission(db, message_id=claimed.id, carrier=Carrier.NATIVE) is True
        db.commit()

    result = list_messages("wrk", claim="hook")
    assert _ids(result) == [unclaimed.id]


def test_list_claim_hook_wins_and_persists_claim(db_env):
    """The won id is returned AND its hook emission (claim) persists — a second
    --claim hook read returns nothing (the claim is durable, not a re-print)."""
    msg = create_inbox_message("sup", "wrk", "x")

    first = list_messages("wrk", claim="hook")
    assert _ids(first) == [msg.id]

    # Second read: the hook already holds the claim → nothing new to print.
    second = list_messages("wrk", claim="hook")
    assert _ids(second) == []


def test_list_without_claim_is_pure_read(db_env):
    """No --claim → the listing is unchanged and creates NO emission row (GET
    semantics preserved); the id lists on every call."""
    msg = create_inbox_message("sup", "wrk", "x")

    r1 = list_messages("wrk")
    r2 = list_messages("wrk")
    assert _ids(r1) == [msg.id]
    assert _ids(r2) == [msg.id]  # still there — no claim consumed it
    # No hook emission row was created.
    from cli_agent_orchestrator.clients.database import DeliveryEmissionModel

    with db_env() as db:
        n = (
            db.query(DeliveryEmissionModel)
            .filter(DeliveryEmissionModel.carrier == Carrier.HOOK.value)
            .count()
        )
    assert n == 0


def test_list_claim_unsupported_carrier_rejected(db_env):
    """Only the hook read-as-claim path is defined by §7/D3; other carriers claim
    on their own emit path, so the service refuses a non-hook claim."""
    create_inbox_message("sup", "wrk", "x")
    with pytest.raises(MailboxDomainError) as exc:
        list_messages("wrk", claim="native")
    assert "claim" in str(exc.value).lower()


def test_list_claim_mutant_no_filter_reprints_claimed(db_env, monkeypatch):
    """MUTANT: make the service NOT filter to won ids (return all listed) → the
    natively-claimed id reappears in the hook listing, reproducing #488's
    duplicate print. Proves the filter is load-bearing."""
    claimed = create_inbox_message("sup", "wrk", "already-native")
    unclaimed = create_inbox_message("sup", "wrk", "fresh")
    with db_env() as db:
        assert claim_emission(db, message_id=claimed.id, carrier=Carrier.NATIVE) is True
        db.commit()

    # Simulate the mutant: hook_claim_ids returns every candidate (no exclusion).
    import cli_agent_orchestrator.clients.database as db_mod

    def _mutant_claim(db, *, candidate_ids):
        return list(candidate_ids)

    monkeypatch.setattr(db_mod, "hook_claim_ids", _mutant_claim)
    result = list_messages("wrk", claim="hook")
    # Under the mutant BOTH ids print, including the natively-carried one.
    assert claimed.id in _ids(result)
    assert unclaimed.id in _ids(result)


# ── CLI flag: --claim passes through to the request ──────────────────────────


def test_cli_list_claim_forwards_param():
    """`cao messages list --to X --claim hook` forwards claim=hook to GET /messages."""
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.main import cli

    class _Resp:
        status_code = 200

        def json(self):
            return {"items": [], "next_after_id": None, "has_more": False}

    with patch(
        "cli_agent_orchestrator.cli.commands.messages.cao_http.get",
        return_value=_Resp(),
    ) as get:
        result = CliRunner().invoke(
            cli, ["messages", "list", "--to", "abcdef12", "--claim", "hook"]
        )
    assert result.exit_code == 0, result.output
    _, kwargs = get.call_args
    assert kwargs["params"]["claim"] == "hook"


def test_cli_list_claim_rejects_unknown_carrier():
    """--claim only accepts the defined vocab (hook); an unknown value is a usage
    error, caught by click before any request."""
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.main import cli

    result = CliRunner().invoke(
        cli, ["messages", "list", "--to", "abcdef12", "--claim", "bogus"]
    )
    assert result.exit_code != 0
    assert "bogus" in result.output or "Invalid value" in result.output


def test_cli_list_without_claim_omits_param():
    """No --claim → no claim key in the request params (opt-in only)."""
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.main import cli

    class _Resp:
        status_code = 200

        def json(self):
            return {"items": [], "next_after_id": None, "has_more": False}

    with patch(
        "cli_agent_orchestrator.cli.commands.messages.cao_http.get",
        return_value=_Resp(),
    ) as get:
        result = CliRunner().invoke(cli, ["messages", "list", "--to", "abcdef12"])
    assert result.exit_code == 0, result.output
    _, kwargs = get.call_args
    assert "claim" not in kwargs["params"]
