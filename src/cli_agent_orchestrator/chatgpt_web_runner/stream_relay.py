"""Transient raw-SSE relay for F862 Amendment D.

The relay deliberately stores only a SHA-256 token digest and bounded in-memory
bytes. Raw origin frames have no file/log/callback representation. A subscriber
is consumed at first bind; disconnect, restart, or overflow never permits a
rebind and never asks the origin sender to retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from fastapi import Header, HTTPException

from cli_agent_orchestrator.chatgpt_web_runner.send_intent import SendIntentLog

CHATGPT_RELAY_QUEUE_BYTES = 4 * 1024 * 1024


def hash_relay_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RelayAccessError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class RelayBinding:
    attempt_id: str
    subscriber_id: str


class AttemptRelay:
    """One attempt's one-subscriber, bounded-byte transient stream."""

    _EOF = object()

    def __init__(
        self,
        *,
        attempt_id: str,
        token_hash: str,
        expires_at: float,
        intent_log: Optional[SendIntentLog] = None,
        queue_bytes: int = CHATGPT_RELAY_QUEUE_BYTES,
    ) -> None:
        if queue_bytes <= 0:
            raise ValueError("relay queue_bytes must be positive")
        self.attempt_id = attempt_id
        self._token_hash = token_hash
        self.expires_at = expires_at
        self.intent_log = intent_log
        self.queue_bytes = queue_bytes
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._queued_bytes = 0
        self._lock = asyncio.Lock()
        self._binding: Optional[RelayBinding] = None
        self._binding_consumed = False
        self._closed = False
        self._subscriber_closed = False
        self.status = "pending"

    @property
    def binding(self) -> Optional[RelayBinding]:
        return self._binding

    @property
    def is_bound(self) -> bool:
        return self._binding_consumed

    async def bind(self, token: str) -> RelayBinding:
        async with self._lock:
            if self._closed or time.time() >= self.expires_at:
                raise RelayAccessError(410, "relay_closed")
            if not hmac.compare_digest(hash_relay_token(token), self._token_hash):
                raise RelayAccessError(401, "relay_token_invalid")
            if self._binding_consumed:
                raise RelayAccessError(409, "relay_already_bound")
            subscriber_id = secrets.token_urlsafe(18)
            binding = RelayBinding(self.attempt_id, subscriber_id)
            self._binding = binding
            self._binding_consumed = True
            self.status = "bound"
            if self.intent_log is not None:
                self.intent_log.record_relay_bound(
                    subscriber_id=subscriber_id,
                    bound_at=time.time(),
                )
            return binding

    async def mark_skipped(self) -> None:
        """Close the binding window when ``MINT_RESERVED`` is reached unbound."""
        async with self._lock:
            if self._binding_consumed:
                return
            self._closed = True
            self.status = "skipped"
            if self.intent_log is not None:
                self.intent_log.record_relay_skipped(skipped_at=time.time())
            self._queue.put_nowait(self._EOF)

    async def publish(self, chunk: bytes) -> bool:
        """Offer decoded origin bytes without ever blocking the origin drain.

        ``False`` means the subscriber was closed/truncated; callers must keep
        draining their HTTP response for GET reconciliation.
        """
        if not isinstance(chunk, bytes):
            raise TypeError("relay chunks must be bytes")
        if not chunk:
            return True
        async with self._lock:
            if self._closed:
                return False
            if self._subscriber_closed:
                return False
            if self._queued_bytes + len(chunk) > self.queue_bytes:
                self._subscriber_closed = True
                self.status = "truncated"
                self._queue.put_nowait(self._EOF)
                return False
            self._queued_bytes += len(chunk)
            self._queue.put_nowait(chunk)
            return True

    async def finish(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.status not in {"truncated", "disconnected"}:
                self.status = "complete"
            self._queue.put_nowait(self._EOF)

    async def disconnect(self) -> None:
        async with self._lock:
            if self.status != "truncated":
                self.status = "disconnected"
            self._subscriber_closed = True
            self._queue.put_nowait(self._EOF)

    async def bytes_for(self, binding: RelayBinding) -> AsyncIterator[bytes]:
        if binding != self._binding:
            raise RelayAccessError(401, "relay_token_invalid")
        completed = False
        try:
            while True:
                item = await self._queue.get()
                if item is self._EOF:
                    completed = True
                    return
                assert isinstance(item, bytes)
                async with self._lock:
                    self._queued_bytes -= len(item)
                yield item
        finally:
            if not completed and not self._closed:
                await self.disconnect()


class RelayHub:
    """Process-local registry owned by the CAO API process."""

    def __init__(self) -> None:
        self._relays: dict[str, AttemptRelay] = {}

    def register(
        self,
        *,
        attempt_id: str,
        token_hash: str,
        expires_at: float,
        intent_log: Optional[SendIntentLog] = None,
        queue_bytes: int = CHATGPT_RELAY_QUEUE_BYTES,
    ) -> AttemptRelay:
        if attempt_id in self._relays:
            raise RelayAccessError(409, "relay_attempt_exists")
        relay = AttemptRelay(
            attempt_id=attempt_id,
            token_hash=token_hash,
            expires_at=expires_at,
            intent_log=intent_log,
            queue_bytes=queue_bytes,
        )
        self._relays[attempt_id] = relay
        return relay

    def get(self, attempt_id: str) -> AttemptRelay:
        relay = self._relays.get(attempt_id)
        if relay is None:
            raise RelayAccessError(401, "relay_token_invalid")
        return relay

    def reset_for_tests(self) -> None:
        self._relays.clear()


_HUB = RelayHub()


def get_relay_hub() -> RelayHub:
    return _HUB


async def require_relay_token(
    attempt_id: str,
    relay_token: Optional[str] = Header(default=None, alias="X-CAO-Relay-Token"),
) -> tuple[AttemptRelay, RelayBinding]:
    """Second dependency on the public relay route, after ordinary CAO auth."""
    if not relay_token:
        raise HTTPException(status_code=401, detail="relay_token_invalid")
    try:
        relay = get_relay_hub().get(attempt_id)
        binding = await relay.bind(relay_token)
    except RelayAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return relay, binding
