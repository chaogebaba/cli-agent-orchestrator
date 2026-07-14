"""Claim attacked: deferred call result has one consumer or observer.

Round: WPM4-A diff gate r10.
Expected post-fix semantics: when Future completion lands between quiescence's
call snapshot and cancellation delivery, exactly one of the owning task or
quiescence observes the exception; it is never raised/consumed twice or lost.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r10/test_deferred_call_double_observation_probe.py
"""

from __future__ import annotations

import asyncio
import concurrent.futures

import pytest

from cli_agent_orchestrator.services import terminal_service as terminals
from cli_agent_orchestrator.services.deferred_dispatcher import DeferredCall


class ExpectedFailure(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_completion_between_snapshot_and_cancel_is_observed_once():
    terminal_id = "double-observation"
    generation = "generation"
    loop = asyncio.get_running_loop()
    future: concurrent.futures.Future = concurrent.futures.Future()
    call = DeferredCall(
        terminal_id=terminal_id,
        generation=generation,
        call_type="abandonable",
        operation="ready_commit",
        future=future,
        ready_winner="commit_decided",
    )
    allow_owner = asyncio.Event()
    owner_observations = 0

    async def owning_task() -> None:
        nonlocal owner_observations
        await allow_owner.wait()
        try:
            future.result()
        except ExpectedFailure:
            owner_observations += 1
        finally:
            terminals._clear_consumed_deferred_call(
                terminal_id, generation, call,
            )

    task = asyncio.create_task(owning_task())

    class CompletionBeforeCancelLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback) -> None:
            # Model the worker completing immediately after quiescence snapshots
            # current_call but before its queued task.cancel callback executes.
            future.set_exception(ExpectedFailure("ready failed"))
            allow_owner.set()
            loop.call_soon(callback)

    record = terminals._DeferredTaskRecord(
        task=task,
        loop=CompletionBeforeCancelLoop(),
        generation=generation,
        current_call=call,
    )
    terminals._deferred_tasks_by_terminal[terminal_id] = record
    quiesce_observations = 0
    try:
        try:
            await terminals.quiesce_deferred_terminal(terminal_id, timeout_s=0.2)
        except ExpectedFailure:
            quiesce_observations += 1
        assert task.done()
        assert owner_observations + quiesce_observations == 1
    finally:
        terminals._deferred_tasks_by_terminal.pop(terminal_id, None)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

