"""AC2 — ULID minting (WP-ARCH phase 1, F725 #581).

Tests that pin a timestamp use their OWN :class:`UlidFactory`.  The process-wide
stream has already issued ids at the real wall clock, and its monotonic
guarantee would (correctly) clamp a back-dated request; a private stream is the
supported way to mint at a chosen millisecond.
"""

from __future__ import annotations

import threading

import pytest

from cli_agent_orchestrator.core.ids import (
    CROCKFORD_ALPHABET,
    ULID_LENGTH,
    UlidFactory,
    is_ulid,
    new_ulid,
    ulid_timestamp_ms,
)


def test_shape() -> None:
    """26 characters, Crockford base32 only, no ambiguous letters."""
    value = new_ulid()
    assert len(value) == ULID_LENGTH
    assert is_ulid(value)
    assert set(value) <= set(CROCKFORD_ALPHABET)
    assert not set("ILOU") & set(CROCKFORD_ALPHABET)


def test_timestamp_round_trips() -> None:
    """The first ten characters decode back to the millisecond they encode."""
    assert ulid_timestamp_ms(UlidFactory().new(1_756_000_000_000)) == 1_756_000_000_000
    assert ulid_timestamp_ms(UlidFactory().new(0)) == 0


def test_lexicographic_order_matches_time_order() -> None:
    """Later timestamps sort later as plain strings — the reason for ULIDs at all."""
    factory = UlidFactory()
    early = factory.new(1_000_000_000_000)
    late = factory.new(2_000_000_000_000)
    assert early < late


def test_monotonic_within_one_millisecond() -> None:
    """Ids minted in the same millisecond still sort in mint order."""
    factory = UlidFactory()
    stamp = 1_756_000_000_123
    minted = [factory.new(stamp) for _ in range(500)]
    assert minted == sorted(minted)
    assert len(set(minted)) == len(minted)
    assert {ulid_timestamp_ms(value) for value in minted} == {stamp}


def test_clock_stepping_backwards_does_not_invert_order() -> None:
    """A backwards clock step keeps the stream monotonic rather than reversing it.

    NTP corrections happen; an id that sorts before its predecessor would
    silently reorder a diag timeline.
    """
    factory = UlidFactory()
    first = factory.new(1_756_000_000_500)
    second = factory.new(1_756_000_000_400)
    assert second > first


def test_default_stream_is_monotonic_across_calls() -> None:
    """The process-wide stream, the one production actually uses, is ordered."""
    minted = [new_ulid() for _ in range(200)]
    assert minted == sorted(minted)


def test_separate_factories_are_independent_streams() -> None:
    """A test factory cannot disturb, or be disturbed by, the process-wide one."""
    private = UlidFactory()
    assert ulid_timestamp_ms(private.new(1_000_000_000_000)) == 1_000_000_000_000
    assert ulid_timestamp_ms(new_ulid()) > 1_000_000_000_000


def test_unique_under_threads() -> None:
    """Concurrent minting from several threads yields no collisions."""
    factory = UlidFactory()
    minted: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        local = [factory.new() for _ in range(200)]
        with lock:
            minted.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(minted) == 800
    assert len(set(minted)) == 800


def test_is_ulid_rejects_malformed() -> None:
    assert not is_ulid("")
    assert not is_ulid("short")
    assert not is_ulid("I" * ULID_LENGTH)  # 'I' is excluded from Crockford base32
    assert not is_ulid("a" * ULID_LENGTH)  # lowercase is not the canonical rendering


def test_ulid_timestamp_ms_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="not a ULID"):
        ulid_timestamp_ms("nope")


def test_out_of_range_timestamp_rejected() -> None:
    with pytest.raises(ValueError, match="out of range"):
        UlidFactory().new(-1)
    with pytest.raises(ValueError, match="out of range"):
        UlidFactory().new(1 << 48)
