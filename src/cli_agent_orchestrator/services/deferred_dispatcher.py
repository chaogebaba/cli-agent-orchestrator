"""Cancellation-aware daemon-thread execution for deferred terminal init."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Literal, cast

DEFERRED_EXECUTOR_MAX_WORKERS = 8
DEFERRED_ADMISSION_QUEUE_MAX = 32

CallType = Literal["abandonable", "mutating"]
ReadyWinner = Literal["open", "timeout", "commit_decided"]
ResultOwner = Literal["open", "task", "quiesce", "reconciler"]


class DeferredExecutorSaturated(RuntimeError):
    code = "deferred_executor_saturated"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass
class DeferredCall:
    terminal_id: str
    generation: str
    call_type: CallType
    operation: str
    future: concurrent.futures.Future
    started: bool = False
    quiesce_failed: bool = False
    ready_committed: bool = False
    ready_winner: ReadyWinner = "open"
    result_owner: ResultOwner = "open"
    ready_winner_lock: threading.Lock = field(default_factory=threading.Lock)
    abandon_event: threading.Event | None = None
    grant_time: float | None = None
    released: asyncio.Event | None = None


@dataclass
class _Waiter:
    ticket: asyncio.Future
    deadline: float | None
    call: DeferredCall


class DaemonDispatcher:
    """Bounded FIFO admission whose workers never hold interpreter shutdown."""

    def __init__(
        self,
        max_workers: int = DEFERRED_EXECUTOR_MAX_WORKERS,
        max_queue: int = DEFERRED_ADMISSION_QUEUE_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_workers = max_workers
        self.max_queue = max_queue
        self.clock = clock
        self._active = 0
        self._waiters: Deque[_Waiter] = deque()
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _loop_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            if self._active or self._waiters:
                raise RuntimeError("deferred_dispatcher_loop_changed_while_active")
            self._loop = loop
            self._lock = asyncio.Lock()
        assert self._lock is not None
        return self._lock

    async def _admit(self, call: DeferredCall, deadline: float | None) -> float:
        loop = asyncio.get_running_loop()
        lock = self._loop_lock()
        waiter: _Waiter | None = None
        async with lock:
            now = self.clock()
            if deadline is not None and now > deadline:
                call.future.cancel()
                raise DeferredExecutorSaturated()
            if self._active < self.max_workers and not self._waiters:
                self._active += 1
                return now
            if len(self._waiters) >= self.max_queue:
                call.future.cancel()
                raise DeferredExecutorSaturated()
            waiter = _Waiter(loop.create_future(), deadline, call)
            self._waiters.append(waiter)
        try:
            if deadline is None:
                return cast(float, await waiter.ticket)
            remaining = deadline - self.clock()
            if remaining < 0:
                raise asyncio.TimeoutError
            return cast(
                float,
                await asyncio.wait_for(asyncio.shield(waiter.ticket), remaining),
            )
        except asyncio.TimeoutError:
            async with lock:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    if (
                        waiter.ticket.done() and not waiter.ticket.cancelled()
                        and waiter.ticket.exception() is None
                    ):
                        self._active -= 1
                        self._grant_next_locked()
                call.future.cancel()
            raise DeferredExecutorSaturated()
        except asyncio.CancelledError:
            async with lock:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    if (
                        waiter.ticket.done() and not waiter.ticket.cancelled()
                        and waiter.ticket.exception() is None
                    ):
                        self._active -= 1
                        self._grant_next_locked()
                call.future.cancel()
            raise

    def _release_from_thread(self, call: DeferredCall) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._release_and_signal(call))
            )

    async def _release_and_signal(self, call: DeferredCall) -> None:
        try:
            await self._release()
        finally:
            if call.released is not None:
                call.released.set()

    async def _release(self) -> None:
        lock = self._loop_lock()
        async with lock:
            self._active -= 1
            self._grant_next_locked()

    def _grant_next_locked(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.ticket.cancelled() or waiter.ticket.done():
                continue
            grant = self.clock()
            if waiter.deadline is not None and grant > waiter.deadline:
                waiter.call.future.cancel()
                waiter.ticket.set_exception(DeferredExecutorSaturated())
                continue
            self._active += 1
            waiter.ticket.set_result(grant)
            break

    async def run(
        self,
        terminal_id: str,
        generation: str,
        call_type: CallType,
        operation: str,
        function: Callable[..., Any],
        *args: Any,
        deadline: float | None = None,
        on_registered: Callable[[DeferredCall], None] | None = None,
        **kwargs: Any,
    ) -> tuple[Any, float]:
        underlying: concurrent.futures.Future = concurrent.futures.Future()
        call = DeferredCall(
            terminal_id=terminal_id, generation=generation,
            call_type=call_type, operation=operation, future=underlying,
        )
        call.abandon_event = threading.Event()
        call.released = asyncio.Event()
        if on_registered is not None:
            on_registered(call)
        grant = await self._admit(call, deadline)
        call.grant_time = grant
        call.started = True

        def invoke() -> None:
            if not underlying.set_running_or_notify_cancel():
                self._release_from_thread(call)
                return
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                self._release_from_thread(call)
                underlying.set_exception(exc)
            else:
                self._release_from_thread(call)
                underlying.set_result(result)

        threading.Thread(
            target=invoke,
            name=f"cao-deferred-{operation}-{terminal_id[:8]}",
            daemon=True,
        ).start()
        wrapped = asyncio.wrap_future(underlying)
        try:
            result = await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            # The retained concurrent Future is reconciled by terminal_service,
            # but wrap_future also needs its late exception retrieved.
            wrapped.add_done_callback(
                lambda completed: (
                    None if completed.cancelled() else completed.exception()
                )
            )
            if underlying.done() and call.released is not None:
                await call.released.wait()
            raise
        except BaseException:
            if underlying.done() and call.released is not None:
                await call.released.wait()
            raise
        if call.released is not None:
            await call.released.wait()
        return result, grant


dispatcher = DaemonDispatcher()
