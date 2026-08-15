"""Deterministic seeded RNG with named sub-streams (D8/D9).

One global seed, one master Random instance. Consumers draw through named
sub-streams: sim.rng.stream("faults"), each seeded Random(seed ^ stable_hash(name)).
Adding a draw in one stream does not shift draws in another — the committed
failing-seed corpus remains valid (D9).
"""

from __future__ import annotations

import hashlib
import random
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class SimRNG:
    """Seeded RNG with independent named sub-streams.

    Each stream is seeded from (master_seed XOR stable_hash(stream_name)),
    ensuring cross-stream independence (D9).
    """

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._master = random.Random(seed)
        self._streams: dict[str, random.Random] = {}
        self._lock = threading.Lock()

    @property
    def seed(self) -> int:
        return self._seed

    def stream(self, name: str) -> random.Random:
        """Get or create a named sub-stream Random instance."""
        with self._lock:
            if name not in self._streams:
                h = int(hashlib.sha256(name.encode()).hexdigest()[:16], 16)
                sub_seed = self._seed ^ h
                self._streams[name] = random.Random(sub_seed)
            return self._streams[name]

    def reset(self) -> None:
        """Reset all streams — useful for replay verification."""
        with self._lock:
            self._master = random.Random(self._seed)
            self._streams.clear()


# ---------------------------------------------------------------------------
# Global binding (matches clock.py pattern)
# ---------------------------------------------------------------------------

_active_rng: SimRNG | None = None
_rng_lock = threading.Lock()


def active() -> SimRNG | None:
    """Return the currently installed SimRNG, or None if in production mode."""
    return _active_rng


def install_rng(rng: SimRNG) -> None:
    """Install a SimRNG globally (called by SimWorld context)."""
    global _active_rng
    with _rng_lock:
        if _active_rng is not None:
            raise RuntimeError("SimRNG already installed — nested installs forbidden.")
        _active_rng = rng


def uninstall_rng(rng: SimRNG) -> None:
    """Uninstall the SimRNG (called on SimWorld exit)."""
    global _active_rng
    with _rng_lock:
        if _active_rng is not rng:
            raise AssertionError("SimRNG restore assertion failed.")
        _active_rng = None
