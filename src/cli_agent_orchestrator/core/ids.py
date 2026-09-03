"""ULID minting for the worker-truth id family (WP-ARCH phase 1, AC2).

The blueprint §4b fixes which identifiers are new and which are inherited:
``event_id``, ``run_id`` and ``msg_id`` are freshly minted ULIDs; ``session_id``
and ``terminal_id`` stay the existing opaque strings the fork already uses, so
nothing here mints those.

A ULID is 128 bits rendered as 26 Crockford base32 characters: a 48-bit
big-endian millisecond timestamp followed by 80 bits of randomness.  Two
properties earn it its place over ``uuid4``:

* **Lexicographic order matches time order.**  ``worker_event.event_id`` is the
  primary key and the audit §3.1 comment says "lexicographically sortable"; an
  ``ORDER BY event_id`` therefore reconstructs ingestion order without a join.
* **Monotonic within a millisecond.**  The canonical ULID spec's monotonic
  variant increments the random component when two ids are minted in the same
  millisecond.  A :class:`UlidFactory` implements that under a lock, so two
  events appended back-to-back by the single writer never tie and never invert —
  including across an NTP correction that steps the clock backwards.

Monotonicity is a property of a STREAM, so it lives on a factory object rather
than in module globals.  Production shares one stream through :func:`new_ulid`;
a test that needs to mint at a chosen timestamp makes its own factory and cannot
disturb, or be disturbed by, anything else.

No I/O, no third-party dependency: ``secrets`` and ``time`` only.
"""

from __future__ import annotations

import secrets
import threading
import time

__all__ = [
    "CROCKFORD_ALPHABET",
    "ULID_LENGTH",
    "UlidFactory",
    "is_ulid",
    "new_ulid",
    "ulid_timestamp_ms",
]

# Crockford base32: the digits plus the uppercase alphabet with I, L, O and U
# removed (they are the characters humans transcribe wrongly).  Order matters —
# this exact ordering is what makes the encoding sort like the integer it holds.
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

ULID_LENGTH = 26
_TIMESTAMP_CHARS = 10
_RANDOM_BITS = 80
_RANDOM_MAX = (1 << _RANDOM_BITS) - 1
_TIMESTAMP_MAX = (1 << 48) - 1

_DECODE = {char: index for index, char in enumerate(CROCKFORD_ALPHABET)}


def _encode(value: int, length: int) -> str:
    """Render ``value`` as ``length`` Crockford base32 characters, zero-padded."""
    chars = [""] * length
    for position in range(length - 1, -1, -1):
        chars[position] = CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


class UlidFactory:
    """One monotonic ULID stream.

    Every id from a given factory sorts after the one before it, whatever the
    clock does.  Separate factories are independent streams and make no promise
    about each other, which is exactly the isolation a test wants.

    The lock is real work, not ceremony: the server is one process (U7) but a
    threaded one — the FastAPI thread pool and the tmux clients both append.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_timestamp_ms = -1
        self._last_randomness = 0

    def new(self, timestamp_ms: int | None = None) -> str:
        """Mint the next ULID in this stream.

        ``timestamp_ms`` overrides the wall clock, for tests and for back-dating
        a replayed record.  It is still subject to the stream's monotonic
        guarantee: asking for an EARLIER millisecond than the stream has already
        issued yields an id at the stream's current millisecond rather than one
        that would sort backwards.  Use a fresh factory to mint at an arbitrary
        timestamp.

        Within one millisecond the randomness is incremented rather than
        redrawn.  If the 80-bit counter would overflow inside a single
        millisecond, the timestamp advances by one — the spec permits that or an
        error, and an orchestrator has no use for the error.
        """
        now_ms = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
        if now_ms < 0 or now_ms > _TIMESTAMP_MAX:
            raise ValueError(f"ULID timestamp out of range: {now_ms}")

        with self._lock:
            if now_ms > self._last_timestamp_ms:
                self._last_timestamp_ms = now_ms
                self._last_randomness = secrets.randbits(_RANDOM_BITS)
            else:
                # Same millisecond, or a clock that stepped backwards: keep the
                # monotonic guarantee by advancing the counter under the LAST
                # timestamp rather than emitting an id that sorts before its
                # predecessor.
                if self._last_randomness >= _RANDOM_MAX:
                    self._last_timestamp_ms += 1
                    self._last_randomness = secrets.randbits(_RANDOM_BITS)
                else:
                    self._last_randomness += 1
                now_ms = self._last_timestamp_ms
            randomness = self._last_randomness

        return _encode(now_ms, _TIMESTAMP_CHARS) + _encode(
            randomness, ULID_LENGTH - _TIMESTAMP_CHARS
        )


#: The process-wide stream.  Every ``event_id``, ``run_id``, ``msg_id`` and
#: ``finding_id`` in the new tree comes from here, so all of them share one
#: monotonic ordering.
_default_factory = UlidFactory()


def new_ulid(timestamp_ms: int | None = None) -> str:
    """Mint a ULID from the process-wide monotonic stream."""
    return _default_factory.new(timestamp_ms)


def is_ulid(value: str) -> bool:
    """True when ``value`` is a well-formed 26-character Crockford base32 ULID."""
    if len(value) != ULID_LENGTH:
        return False
    return all(char in _DECODE for char in value)


def ulid_timestamp_ms(value: str) -> int:
    """Recover the millisecond timestamp a ULID was minted with."""
    if not is_ulid(value):
        raise ValueError(f"not a ULID: {value!r}")
    timestamp = 0
    for char in value[:_TIMESTAMP_CHARS]:
        timestamp = (timestamp << 5) | _DECODE[char]
    return timestamp
