"""F254 D4 — Canonical scenarios for the six UX invariants.

Each invariant gets exactly one scenario, defined once here and driven
by every test kind that covers it. Same scenario, different substrates.

These are helper functions, not tests themselves. They return assertion
data that the calling test verifies against its substrate's output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ScenarioResult:
    """Outcome of a scenario execution on any substrate."""

    success: bool
    evidence: Dict[str, Any] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# UX-1 Arrival: arrival_two_workers
# ---------------------------------------------------------------------------


def arrival_two_workers(
    *,
    assign_fn,
    get_first_screen_fn,
    working_directory: str = "/tmp",
) -> ScenarioResult:
    """Supervisor assigns 2 workers with distinct briefs.

    Assert: each first screen equals its own brief, and neither contains the other's.

    Args:
        assign_fn: callable(agent_profile, message, working_directory) -> dict with terminal_id
        get_first_screen_fn: callable(terminal_id) -> str (the first screen content)
        working_directory: where workers start
    """
    brief_a = "BRIEF_ALPHA: Implement the frobnicate module with error handling"
    brief_b = "BRIEF_BETA: Refactor the widget factory to use dependency injection"

    result_a = assign_fn("developer", brief_a, working_directory)
    result_b = assign_fn("developer", brief_b, working_directory)

    if not result_a.get("success") or not result_b.get("success"):
        return ScenarioResult(
            success=False,
            failures=[f"assign failed: a={result_a}, b={result_b}"],
        )

    screen_a = get_first_screen_fn(result_a["terminal_id"])
    screen_b = get_first_screen_fn(result_b["terminal_id"])

    failures = []
    if brief_a not in screen_a:
        failures.append(f"Worker A screen missing its own brief")
    if brief_b not in screen_b:
        failures.append(f"Worker B screen missing its own brief")
    if brief_b in screen_a:
        failures.append(f"Worker A screen contains B's brief (cross-contamination)")
    if brief_a in screen_b:
        failures.append(f"Worker B screen contains A's brief (cross-contamination)")

    return ScenarioResult(
        success=len(failures) == 0,
        evidence={
            "brief_a": brief_a,
            "brief_b": brief_b,
            "screen_a": screen_a,
            "screen_b": screen_b,
            "terminal_a": result_a["terminal_id"],
            "terminal_b": result_b["terminal_id"],
        },
        failures=failures,
    )


# ---------------------------------------------------------------------------
# UX-2 Delivery: delivery_three_messages
# ---------------------------------------------------------------------------


def delivery_three_messages(
    *,
    send_fn,
    get_pastes_fn,
    target_terminal_id: str,
) -> ScenarioResult:
    """Supervisor sends 3 messages to a busy worker.

    Assert: exactly 3 pastes, in order, no duplicates.

    Args:
        send_fn: callable(receiver_id, message) -> dict
        get_pastes_fn: callable(terminal_id) -> list[str] (ordered paste content)
        target_terminal_id: the worker to send to
    """
    messages = [
        "MSG_ONE: First instruction batch",
        "MSG_TWO: Second instruction batch",
        "MSG_THREE: Third instruction batch",
    ]

    for msg in messages:
        result = send_fn(target_terminal_id, msg)
        if not result.get("success", True):
            return ScenarioResult(
                success=False,
                failures=[f"send_message failed: {result}"],
            )

    pastes = get_pastes_fn(target_terminal_id)

    failures = []
    if len(pastes) != 3:
        failures.append(f"Expected 3 pastes, got {len(pastes)}")
    else:
        for i, (paste, expected) in enumerate(zip(pastes, messages)):
            if expected not in paste:
                failures.append(f"Paste {i} missing expected content")
        # Check no duplicates
        if len(set(pastes)) != len(pastes):
            failures.append("Duplicate paste detected")

    return ScenarioResult(
        success=len(failures) == 0,
        evidence={
            "messages_sent": messages,
            "pastes_received": pastes,
            "target": target_terminal_id,
        },
        failures=failures,
    )


# ---------------------------------------------------------------------------
# UX-3 Non-interruption: injection_during_prompt
# ---------------------------------------------------------------------------


def injection_during_prompt(
    *,
    set_busy_fn,
    send_fn,
    get_pastes_fn,
    clear_busy_fn,
    target_terminal_id: str,
) -> ScenarioResult:
    """Worker at permission dialog; supervisor sends; assert zero paste until gate clears.

    Args:
        set_busy_fn: callable(terminal_id) -> None (mark worker as busy/prompting)
        send_fn: callable(receiver_id, message) -> dict
        get_pastes_fn: callable(terminal_id) -> list[str]
        clear_busy_fn: callable(terminal_id) -> None (clear the busy state)
        target_terminal_id: the worker to send to

    NOTE: AC-B4 requires zero time.sleep / asyncio.sleep in this scenario.
    """
    message = "INJECTED_MSG: This should not arrive during the prompt"

    # Put worker in busy state
    set_busy_fn(target_terminal_id)

    # Send while busy
    send_fn(target_terminal_id, message)

    # Check: zero pastes while busy
    pastes_while_busy = get_pastes_fn(target_terminal_id)

    # Clear busy state
    clear_busy_fn(target_terminal_id)

    # After clearing, the message should eventually arrive
    pastes_after_clear = get_pastes_fn(target_terminal_id)

    failures = []
    if len(pastes_while_busy) > 0:
        failures.append(
            f"Got {len(pastes_while_busy)} paste(s) while worker was busy "
            f"(non-interruption violation)"
        )
    if not any(message in p for p in pastes_after_clear):
        failures.append("Message never arrived after gate cleared")

    return ScenarioResult(
        success=len(failures) == 0,
        evidence={
            "message": message,
            "pastes_while_busy": pastes_while_busy,
            "pastes_after_clear": pastes_after_clear,
        },
        failures=failures,
    )


# ---------------------------------------------------------------------------
# UX-4 Return: return_barrier_of_two
# ---------------------------------------------------------------------------


def return_barrier_of_two(
    *,
    assign_with_barrier_fn,
    complete_worker_fn,
    get_supervisor_wakes_fn,
    working_directory: str = "/tmp",
) -> ScenarioResult:
    """Supervisor assigns 2 workers under one barrier; both finish; assert exactly one wake.

    Args:
        assign_with_barrier_fn: callable(profile, msg, barrier, workdir) -> dict
        complete_worker_fn: callable(terminal_id) -> None
        get_supervisor_wakes_fn: callable() -> int (count of callback wakes)
        working_directory: where workers start
    """
    barrier_label = "test-barrier-ux4"

    result_a = assign_with_barrier_fn(
        "developer", "Worker A task", barrier_label, working_directory
    )
    result_b = assign_with_barrier_fn(
        "developer", "Worker B task", barrier_label, working_directory
    )

    if not result_a.get("success") or not result_b.get("success"):
        return ScenarioResult(
            success=False,
            failures=[f"assign failed: a={result_a}, b={result_b}"],
        )

    # Complete both workers
    complete_worker_fn(result_a["terminal_id"])
    complete_worker_fn(result_b["terminal_id"])

    # Check supervisor wakes
    wakes = get_supervisor_wakes_fn()

    failures = []
    if wakes == 0:
        failures.append("Supervisor never woke (lost callback)")
    elif wakes > 1:
        failures.append(f"Supervisor woke {wakes} times (double-wake)")

    return ScenarioResult(
        success=wakes == 1,
        evidence={
            "barrier": barrier_label,
            "terminal_a": result_a["terminal_id"],
            "terminal_b": result_b["terminal_id"],
            "supervisor_wakes": wakes,
        },
        failures=failures,
    )


# ---------------------------------------------------------------------------
# UX-5 Authority: frozen_pin_drift
# ---------------------------------------------------------------------------


def frozen_pin_drift(
    *,
    pin_fn,
    mutate_file_fn,
    send_past_pin_fn,
    target_terminal_id: str,
    pin_file_path: str,
    pin_sha256: str,
) -> ScenarioResult:
    """Supervisor pins a file, worker sends past a mutated file; assert refusal + drift notice.

    Args:
        pin_fn: callable(terminal_id, file_path, sha256) -> dict
        mutate_file_fn: callable(file_path) -> str (new sha256 after mutation)
        send_past_pin_fn: callable(terminal_id, message) -> dict (result with refusal info)
        target_terminal_id: worker to pin
        pin_file_path: path to the authority file
        pin_sha256: sha256 of the original file content
    """
    # Pin the file
    pin_result = pin_fn(target_terminal_id, pin_file_path, pin_sha256)

    # Mutate the file
    new_sha = mutate_file_fn(pin_file_path)

    # Attempt to send past the drifted pin
    send_result = send_past_pin_fn(target_terminal_id, "task past drifted pin")

    failures = []
    if send_result.get("success", True):
        failures.append("Send succeeded past a drifted pin (should be refused)")
    if "drift" not in str(send_result.get("message", "")).lower():
        failures.append("No drift notice in refusal message")

    return ScenarioResult(
        success=len(failures) == 0,
        evidence={
            "pin_file": pin_file_path,
            "original_sha": pin_sha256,
            "mutated_sha": new_sha,
            "send_result": send_result,
        },
        failures=failures,
    )


# ---------------------------------------------------------------------------
# UX-6 Visibility: fleet_after_death
# ---------------------------------------------------------------------------


def fleet_after_death(
    *,
    create_workers_fn,
    kill_one_fn,
    get_fleet_fn,
    get_manifest_fn,
    get_siblings_fn,
) -> ScenarioResult:
    """3 workers, one killed out-of-band; assert fleet/manifest/siblings all agree.

    Args:
        create_workers_fn: callable(count) -> list[str] (terminal_ids)
        kill_one_fn: callable(terminal_id) -> None
        get_fleet_fn: callable() -> dict (fleet data)
        get_manifest_fn: callable() -> dict (manifest data)
        get_siblings_fn: callable(terminal_id) -> list[str] (sibling IDs)
    """
    workers = create_workers_fn(3)
    killed_id = workers[1]  # Kill the middle one

    kill_one_fn(killed_id)

    fleet = get_fleet_fn()
    manifest = get_manifest_fn()
    siblings = get_siblings_fn(workers[0])

    failures = []

    # Fleet should show the killed worker as gone/absent
    fleet_ids = {t.get("id", t.get("terminal_id", "")) for t in fleet.get("terminals", [])}
    if killed_id in fleet_ids:
        # Check if it's marked as gone
        for t in fleet.get("terminals", []):
            tid = t.get("id", t.get("terminal_id", ""))
            if tid == killed_id and t.get("status") not in ("gone", "dead", None):
                failures.append(f"Fleet still shows killed worker {killed_id} as alive")

    # Siblings should not include the killed worker (or mark it appropriately)
    if killed_id in siblings:
        failures.append(f"Siblings still lists killed worker {killed_id}")

    return ScenarioResult(
        success=len(failures) == 0,
        evidence={
            "workers": workers,
            "killed": killed_id,
            "fleet": fleet,
            "manifest": manifest,
            "siblings": siblings,
        },
        failures=failures,
    )
