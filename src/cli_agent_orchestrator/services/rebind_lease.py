"""Non-reentrant per-terminal leases shared by rebind and teardown."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class RebindLeaseToken:
    terminal_id: str
    generation: int
    nonce: str


_guard = threading.Lock()
_owners: dict[str, RebindLeaseToken] = {}
_generations: dict[str, int] = {}


def acquire_rebind_lease(terminal_id: str) -> RebindLeaseToken | None:
    with _guard:
        if terminal_id in _owners:
            return None
        generation = _generations.get(terminal_id, 0) + 1
        _generations[terminal_id] = generation
        token = RebindLeaseToken(terminal_id, generation, uuid.uuid4().hex)
        _owners[terminal_id] = token
        return token


def validate_rebind_lease(terminal_id: str, token: RebindLeaseToken) -> None:
    with _guard:
        if token.terminal_id != terminal_id or _owners.get(terminal_id) != token:
            raise RuntimeError("invalid_rebind_lease_token")


def release_rebind_lease(token: RebindLeaseToken) -> None:
    with _guard:
        if _owners.get(token.terminal_id) != token:
            raise RuntimeError("invalid_rebind_lease_token")
        del _owners[token.terminal_id]


def rebind_lease_held(terminal_id: str) -> bool:
    with _guard:
        return terminal_id in _owners
