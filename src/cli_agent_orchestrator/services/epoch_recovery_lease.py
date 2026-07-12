"""Non-reentrant recovery leases keyed by durable session/base identity."""

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class EpochRecoveryLease:
    session_name: str
    base_name: str


_guard = threading.Lock()
_held: set[tuple[str, str]] = set()


def acquire_epoch_recovery_lease(session_name: str, base_name: str):
    key = (session_name, base_name)
    with _guard:
        if key in _held:
            return None
        _held.add(key)
    return EpochRecoveryLease(*key)


def release_epoch_recovery_lease(token: EpochRecoveryLease) -> None:
    key = (token.session_name, token.base_name)
    with _guard:
        if key not in _held:
            raise RuntimeError("invalid_epoch_recovery_lease")
        _held.remove(key)
