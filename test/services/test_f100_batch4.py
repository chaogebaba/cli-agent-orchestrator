"""Tests for f100 batch 4: C2 async seed, C5 codex MCP-interrupt, B4 notice reason."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.services import terminal_service

# ---------------------------------------------------------------------------
# C2: seed_resume_bootstrap wraps seed_resume_identity in asyncio.to_thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSeedResumeBootstrapAsync:
    """seed_resume_bootstrap must call seed_resume_identity via asyncio.to_thread."""

    async def test_seed_resume_identity_called_via_to_thread(self, monkeypatch):
        """The blocking seed_resume_identity call runs in a thread."""
        fake_provider = SimpleNamespace(
            supports_seed_resume_identity=True,
            seed_resume_identity=MagicMock(return_value="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
        )
        monkeypatch.setattr(terminal_service, "get_provider_class", lambda _p: fake_provider)

        to_thread_called_with = []
        original_to_thread = asyncio.to_thread

        async def mock_to_thread(func, *args):
            to_thread_called_with.append((func, args))
            return func(*args)

        with patch("asyncio.to_thread", side_effect=mock_to_thread):
            result = await terminal_service.seed_resume_bootstrap("dev", "codex", "/work")

        assert result is not None
        assert result.session_uuid == "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        assert result.mode == "resume"
        assert len(to_thread_called_with) == 1
        assert to_thread_called_with[0][0] is fake_provider.seed_resume_identity
        assert to_thread_called_with[0][1] == ("/work", "dev")

    async def test_seed_resume_bootstrap_returns_none_for_unsupported(self, monkeypatch):
        """Providers without seed support return None immediately."""
        fake_provider = SimpleNamespace(supports_seed_resume_identity=False)
        monkeypatch.setattr(terminal_service, "get_provider_class", lambda _p: fake_provider)

        result = await terminal_service.seed_resume_bootstrap("dev", "grok_cli", "/work")
        assert result is None


# ---------------------------------------------------------------------------
# B4: _notice_text includes optional reason parameter
# ---------------------------------------------------------------------------


class TestNoticeTextReason:
    """_notice_text should include reason text when provided."""

    def test_notice_text_without_reason(self):
        """Default behavior: no reason field in output."""
        result = terminal_service._notice_text(
            code="deferred_init_internal",
            deadline_s=60.0,
            token="tok12345",
            worker="w1234567",
            profile="developer",
            provider="codex",
        )
        assert "reason=" not in result
        assert "deferred_init_internal" in result

    def test_notice_text_with_reason(self):
        """When reason is provided, it appears in output."""
        result = terminal_service._notice_text(
            code="deferred_init_internal",
            deadline_s=60.0,
            token="tok12345",
            worker="w1234567",
            profile="developer",
            provider="codex",
            reason="RuntimeError('seed_timeout')",
        )
        assert "reason=RuntimeError('seed_timeout')" in result

    def test_notice_text_reason_truncated_at_caller(self):
        """Caller passes repr(e)[:200]; reason fits the notice."""
        long_reason = repr(RuntimeError("x" * 300))[:200]
        result = terminal_service._notice_text(
            code="deferred_init_internal",
            deadline_s=90.0,
            token="tok99999",
            worker="w9999999",
            profile="developer",
            provider="codex",
            reason=long_reason,
        )
        assert "reason=" in result
        assert len(long_reason) <= 200


# ---------------------------------------------------------------------------
# B4: _claim_and_settle_deferred_failure passes reason to _notice_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestClaimSettlePassesReason:
    """_claim_and_settle_deferred_failure propagates reason to the notice."""

    async def test_reason_appears_in_claim_notice_kwarg(self, monkeypatch):
        """The notice passed to claim_deferred_init_failure contains reason text."""
        snapshot = {
            "caller_id": "caller1",
            "agent_profile": "developer",
            "provider": "codex",
            "init_deadline_s": 60,
            "init_owner_epoch": "12345678-1234-4123-8123-123456789012",
        }
        claim_kwargs_captured = []

        async def fake_tracked_blocking(_tid, _gen, _kind, stage, func, *args, **kwargs):
            if stage == "h3_claim":
                # Capture kwargs passed to claim_deferred_init_failure
                claim_kwargs_captured.append(kwargs)
                # Return early with row_missing to stop further processing
                return {"status": "row_missing"}, None
            return None, None

        monkeypatch.setattr(terminal_service, "_tracked_blocking", fake_tracked_blocking)

        await terminal_service._claim_and_settle_deferred_failure(
            "w1234567",
            "gen-1",
            snapshot,
            "deferred_init_internal",
            None,  # registry
            None,  # uuid_lease_token
            reason="RuntimeError('boom')",
        )

        # _tracked_blocking is called with positional + kwargs; the kwargs include
        # caller_id, failure_token, notice which are forwarded to claim_deferred_init_failure.
        # The notice= kwarg should contain the reason text.
        assert len(claim_kwargs_captured) == 1
        notice_value = claim_kwargs_captured[0].get("notice", "")
        assert "reason=RuntimeError('boom')" in notice_value
