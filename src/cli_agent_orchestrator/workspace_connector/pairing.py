"""Single-use pairing code — port of ``src/pairing/manager.ts``.

Upstream: XiaoDuoYa/codex-with-chatgpt at commit
8fdd97c188c7678d0d9c43b3769b426940de568a (MIT).  Behavioural port: a
short-lived, one-time, local verification credential (NOT an OAuth authorization
code); unambiguous alphabet, rejection sampling, constant-time hash compare,
per-IP rate limiting and attempt counters.  ``create()`` invalidates previous
sessions (one active at a time).
"""

from __future__ import annotations

import hashlib
import secrets
import time

# No ambiguous characters (I, L, O, 0, 1).
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _generate_code(length: int = 8) -> str:
    chars: list[str] = []
    limit = (256 // len(ALPHABET)) * len(ALPHABET)
    while len(chars) < length:
        for byte in secrets.token_bytes(length * 2):
            if byte < limit:  # rejection sampling for uniformity
                chars.append(ALPHABET[byte % len(ALPHABET)])
                if len(chars) == length:
                    break
    return "".join(chars)


def _hash_code(code: str) -> bytes:
    return hashlib.sha256(code.encode("utf-8")).digest()


def format_pairing_code(raw: str) -> str:
    return f"{raw[:4]}-{raw[4:8]}"


def normalize_pairing_code(value: str) -> str:
    import re

    return re.sub(r"[^A-Z2-9]", "", value.upper())


class PairingSession:
    def __init__(
        self, session_id: str, code_hash: bytes, expires_at: float, max_attempts: int
    ) -> None:
        self.id = session_id
        self.code_hash = code_hash
        self.expires_at = expires_at
        self.attempts_left = max_attempts
        self.used = False


class PairingManager:
    """One active pairing session at a time; the code is single-use."""

    def __init__(
        self,
        workspace_id: str | None = None,
        *,
        ttl_s: float = 5 * 60,
        max_attempts: int = 5,
        ip_rate_limit: int = 10,
        ip_rate_window_s: float = 60,
    ) -> None:
        self.workspace_id = workspace_id
        self._sessions: dict[str, PairingSession] = {}
        self._ip_hits: dict[str, tuple[int, float]] = {}
        self._ttl_s = ttl_s
        self._max_attempts = max_attempts
        self._ip_rate_limit = ip_rate_limit
        self._ip_rate_window_s = ip_rate_window_s

    def create(self) -> dict[str, object]:
        """Create a new pairing session; invalidates previous ones (upstream parity)."""
        self._sessions.clear()
        raw = _generate_code()
        session = PairingSession(
            secrets.token_hex(16), _hash_code(raw), time.time() + self._ttl_s, self._max_attempts
        )
        self._sessions[session.id] = session
        return {
            "session_id": session.id,
            "code": format_pairing_code(raw),
            "expires_at": session.expires_at,
        }

    def _check_ip_rate(self, ip: str | None) -> bool:
        if not ip:
            return True
        now = time.time()
        entry = self._ip_hits.get(ip)
        if entry is None or now > entry[1]:
            self._ip_hits[ip] = (1, now + self._ip_rate_window_s)
            return True
        count = entry[0] + 1
        self._ip_hits[ip] = (count, entry[1])
        return count <= self._ip_rate_limit

    def verify(self, code_input: str, ip: str | None = None) -> dict[str, object]:
        """Verify a pairing code; returns ``{ok, reason, ...}`` (upstream parity)."""
        if not self._check_ip_rate(ip):
            return {"ok": False, "reason": "rate_limited"}
        normalized = normalize_pairing_code(code_input)
        input_hash = _hash_code(normalized)
        now = time.time()

        active = [s for s in self._sessions.values() if not s.used]
        if not active:
            return {"ok": False, "reason": "no_active_session"}

        for session in active:
            if now > session.expires_at:
                self._sessions.pop(session.id, None)
                return {"ok": False, "reason": "expired"}
            if session.attempts_left <= 0:
                self._sessions.pop(session.id, None)
                return {"ok": False, "reason": "too_many_attempts"}
            if secrets.compare_digest(input_hash, session.code_hash):
                # one-time use: destroy immediately
                self._sessions.pop(session.id, None)
                return {"ok": True, "session_id": session.id}
            session.attempts_left -= 1
            if session.attempts_left <= 0:
                self._sessions.pop(session.id, None)
                return {"ok": False, "reason": "too_many_attempts"}
            return {"ok": False, "reason": "invalid", "attempts_left": session.attempts_left}
        return {"ok": False, "reason": "no_active_session"}

    def has_active_session(self) -> bool:
        now = time.time()
        return any(not s.used and now <= s.expires_at for s in self._sessions.values())

    def invalidate_all(self) -> None:
        self._sessions.clear()
