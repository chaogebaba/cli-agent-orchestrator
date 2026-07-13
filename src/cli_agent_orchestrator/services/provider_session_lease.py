"""Non-reentrant, process-local leases for resumable provider-session UUIDs."""

from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class ProviderSessionLeaseToken:
    session_uuid: str
    generation: int


_lock = threading.Lock()
_held: dict[str, ProviderSessionLeaseToken] = {}
_generation = 0


def acquire_provider_session_lease(session_uuid: str) -> ProviderSessionLeaseToken | None:
    global _generation
    with _lock:
        if session_uuid in _held:
            return None
        _generation += 1
        token = ProviderSessionLeaseToken(session_uuid, _generation)
        _held[session_uuid] = token
        return token


def validate_provider_session_lease(session_uuid: str, token: ProviderSessionLeaseToken) -> None:
    with _lock:
        if token.session_uuid != session_uuid or _held.get(session_uuid) != token:
            raise RuntimeError("invalid_provider_session_lease_token")


def release_provider_session_lease(token: ProviderSessionLeaseToken) -> None:
    with _lock:
        if _held.get(token.session_uuid) != token:
            raise RuntimeError("invalid_provider_session_lease_token")
        del _held[token.session_uuid]


def provider_session_lease_held(session_uuid: str) -> bool:
    with _lock:
        return session_uuid in _held

