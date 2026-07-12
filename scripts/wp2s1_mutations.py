#!/usr/bin/env python3
"""Run the frozen WP2S1 mutation set with byte-exact source restoration."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SVC = "src/cli_agent_orchestrator/services/provider_rebind_service.py"
TEST = "test/services/test_provider_rebind_service.py"

MUTATIONS = [
    (1, SVC, "phase = \"p9\"\n        await candidate.initialize(",
     "provider_manager.commit_provider(terminal_id, candidate, expected_current=old_provider)\n        phase = \"p9\"\n        await candidate.initialize(", f"{TEST}::test_success_order_is_initialize_then_cas_backend_raw_persist"),
    (2, SVC, "set_terminal_recovery_state(terminal_id, \"rebind_failed\", str(exc))",
     "set_terminal_recovery_state(terminal_id, \"rebound\", str(exc))", f"{TEST}::test_phase_failure_matrix"),
    (3, "src/cli_agent_orchestrator/services/terminal_service.py",
     "if not update_terminal_runtime_identity(terminal_id, session_uuid, shell):",
     "if not True:", f"{TEST}::test_eager_identity_helper_orders_capture_validate_then_atomic_persist"),
    (4, SVC, "    backend = get_backend()\n    deadline = time.monotonic() + 15.0\n    if not backend.supports_event_inbox():",
     "    return  # mutant: backend positive control removed\n    backend = get_backend()\n    deadline = time.monotonic() + 15.0\n    if not backend.supports_event_inbox():", f"{TEST}::test_tmux_proof_waits_for_real_process_chunk_frame {TEST}::test_herdr_proof_waits_for_exact_new_pane_native_event"),
    (5, SVC, "allowed.add(TerminalStatus.PROCESSING)",
     "allowed.update({TerminalStatus.PROCESSING, TerminalStatus.WAITING_USER_ANSWER})", f"{TEST}::test_quiescence_policy"),
    (6, "src/cli_agent_orchestrator/clients/database.py",
     "InboxModel.status == MessageStatus.PENDING.value,\n        ).update({\"receiver_id\": new_terminal_id}",
     "InboxModel.status != MessageStatus.PENDING.value,\n        ).update({\"receiver_id\": new_terminal_id}", "test/clients/test_database.py::TestTerminalOperations::test_fallback_settlement_moves_only_pending_and_commits_pointer"),
    (7, SVC, "results.append(await rebind_terminal(\n            terminal_id, interrupt=interrupt,\n            acknowledge_ownership=acknowledge_ownership,\n        ))",
     "results = [await rebind_terminal(\n            terminal_id, interrupt=interrupt,\n            acknowledge_ownership=acknowledge_ownership,\n        )]", f"{TEST}::test_fleet_runs_stable_one_at_a_time_and_manifest_failure_is_separate"),
    (8, "src/cli_agent_orchestrator/services/session_manifest_service.py",
     "auth_staleness = \"stale\" if started_at < auth_mtime else \"current\"",
     "auth_staleness = \"stale\"", "test/services/test_session_manifest_service.py::test_auth_staleness_current_is_observation_only"),
    (9, SVC, "lease = acquire_rebind_lease(terminal_id)",
     "lease = None", f"{TEST}::test_duplicate_rebind_returns_deterministic_busy {TEST}::test_delete_is_busy_with_zero_teardown_at_commit_boundaries"),
    (10, SVC, "stalled_callback_watchdog.resume_terminal(terminal_id, watchdog_snapshot)\n            watchdog_snapshot = None",
     "watchdog_snapshot = None", f"{TEST}::test_p14_resume_failure_demotes_proven_candidate_without_fallback"),
    (11, SVC, "if not settle_terminal_rebound(terminal_id, session_uuid, baseline):",
     "if False:", f"{TEST}::test_phase_failure_matrix"),
    (12, SVC, "if session_uuid and candidate_death_confirmed:",
     "if session_uuid:", f"{TEST}::test_candidate_exit_uncertain_blocks_fallback"),
    (13, SVC, "self.cancel.set()\n            self.release.set()",
     "pass  # mutant: cancellation signals removed", f"{TEST}::test_delivery_guard_cancelled_while_waiting_does_not_orphan_lock"),
    (14, "src/cli_agent_orchestrator/services/terminal_service.py",
     "token = acquire_rebind_lease(terminal_id)\n    if token is None:",
     "token = None\n    if False:", f"{TEST}::test_public_delete_passes_current_lease_token_to_single_teardown_body {TEST}::test_delete_is_busy_with_zero_teardown_at_commit_boundaries"),
    (15, SVC, "raw = status_monitor.get_raw_status(terminal_id, provider_override=candidate)",
     "raw = status_monitor.get_status(terminal_id)", f"{TEST}::test_transaction_p12_uses_real_raw_monitor_under_error_overlay"),
    (16, SVC, "restored = set_terminal_recovery_state(terminal_id, previous_state)",
     "restored = True", f"{TEST}::test_p6_pause_failure_restores_p1_state_without_exit"),
    (17, "src/cli_agent_orchestrator/services/session_service.py",
     "terminal_service._delete_terminal_under_lease(\n                    terminal[\"id\"], tokens[terminal[\"id\"]], registry=registry\n                )",
     "terminal_service.delete_terminal(terminal[\"id\"], registry=registry)", "test/services/test_session_service.py"),
    (18, SVC, "            if not guard_released:\n                await guard.close()",
     "            if False:  # mutant: P15 re-signal removed\n                await guard.close()",
     f"{TEST}::test_p15_first_close_failure_resignals_before_lease_release"),
    (19, SVC, "        if metadata and phase in {\"p7_send\", \"p7_death\"}:",
     "        if False:  # mutant: post-send exception quarantine removed",
     f"{TEST}::test_p7_post_send_exception_and_retry_emit_exactly_one_exit"),
    (20, SVC, "        raw = status_monitor.get_raw_status(terminal_id)",
     "        raw = (TerminalStatus.IDLE if acknowledge_ownership else status_monitor.get_raw_status(terminal_id))",
     f"{TEST}::test_acknowledged_ownership_does_not_bypass_live_quiescence"),
]


def main() -> int:
    survivors = []
    for number, relative, old, new, tests in MUTATIONS:
        path = ROOT / relative
        original = path.read_text()
        if original.count(old) != 1:
            print(f"M{number}: ERROR replacement count={original.count(old)}")
            survivors.append(number)
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            result = subprocess.run(
                ["uv", "run", "pytest", "-q", *tests.split()], cwd=ROOT,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10,
            )
            summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "no output"
            killed = result.returncode != 0
            print(f"M{number}: {'KILLED' if killed else 'SURVIVED'} | tests={tests} | {summary}")
            if not killed:
                survivors.append(number)
        except subprocess.TimeoutExpired:
            print(f"M{number}: KILLED | tests={tests} | timed out (deadlock/liveness mutation)")
        finally:
            path.write_text(original)
            cache_dir = path.parent / "__pycache__"
            if cache_dir.is_dir():
                for pyc in cache_dir.glob(f"{path.stem}.*.pyc"):
                    pyc.unlink()
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
