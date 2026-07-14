"""Claim attacked: result ownership and closed-loop fallback compose safely.

Round: WPM4-A diff gate r11.
Expected post-fix semantics: 1,000 simultaneous loop claims choose exactly one
owner while loser cleanup still releases the slot; a loop closing between the
closed check and handoff falls back without stranding the registry record.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r11/test_deferred_result_owner_stress_probe.py
"""

from __future__ import annotations

import asyncio
import concurrent.futures

import pytest

from cli_agent_orchestrator.services import terminal_service as terminals
from cli_agent_orchestrator.services.deferred_dispatcher import DeferredCall


@pytest.mark.asyncio
async def test_result_owner_race_is_single_and_loser_clears_bookkeeping():
    task_wins = 0
    quiesce_wins = 0

    for iteration in range(1000):
        call = DeferredCall(
            terminal_id=f"owner-race-{iteration}",
            generation="generation",
            call_type="abandonable",
            operation="capture_persist",
            future=concurrent.futures.Future(),
        )
        gate = asyncio.Event()

        async def claim(owner: str) -> bool:
            await gate.wait()
            return terminals._claim_deferred_call_result(call, owner)

        owners = ("task", "quiesce") if iteration % 2 == 0 else ("quiesce", "task")
        claims = [asyncio.create_task(claim(owner)) for owner in owners]
        gate.set()
        results = await asyncio.gather(*claims)
        assert results.count(True) == 1
        assert results.count(False) == 1

        if call.result_owner == "task":
            task_wins += 1
            assert not terminals._claim_deferred_call_result(call, "quiesce")
        else:
            quiesce_wins += 1
            record = terminals._DeferredTaskRecord(
                task=asyncio.current_task(),
                loop=asyncio.get_running_loop(),
                generation=call.generation,
                current_call=call,
            )
            terminals._deferred_tasks_by_terminal[call.terminal_id] = record
            try:
                assert not terminals._clear_consumed_deferred_call(
                    call.terminal_id, call.generation, call,
                )
                assert record.current_call is None
            finally:
                terminals._deferred_tasks_by_terminal.pop(call.terminal_id, None)

    assert task_wins == 500
    assert quiesce_wins == 500


@pytest.mark.asyncio
async def test_loop_close_check_use_race_falls_back_without_strand():
    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task

    class ClosesDuringHandoff:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, _callback) -> None:
            raise RuntimeError("Event loop is closed")

    for iteration in range(200):
        terminal_id = f"closing-loop-{iteration}"
        future: concurrent.futures.Future = concurrent.futures.Future()
        call = DeferredCall(
            terminal_id=terminal_id,
            generation="generation",
            call_type="abandonable",
            operation="capture_persist",
            future=future,
            result_owner="task",
        )
        record = terminals._DeferredTaskRecord(
            task=done_task,
            loop=ClosesDuringHandoff(),
            generation=call.generation,
            current_call=call,
        )
        terminals._deferred_tasks_by_terminal[terminal_id] = record
        terminals._register_deferred_call(terminal_id, call.generation, call)
        future.set_result("done")
        assert terminal_id not in terminals._deferred_tasks_by_terminal

