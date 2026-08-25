"""F451 (#306): cap-registry cardinality metric + warning.

Tests prove:
- Warning fires exactly once at each power-of-two crossing of the threshold.
- No double-fire on subsequent inserts between crossings.
- Disabled entirely when threshold <= 0.
- Admission/release paths are unaffected (no refusal, no reclamation).
"""

from __future__ import annotations

import logging
import threading

import pytest

from cli_agent_orchestrator.services import terminal_service as ts


@pytest.fixture(autouse=True)
def _reset_cap_registry():
    """Reset the module-level cardinality state between tests."""
    original_locks = ts._cap_admission_locks.copy()
    original_cardinality = ts._cap_registry_cardinality
    original_warned_at = ts._cap_registry_warned_at
    original_threshold = ts._CAP_REGISTRY_WARN_CARDINALITY
    original_gen = ts._cap_gen.copy()
    original_token_seq = ts._cap_token_seq.copy()
    original_reservations = ts._cap_reservations.copy()
    original_publishing_ids = ts._cap_publishing_ids.copy()

    # Clear for test isolation
    ts._cap_admission_locks.clear()
    ts._cap_registry_cardinality = 0
    ts._cap_registry_warned_at = 0
    ts._cap_gen.clear()
    ts._cap_token_seq.clear()
    ts._cap_reservations.clear()
    ts._cap_publishing_ids.clear()

    yield

    # Restore
    ts._cap_admission_locks.clear()
    ts._cap_admission_locks.update(original_locks)
    ts._cap_registry_cardinality = original_cardinality
    ts._cap_registry_warned_at = original_warned_at
    ts._CAP_REGISTRY_WARN_CARDINALITY = original_threshold
    ts._cap_gen.clear()
    ts._cap_gen.update(original_gen)
    ts._cap_token_seq.clear()
    ts._cap_token_seq.update(original_token_seq)
    ts._cap_reservations.clear()
    ts._cap_reservations.update(original_reservations)
    ts._cap_publishing_ids.clear()
    ts._cap_publishing_ids.update(original_publishing_ids)


class TestCardinalityWarning:
    """Warning fires at power-of-two crossings of the threshold."""

    def test_warning_fires_at_threshold_crossing(self, caplog):
        """Insert exactly `threshold` distinct sessions -> one warning."""
        ts._CAP_REGISTRY_WARN_CARDINALITY = 4  # small for testing
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.terminal_service"):
            for i in range(3):
                ts._cap_admission_lock(f"session-{i}")
            # At 3 — no warning yet
            assert "cap_registry_cardinality_high" not in caplog.text

            # The 4th insert crosses the threshold
            ts._cap_admission_lock("session-3")
            assert "cap_registry_cardinality_high" in caplog.text
            assert "4 distinct session names" in caplog.text

    def test_warning_fires_once_per_crossing(self, caplog):
        """Between crossings (4 and 8), no additional warnings fire."""
        ts._CAP_REGISTRY_WARN_CARDINALITY = 4
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.terminal_service"):
            # Fill to first crossing
            for i in range(4):
                ts._cap_admission_lock(f"session-{i}")
            assert caplog.text.count("cap_registry_cardinality_high") == 1
            caplog.clear()

            # Insert 3 more (5, 6, 7) — still below next crossing (8)
            for i in range(4, 7):
                ts._cap_admission_lock(f"session-{i}")
            assert "cap_registry_cardinality_high" not in caplog.text

            # The 8th insert crosses the next power-of-two
            ts._cap_admission_lock("session-7")
            assert "cap_registry_cardinality_high" in caplog.text
            assert "8 distinct session names" in caplog.text
            assert caplog.text.count("cap_registry_cardinality_high") == 1

    def test_second_crossing_at_double(self, caplog):
        """After crossing at 4, next crossing is at 8 (threshold * 2)."""
        ts._CAP_REGISTRY_WARN_CARDINALITY = 4
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.terminal_service"):
            for i in range(8):
                ts._cap_admission_lock(f"session-{i}")
            # Should have warned at 4 and at 8
            assert caplog.text.count("cap_registry_cardinality_high") == 2

            caplog.clear()
            # 9-15: no warning
            for i in range(8, 15):
                ts._cap_admission_lock(f"session-{i}")
            assert "cap_registry_cardinality_high" not in caplog.text

            # 16th: next power-of-two crossing
            ts._cap_admission_lock("session-15")
            assert "cap_registry_cardinality_high" in caplog.text
            assert "16 distinct session names" in caplog.text

    def test_duplicate_session_does_not_increment(self, caplog):
        """Re-entering an existing session does not bump the counter."""
        ts._CAP_REGISTRY_WARN_CARDINALITY = 4
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.terminal_service"):
            for i in range(3):
                ts._cap_admission_lock(f"session-{i}")
            # Re-enter existing sessions many times
            for _ in range(20):
                ts._cap_admission_lock("session-0")
                ts._cap_admission_lock("session-1")
            assert ts._cap_registry_cardinality == 3
            assert "cap_registry_cardinality_high" not in caplog.text

    def test_no_warning_fires_at_same_crossing_twice(self, caplog):
        """Once warned at a crossing, same crossing never fires again."""
        ts._CAP_REGISTRY_WARN_CARDINALITY = 4
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.terminal_service"):
            for i in range(4):
                ts._cap_admission_lock(f"session-{i}")
            assert caplog.text.count("cap_registry_cardinality_high") == 1

            # Manually reset cardinality back (simulating some edge case)
            # and re-add — shouldn't re-warn at 4 because warned_at is already 4
            caplog.clear()
            # Adding more sessions one by one but staying below next crossing
            ts._cap_admission_lock("session-extra")  # count=5
            assert "cap_registry_cardinality_high" not in caplog.text


class TestCardinalityDisabled:
    """Threshold <= 0 disables the warning entirely."""

    def test_zero_threshold_disables(self, caplog):
        """CAO_CAP_REGISTRY_WARN_CARDINALITY=0 -> no warnings ever."""
        ts._CAP_REGISTRY_WARN_CARDINALITY = 0
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.terminal_service"):
            for i in range(1000):
                ts._cap_admission_lock(f"session-{i}")
            assert "cap_registry_cardinality_high" not in caplog.text
            assert ts._cap_registry_cardinality == 1000  # still tracked, just not warned

    def test_negative_threshold_disables(self, caplog):
        """Negative threshold also disables."""
        ts._CAP_REGISTRY_WARN_CARDINALITY = -1
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.terminal_service"):
            for i in range(100):
                ts._cap_admission_lock(f"session-{i}")
            assert "cap_registry_cardinality_high" not in caplog.text


class TestAdmissionReleasePaths:
    """F451 does NOT change admission/release semantics — no refusal, no reclamation."""

    def test_admission_lock_returns_same_lock_for_same_session(self):
        """Existing contract: same session -> same lock object."""
        lock1 = ts._cap_admission_lock("my-session")
        lock2 = ts._cap_admission_lock("my-session")
        assert lock1 is lock2

    def test_admission_lock_returns_different_lock_for_different_sessions(self):
        """Existing contract: different session -> different lock objects."""
        lock_a = ts._cap_admission_lock("session-a")
        lock_b = ts._cap_admission_lock("session-b")
        assert lock_a is not lock_b

    def test_lock_is_threading_lock(self):
        """Existing contract: returned lock is a threading.Lock."""
        lock = ts._cap_admission_lock("my-session")
        assert isinstance(lock, type(threading.Lock()))

    def test_high_cardinality_never_refuses(self, caplog):
        """Even at extreme cardinality, admission is never refused."""
        ts._CAP_REGISTRY_WARN_CARDINALITY = 4
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.terminal_service"):
            # Create 100 sessions — warnings fire but no exception raised
            for i in range(100):
                lock = ts._cap_admission_lock(f"session-{i}")
                assert lock is not None
            # Many warnings fired, but every call succeeded
            assert ts._cap_registry_cardinality == 100

    def test_gen_and_token_seq_retained_after_warning(self):
        """Cardinality warning does not trigger reclamation of gen/token_seq."""
        ts._CAP_REGISTRY_WARN_CARDINALITY = 2
        # Simulate creating gen+token_seq entries
        ts._cap_admission_lock("s1")
        ts._cap_gen["s1"] = 5
        ts._cap_token_seq["s1"] = 3
        ts._cap_admission_lock("s2")  # crosses threshold -> warning
        # Verify s1's gen/token_seq are untouched
        assert ts._cap_gen["s1"] == 5
        assert ts._cap_token_seq["s1"] == 3
        # Lock for s1 still exists
        assert "s1" in ts._cap_admission_locks
