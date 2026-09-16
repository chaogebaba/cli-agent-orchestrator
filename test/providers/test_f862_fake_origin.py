"""AC-25 fake-origin receipt oracle, independent of browser event claims."""

from __future__ import annotations

import hashlib
from test.fixtures.chatgpt_web_fake_origin import DETERMINISTIC_PAGE, FakeOrigin

import pytest
import requests

pytestmark = pytest.mark.unit


def test_deterministic_page_issues_the_routed_conversation_post(tmp_path):
    with FakeOrigin(tmp_path / "tls") as origin:
        response = requests.get(origin.origin, verify=str(origin.cert_path), timeout=3)
    assert response.status_code == 200
    assert response.content == DETERMINISTIC_PAGE
    assert b"/backend-api/f/conversation" in response.content
    assert b"window.issueConversation" in response.content


def test_receipt_ledger_proves_origin_receipt_even_when_response_resets(tmp_path):
    body = b'{"messages":[{"id":"oracle"}]}'
    with FakeOrigin(tmp_path / "tls", response_mode="reset") as origin:
        with pytest.raises(requests.RequestException):
            requests.post(
                origin.origin + "/backend-api/f/conversation",
                data=body,
                verify=str(origin.cert_path),
                timeout=3,
            )
        receipts = origin.ledger.snapshot()

    # This independent server-side fact is the discriminator a Playwright-only
    # requestfailed mutant lacks: the request reached origin before reset.
    assert len(receipts) == 1
    assert receipts[0].path == "/backend-api/f/conversation"
    assert receipts[0].body_sha256 == hashlib.sha256(body).hexdigest()
    assert receipts[0].response_mode == "reset"


def test_complete_and_reset_arms_have_distinct_server_side_dispositions(tmp_path):
    body = b"{}"
    with FakeOrigin(tmp_path / "complete", response_mode="complete") as complete:
        response = requests.post(
            complete.origin + "/backend-api/f/conversation",
            data=body,
            verify=str(complete.cert_path),
            timeout=3,
        )
        complete_rows = complete.ledger.snapshot()
    with FakeOrigin(tmp_path / "reset", response_mode="reset") as reset:
        with pytest.raises(requests.RequestException):
            requests.post(
                reset.origin + "/backend-api/f/conversation",
                data=body,
                verify=str(reset.cert_path),
                timeout=3,
            )
        reset_rows = reset.ledger.snapshot()

    assert response.content.endswith(b"data: [DONE]\n\n")
    assert complete_rows[0].response_mode == "complete"
    assert reset_rows[0].response_mode == "reset"
