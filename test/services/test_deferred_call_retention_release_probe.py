"""Claim attacked: retained call slots are released after lawful ownership.

Round: WPM4-A diff gate r10.
Expected post-fix semantics: repeated normally consumed calls leave no retained
slot, and a completed Future observed by quiescence releases the production
registry entry after the cancelled owner task finishes.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r10/test_deferred_call_retention_release_probe.py
"""

from __future__ import annotations

import asyncio
import concurrent.futures

import pytest

from cli_agent_orchestrator.services import terminal_service as terminals
from cli_agent_orchestrator.services.deferred_dispatcher import DeferredCall


@pytest.mark.asyncio
async def test_repeated_normal_consumption_clears_single_retained_slot():
    terminal_id = "normal-retention-release"
    generation = "generation"
    start = asyncio.Event()
    record = None

    async def owner() -> None:
        await start.wait()
        for value in range(100):
            result, _grant = await terminals._tracked_blocking(
                terminal_id,
                generation,
                "abandonable",
                "retention_probe",
                lambda item: item,
                value,
            )
            assert result == value
            assert record.current_call is None

    task = asyncio.create_task(owner())
    record = terminals._DeferredTaskRecord(
        task=task,
        loop=asyncio.get_running_loop(),
        generation=generation,
    )
    terminals._deferred_tasks_by_terminal[terminal_id] = record
    try:
        start.set()
        await task
        assert record.current_call is None
    finally:
        terminals._deferred_tasks_by_terminal.pop(terminal_id, None)


@pytest.mark.asyncio
async def test_quiescence_observation_releases_completed_registry_entry():
    terminal_id = "quiesce-retention-release"
    generation = "generation"
    parked = asyncio.Event()

    async def owner() -> None:
        await parked.wait()

    task = asyncio.create_task(owner())
    future: concurrent.futures.Future = concurrent.futures.Future()
    future.set_exception(RuntimeError("observed failure"))
    call = DeferredCall(
        terminal_id=terminal_id,
        generation=generation,
        call_type="abandonable",
        operation="capture_persist",
        future=future,
    )
    record = terminals._DeferredTaskRecord(
        task=task,
        loop=asyncio.get_running_loop(),
        generation=generation,
        current_call=call,
    )
    terminals._deferred_tasks_by_terminal[terminal_id] = record

    def production_done(_completed) -> None:
        current = terminals._deferred_tasks_by_terminal.get(terminal_id)
        if (
            current is record
            and (current.current_call is None or current.current_call.future.done())
        ):
            terminals._deferred_tasks_by_terminal.pop(terminal_id, None)

    task.add_done_callback(production_done)
    try:
        with pytest.raises(RuntimeError, match="observed failure"):
            await terminals.quiesce_deferred_terminal(terminal_id, timeout_s=0.2)
        await asyncio.sleep(0)
        assert terminal_id not in terminals._deferred_tasks_by_terminal
    finally:
        terminals._deferred_tasks_by_terminal.pop(terminal_id, None)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

