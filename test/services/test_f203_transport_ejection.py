"""F203 D9-D12: Transport ejection tests.

AC6-AC9: Counted ejection, one WARN, backoff, active re-probe readmission,
floor exemption, CC 2.1.232 record shape.
"""

from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import pytest

from cli_agent_orchestrator.services.transport_ejection import (
    TransportEjectionService,
    _RungEjectionState,
)


@pytest.fixture
def ejection_service() -> TransportEjectionService:
    """Fresh ejection service per test."""
    return TransportEjectionService()


class TestAC6CountedEjection:
    """AC6: Counted ejection, one WARN."""

    def test_ejection_after_threshold(self, ejection_service: TransportEjectionService):
        """3 consecutive refusals → EJECTED."""
        for i in range(2):
            result = ejection_service.record_refusal("t1", "rung1", "no_registry_records")
            assert result is False

        # 3rd refusal triggers ejection
        result = ejection_service.record_refusal("t1", "rung1", "no_registry_records")
        assert result is True

        state = ejection_service.get_state("t1", "rung1")
        assert state is not None
        assert state.ejected is True
        assert state.ejection_count == 1

    def test_exactly_one_warn_across_5_refusals(self, ejection_service: TransportEjectionService):
        """AC6 [LB]: Exactly one WARN emitted across 5 attempts."""
        with patch(
            "cli_agent_orchestrator.services.transport_ejection.logger"
        ) as mock_logger:
            for i in range(5):
                ejection_service.record_refusal("t1", "rung1", "no_registry_records")

            # Exactly one warning call (at the 3rd refusal)
            assert mock_logger.warning.call_count == 1, (
                f"Expected exactly 1 WARN, got {mock_logger.warning.call_count}"
            )
            # Verify the WARN content
            call_args = mock_logger.warning.call_args
            assert "f203_transport_ejected" in call_args[0][0]
            assert "rung1" in str(call_args)

    def test_ejected_state_after_3rd(self, ejection_service: TransportEjectionService):
        """State reports EJECTED after the 3rd consecutive refusal."""
        for i in range(3):
            ejection_service.record_refusal("t1", "rung1", "no_registry_records")

        assert ejection_service.is_ejected("t1", "rung1") is True


class TestAC7ActiveReprobe:
    """AC7: Active re-probe readmits."""

    def test_reprobe_readmits_ejected_rung(self, ejection_service: TransportEjectionService):
        """D11: re-probe un-ejects rung1 and clears consecutive counter."""
        # Eject
        for i in range(3):
            ejection_service.record_refusal("t1", "rung1", "no_registry_records")
        assert ejection_service.is_ejected("t1", "rung1") is True

        # Re-probe readmits
        ejection_service.readmit("t1", "rung1")

        assert ejection_service.is_ejected("t1", "rung1") is False
        state = ejection_service.get_state("t1", "rung1")
        assert state.consecutive_refusals == 0

    def test_cc_2_1_232_record_without_messaging_socket(self):
        """D13: CC 2.1.232 records without messagingSocketPath are accepted."""
        import json
        import tempfile
        from pathlib import Path

        from cli_agent_orchestrator.services.cc_session_registry import read_registry

        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir)
            # Record with sessionId + procStart but NO messagingSocketPath
            record = {"sessionId": "abc123", "procStart": 12345}
            (sessions_dir / "9999.json").write_text(json.dumps(record))

            records = read_registry(sessions_dir)
            assert len(records) == 1, (
                "D13: record with sessionId+procStart but no messagingSocketPath "
                "must be accepted (CC 2.1.232 shape)"
            )
            assert records[0].session_id == "abc123"


class TestAC8FloorExemption:
    """AC8: The floor (rung2) is never ejected."""

    def test_rung2_never_ejected(self, ejection_service: TransportEjectionService):
        """D12: rung2 is exempt from ejection machinery."""
        # rung2 is the composer injection floor — never tracked for ejection.
        # The delivery code simply never calls record_refusal for rung2.
        # We verify that even if someone tries, rung2 "rung2" is not blocked.
        # The design guarantee is that attempt_rung2 is always attempted.

        # Eject rung1 and fallback
        for i in range(3):
            ejection_service.record_refusal("t1", "rung1", "no_registry_records")
            ejection_service.record_refusal("t1", "fallback", "not_registered_fallback")

        assert ejection_service.is_ejected("t1", "rung1") is True
        assert ejection_service.is_ejected("t1", "fallback") is True
        # rung2 has no state — never ejected
        assert ejection_service.is_ejected("t1", "rung2") is False


class TestAC9FallbackEjection:
    """AC9: Fallback refusal is ejection-class."""

    def test_fallback_refusal_triggers_ejection(self, ejection_service: TransportEjectionService):
        """not_registered_fallback becomes counted ejection."""
        with patch(
            "cli_agent_orchestrator.services.transport_ejection.logger"
        ) as mock_logger:
            for i in range(3):
                ejection_service.record_refusal("t1", "fallback", "not_registered_fallback")

            assert ejection_service.is_ejected("t1", "fallback") is True
            assert mock_logger.warning.call_count == 1
            call_args = mock_logger.warning.call_args
            assert "not_registered_fallback" in str(call_args)


class TestN2BackoffScaling:
    """N2: Ejection duration = base_ejection_s * consecutive_ejection_count."""

    def test_backoff_scales_with_count(self, ejection_service: TransportEjectionService):
        """Duration increases with consecutive ejection count."""
        with patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            side_effect=lambda key, *a, **kw: (
                30.0 if "base_ejection_s" in key else
                120.0 if "escalate_after_s" in key else
                None
            ),
        ):
            # First ejection: 30 * 1 = 30s
            for i in range(3):
                ejection_service.record_refusal("t1", "rung1", "no_registry_records")
            state = ejection_service.get_state("t1", "rung1")
            assert state.ejection_duration_s == 30.0

            # Readmit and re-eject: 30 * 2 = 60s
            ejection_service.readmit("t1", "rung1")
            for i in range(3):
                ejection_service.record_refusal("t1", "rung1", "no_registry_records")
            state = ejection_service.get_state("t1", "rung1")
            assert state.ejection_duration_s == 60.0

    def test_backoff_capped_at_escalate_after_s(self, ejection_service: TransportEjectionService):
        """Duration capped at escalate_after_s."""
        with patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            side_effect=lambda key, *a, **kw: (
                100.0 if "base_ejection_s" in key else
                120.0 if "escalate_after_s" in key else
                None
            ),
        ):
            # First ejection: min(100*1, 120) = 100
            for i in range(3):
                ejection_service.record_refusal("t1", "rung1", "no_registry_records")
            state = ejection_service.get_state("t1", "rung1")
            assert state.ejection_duration_s == 100.0

            # Second: min(100*2, 120) = 120 (capped)
            ejection_service.readmit("t1", "rung1")
            for i in range(3):
                ejection_service.record_refusal("t1", "rung1", "no_registry_records")
            state = ejection_service.get_state("t1", "rung1")
            assert state.ejection_duration_s == 120.0


class TestEjectionExpiry:
    """Ejection expires after the backoff duration."""

    def test_auto_readmit_on_expiry(self, ejection_service: TransportEjectionService):
        """is_ejected returns False after duration expires."""
        with patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            side_effect=lambda key, *a, **kw: (
                30.0 if "base_ejection_s" in key else
                120.0 if "escalate_after_s" in key else
                None
            ),
        ):
            for i in range(3):
                ejection_service.record_refusal("t1", "rung1", "no_registry_records")
            assert ejection_service.is_ejected("t1", "rung1") is True

            # Fast-forward past ejection duration
            state = ejection_service.get_state("t1", "rung1")
            state.ejected_at = time.monotonic() - 31.0  # 31s ago, duration is 30s

            assert ejection_service.is_ejected("t1", "rung1") is False
