"""Claim attacked: quiescence never blocks the loop on a threading.Lock.

Round: WPM4-A diff gate r10.
Expected post-fix semantics: if a competing commit thread is descheduled while
holding the ready-winner lock, quiescence remains within its shared budget,
keeps the event loop responsive, and returns a truthful unknown/timeout branch.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r10/test_ready_winner_lock_responsiveness_probe.py
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

import pytest

from cli_agent_orchestrator.services import terminal_service as terminals
from cli_agent_orchestrator.services.deferred_dispatcher import DeferredCall


@pytest.mark.asyncio
async def test_quiescence_does_not_block_loop_on_ready_winner_lock():
    terminal_id = "ready-winner-lock-stall"
    generation = "generation"
    parked = asyncio.Event()

    async def owner() -> None:
        await parked.wait()

    task = asyncio.create_task(owner())
    future: concurrent.futures.Future = concurrent.futures.Future()
    call = DeferredCall(
        terminal_id=terminal_id,
        generation=generation,
        call_type="abandonable",
        operation="ready_commit",
        future=future,
    )
    record = terminals._DeferredTaskRecord(
        task=task,
        loop=asyncio.get_running_loop(),
        generation=generation,
        current_call=call,
    )
    terminals._deferred_tasks_by_terminal[terminal_id] = record
    lock_held = threading.Event()
    release_lock = threading.Event()

    def stalled_commit_thread() -> None:
        with call.ready_winner_lock:
            lock_held.set()
            release_lock.wait(1)

    holder = threading.Thread(target=stalled_commit_thread)
    holder.start()
    assert await asyncio.to_thread(lock_held.wait, 1)

    ticks = 0
    stop_ticker = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticker.is_set():
            ticks += 1
            await asyncio.sleep(0.001)

    ticking = asyncio.create_task(ticker())
    tick_samples: list[int] = []
    sample_one = threading.Timer(0.05, lambda: tick_samples.append(ticks))
    sample_two = threading.Timer(0.12, lambda: tick_samples.append(ticks))
    timer = threading.Timer(0.15, release_lock.set)
    sample_one.start()
    sample_two.start()
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError):
            await terminals.quiesce_deferred_terminal(terminal_id, timeout_s=0.02)
        elapsed = time.monotonic() - started
        if elapsed < 0.1:
            await asyncio.sleep(0.13 - elapsed)
        assert len(tick_samples) == 2
        assert elapsed < 0.1 and tick_samples[1] > tick_samples[0] + 20, (
            elapsed, tick_samples,
        )
    finally:
        stop_ticker.set()
        release_lock.set()
        sample_one.join(1)
        sample_two.join(1)
        timer.join(1)
        holder.join(1)
        await asyncio.gather(ticking, return_exceptions=True)
        terminals._deferred_tasks_by_terminal.pop(terminal_id, None)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
