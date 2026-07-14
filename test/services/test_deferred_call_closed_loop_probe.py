"""Claim attacked: closed-loop completion cannot strand a retained call.

Round: WPM4-A diff gate r10.
Expected post-fix semantics: when a retained Future completes after its owning
loop has closed, cleanup has a non-loop fallback (or equally visible owner),
returns without deadlock, and releases the completed registry entry.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r10/test_deferred_call_closed_loop_probe.py
"""

from __future__ import annotations

import asyncio
import concurrent.futures

from cli_agent_orchestrator.services import terminal_service as terminals
from cli_agent_orchestrator.services.deferred_dispatcher import DeferredCall


def test_future_completion_after_loop_close_releases_retained_record():
    terminal_id = "closed-loop-retained-call"
    generation = "generation"
    loop = asyncio.new_event_loop()

    async def parked() -> None:
        await asyncio.Event().wait()

    task = loop.create_task(parked())
    loop.run_until_complete(asyncio.sleep(0))
    future: concurrent.futures.Future = concurrent.futures.Future()
    call = DeferredCall(
        terminal_id=terminal_id,
        generation=generation,
        call_type="abandonable",
        operation="capture_persist",
        future=future,
    )
    record = terminals._DeferredTaskRecord(
        task=task,
        loop=loop,
        generation=generation,
        current_call=call,
    )
    terminals._deferred_tasks_by_terminal[terminal_id] = record
    terminals._register_deferred_call(terminal_id, generation, call)

    task.cancel()
    loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
    assert task.done() and not future.done()
    loop.close()

    try:
        future.set_result("done")
        assert future.done()
        assert terminal_id not in terminals._deferred_tasks_by_terminal
    finally:
        terminals._deferred_tasks_by_terminal.pop(terminal_id, None)

