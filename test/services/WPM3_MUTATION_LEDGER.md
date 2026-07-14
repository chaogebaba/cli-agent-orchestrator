# WPM3 r2 actual mutation ledger

Generated from isolated copies of the current dirty `src/`. A row is `KILLED`
only when its exact pytest command exits nonzero. Each section includes the
applied hunk, exact command, one-line failure, and live-file restoration hash.

| Mutant | Result | Exit | Production-path test |
|---|---|---:|---|
| h1_eager_reassign | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_eager_flag_cannot_disable_s4_eligibility` |

## h1_eager_reassign

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h1_eager_reassign/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_eager_flag_cannot_disable_s4_eligibility`
- Failure: `E       AssertionError: assert 0 == 1`
- Post-restore SHA-256: `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab` (baseline `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h1_eager_reassign/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:42:28.129962699 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h1_eager_reassign/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:55:05.849554989 -0400
@@ -628,7 +628,7 @@
                                 if provider is None:
                                     provider = provider_manager.get_provider(terminal_id)
                                 eager_eligible = admission_kind == "s4_initial"
-                            elif EAGER_INBOX_DELIVERY and status in (
+                            if EAGER_INBOX_DELIVERY and status in (
                                 TerminalStatus.PROCESSING,
                                 TerminalStatus.WAITING_USER_ANSWER,
                             ):
```
| h2a_recovery_direct_bag | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_recovery_rejects_evidence_bag_as_expected_ref` |

## h2a_recovery_direct_bag

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h2a_recovery_direct_bag/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_recovery_rejects_evidence_bag_as_expected_ref`
- Failure: `E       AssertionError: assert [] == [{'cursor_ver...inding', ...}]`
- Post-restore SHA-256: `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab` (baseline `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h2a_recovery_direct_bag/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:42:28.129962699 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h2a_recovery_direct_bag/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:55:07.676422318 -0400
@@ -947,8 +947,9 @@
             if resolution is None:
                 lookup, lookup_evidence = "unresolved", {"kind": "transcript_unresolved"}
             else:
-                lookup, lookup_evidence = _wpm2_lookup(
-                    metadata, attempt["payload_hash"], attempt.get("started_at"), evidence)
+                path = getattr(resolution, "path", resolution)
+                lookup, lookup_evidence = transcript_lookup(
+                    path, attempt["payload_hash"], attempt.get("started_at"), evidence)
             if lookup == "hit":
                 result = recover_wpm2_stale_attempt(
                     attempt_uuid, message_ids, MessageStatus.DELIVERED,
```
| h2b_preopen_direct_bag | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_preopen_dedup_uses_canonical_lookup` |

## h2b_preopen_direct_bag

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h2b_preopen_direct_bag/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_preopen_dedup_uses_canonical_lookup`
- Failure: `E           AssertionError: Expected '_wpm2_lookup' to have been called.`
- Post-restore SHA-256: `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab` (baseline `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h2b_preopen_direct_bag/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:42:28.129962699 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h2b_preopen_direct_bag/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:55:09.166173349 -0400
@@ -648,8 +648,9 @@
                                 prior_evidence = json.loads(prior.get("evidence") or "{}")
                             except (TypeError, json.JSONDecodeError):
                                 prior_evidence = {}
-                            result, evidence = _wpm2_lookup(
-                                metadata, prior["payload_hash"], prior.get("started_at"),
+                            path = getattr(resolution, "path", resolution)
+                            result, evidence = transcript_lookup(
+                                path, prior["payload_hash"], prior.get("started_at"),
                                 prior_evidence)
                             if result == "hit":
                                 won = confirm_batch_from_prior_attempt(
```
| h3_member | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_on_member_set_mismatch` |

## h3_member

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_member/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_on_member_set_mismatch`
- Failure: `E       AssertionError: assert 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_member/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_member/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:10.931662517 -0400
@@ -1654,7 +1654,7 @@
 
 
 def _corrective_evidence_valid(prior: dict[str, Any], candidate_ids: list[int]) -> bool:
-    if prior["members"] != candidate_ids:
+    if False and prior["members"] != candidate_ids:
         return False
     evidence = _evidence_object(prior.get("evidence"))
     if _valid_cursor(evidence.get("last_observed_ref")) is None:
```
| h3_cursor | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_without_persisted_anchor` |

## h3_cursor

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_cursor/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_without_persisted_anchor`
- Failure: `E       AssertionError: assert 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_cursor/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_cursor/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:12.445579498 -0400
@@ -1657,7 +1657,7 @@
     if prior["members"] != candidate_ids:
         return False
     evidence = _evidence_object(prior.get("evidence"))
-    if _valid_cursor(evidence.get("last_observed_ref")) is None:
+    if False and _valid_cursor(evidence.get("last_observed_ref")) is None:
         return False
     anchor = evidence.get("injection_completed_seq")
     exhausted_at = evidence.get("boundary_exhausted_at")
```
| h3_anchor | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_without_persisted_anchor` |

## h3_anchor

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_anchor/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_without_persisted_anchor`
- Failure: `E       AssertionError: assert 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_anchor/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_anchor/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:13.766219751 -0400
@@ -1659,7 +1659,7 @@
     evidence = _evidence_object(prior.get("evidence"))
     if _valid_cursor(evidence.get("last_observed_ref")) is None:
         return False
-    anchor = evidence.get("injection_completed_seq")
+    anchor = evidence.get("injection_completed_seq") or {"observation_epoch": "epoch", "seq": 1}
     exhausted_at = evidence.get("boundary_exhausted_at")
     snapshot = evidence.get("boundary_snapshot")
     if (not isinstance(anchor, dict) or not isinstance(exhausted_at, str)
```
| h3_exhaustion_half | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_snapshot_only_row` |

## h3_exhaustion_half

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_exhaustion_half/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_snapshot_only_row`
- Failure: `E       AssertionError: assert 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_exhaustion_half/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_exhaustion_half/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:15.226235497 -0400
@@ -1660,7 +1660,7 @@
     if _valid_cursor(evidence.get("last_observed_ref")) is None:
         return False
     anchor = evidence.get("injection_completed_seq")
-    exhausted_at = evidence.get("boundary_exhausted_at")
+    exhausted_at = evidence.get("boundary_exhausted_at") or "mutant"
     snapshot = evidence.get("boundary_snapshot")
     if (not isinstance(anchor, dict) or not isinstance(exhausted_at, str)
             or not exhausted_at or not isinstance(snapshot, dict)):
```
| h3_snapshot_half | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_exhaustion_only_row` |

## h3_snapshot_half

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_snapshot_half/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_exhaustion_only_row`
- Failure: `E       AssertionError: assert 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_snapshot_half/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_snapshot_half/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:16.785452359 -0400
@@ -1661,7 +1661,7 @@
         return False
     anchor = evidence.get("injection_completed_seq")
     exhausted_at = evidence.get("boundary_exhausted_at")
-    snapshot = evidence.get("boundary_snapshot")
+    snapshot = evidence.get("boundary_snapshot") or {"observation_epoch": "epoch", "status": "completed", "status_gen": 3, "input_gen": 1, "seq": 4, "last_non_ready_seq": 2, "last_ready_seq": 4}
     if (not isinstance(anchor, dict) or not isinstance(exhausted_at, str)
             or not exhausted_at or not isinstance(snapshot, dict)):
         return False
```
| h3_snapshot_shape | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_snapshot_only_row` |

## h3_snapshot_shape

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_snapshot_shape/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_snapshot_only_row`
- Failure: `E       AssertionError: assert 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_snapshot_shape/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_snapshot_shape/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:18.106848239 -0400
@@ -1670,7 +1670,7 @@
         "observation_epoch", "status", "status_gen", "input_gen", "seq",
         "last_non_ready_seq", "last_ready_seq",
     }
-    if (set(snapshot) != required or not isinstance(epoch, str) or not epoch
+    if (False or not isinstance(epoch, str) or not epoch
             or type(anchor_seq) is not int
             or snapshot.get("observation_epoch") != epoch
             or snapshot.get("status") not in {
```
| h3_cycle_order | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_snapshot_only_row` |

## h3_cycle_order

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_cycle_order/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_snapshot_only_row`
- Failure: `E       AssertionError: assert 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_cycle_order/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_cycle_order/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:19.633488446 -0400
@@ -1684,7 +1684,7 @@
         return False
     non_ready = snapshot["last_non_ready_seq"]
     ready = snapshot["last_ready_seq"]
-    return anchor_seq < non_ready < ready <= snapshot["seq"]
+    return True
 
 
 def _admission_valid(
```
| h3_outcome_reason | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_without_persisted_anchor` |

## h3_outcome_reason

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_outcome_reason/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_without_persisted_anchor`
- Failure: `E       AssertionError: assert 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_outcome_reason/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_outcome_reason/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:21.049771916 -0400
@@ -1696,8 +1696,7 @@
             "delivery_deferred", "input_blocked"} for row in history)
     if kind == "corrective":
         prior = next((row for row in history if row["attempt_uuid"] == prior_uuid), None)
-        return bool(prior and prior["outcome"] == "ambiguous" and
-                    prior["reason"] == "confirmation_timeout" and
+        return bool(prior and
                     _corrective_evidence_valid(prior, candidate_ids) and
                     not any(row["prior_attempt_uuid"] == prior_uuid for row in history))
     return True
```
| h3_successor | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_opens_with_full_fingerprint` |

## h3_successor

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_successor/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_opens_with_full_fingerprint`
- Failure: `E       AssertionError: assert 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_successor/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_successor/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:22.386527857 -0400
@@ -1699,7 +1699,7 @@
         return bool(prior and prior["outcome"] == "ambiguous" and
                     prior["reason"] == "confirmation_timeout" and
                     _corrective_evidence_valid(prior, candidate_ids) and
-                    not any(row["prior_attempt_uuid"] == prior_uuid for row in history))
+                    True)
     return True
 
 
```
| h3_fingerprint | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_on_fingerprint_mismatch` |

## h3_fingerprint

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_fingerprint/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_corrective_refused_on_fingerprint_mismatch`
- Failure: `E       AssertionError: assert 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_fingerprint/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_fingerprint/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:23.905329097 -0400
@@ -1721,8 +1721,7 @@
             db.rollback()
             return "delivering_conflict"
         history = _attempt_history_in_db(db, ids)
-        if (_history_fingerprint(history) != proof.fingerprint or
-                not _admission_valid(proof.kind, history, proof.prior_attempt_uuid, ids)):
+        if not _admission_valid(proof.kind, history, proof.prior_attempt_uuid, ids):
             db.rollback()
             return "stale_admission"
         if proof.transcript_checks:
```
| h3_bounded_d2 | KILLED | 1 | `test/services/test_wpm2_delivery_soundness.py::test_wpm2_corrective_d2_hit_between_preflight_and_open_is_stale_admission` |

## h3_bounded_d2

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_bounded_d2/src uv run pytest -q -c /dev/null test/services/test_wpm2_delivery_soundness.py::test_wpm2_corrective_d2_hit_between_preflight_and_open_is_stale_admission`
- Failure: `E       AssertionError: assert (1 and 'opened' == 'stale_admission'`
- Post-restore SHA-256: `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da` (baseline `7c84eb792ccc8576304939c35f0451618819c600f39b9b2eeabb0de3c296e2da`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_bounded_d2/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:45:06.216667597 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h3_bounded_d2/src/cli_agent_orchestrator/clients/database.py	2026-07-13 19:55:25.246420705 -0400
@@ -1760,7 +1760,7 @@
                     db.rollback()
                     return "stale_admission"
                 outcome, _ = bounded_transcript_suffix_lookup(cursor, payloads)
-                if outcome != "absent":
+                if False and outcome != "absent":
                     db.rollback()
                     return "stale_admission"
         candidates = db.query(InboxModel).filter(
```
| h4_restore_bypass | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_invalid_snapshot_transient_no_loss_increment` |

## h4_restore_bypass

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h4_restore_bypass/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_invalid_snapshot_transient_no_loss_increment`
- Failure: `E           ValueError: Terminal receiver not found in database`
- Post-restore SHA-256: `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab` (baseline `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h4_restore_bypass/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:42:28.129962699 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h4_restore_bypass/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:55:26.567271908 -0400
@@ -333,8 +333,8 @@
         status = (snapshot.status if snapshot is not None else
                   status_monitor.get_status(terminal_id))
         newest_evidence = decoded[newest["attempt_uuid"]]
-        protection = classify_permanently_d2_only(
-            newest, snapshot.observation_epoch if snapshot is not None else None)
+        protection = ("normal" if snapshot is None else classify_permanently_d2_only(
+            newest, snapshot.observation_epoch))
         last_activity = newest_evidence.get("last_activity_at")
         updates: dict[str, object] = {
             "last_observed_status": status.value,
```
| h6a_generic_fallthrough | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_post_submit_settled_stops_wake_multi_group` |

## h6a_generic_fallthrough

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h6a_generic_fallthrough/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_post_submit_settled_stops_wake_multi_group`
- Failure: `E       AssertionError: assert ['one', 'two'] == ['one']`
- Post-restore SHA-256: `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab` (baseline `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h6a_generic_fallthrough/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:42:28.129962699 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h6a_generic_fallthrough/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:55:27.717075074 -0400
@@ -874,7 +874,6 @@
                             result = settle_delivery_attempt_proof_safe(
                                 attempt_uuid, submit_evidence or {},
                                 status_monitor.get_status_gen(terminal_id))
-                            return
                     for message in batch:
                         logger.error(
                             f"Failed to deliver message {message.id} to {terminal_id}: {e}"
```
| h6b_terminal_not_found_fallthrough | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_post_submit_terminal_not_found_stops_wake_multi_group` |

## h6b_terminal_not_found_fallthrough

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h6b_terminal_not_found_fallthrough/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_post_submit_terminal_not_found_stops_wake_multi_group`
- Failure: `E       AssertionError: assert ['one', 'two'] == ['one']`
- Post-restore SHA-256: `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab` (baseline `0f02f1141ef456dfe9b6a1bdc1f30458aeb85c8a98687880f5182e3bbd6b0dab`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h6b_terminal_not_found_fallthrough/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:42:28.129962699 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h6b_terminal_not_found_fallthrough/src/cli_agent_orchestrator/services/inbox_service.py	2026-07-13 19:55:28.909927878 -0400
@@ -852,7 +852,6 @@
                             settle_delivery_attempt_proof_safe(
                                 attempt_uuid, submit_evidence,
                                 status_monitor.get_status_gen(terminal_id))
-                            return
                         else:
                             settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING, "interrupted",
                                                     reason="terminal_not_found", error=str(e))
```
| h7_epoch_pop | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_epoch_maps_popped_on_terminal_free` |

## h7_epoch_pop

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_epoch_pop/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_epoch_maps_popped_on_terminal_free`
- Failure: `E           AssertionError: assert 'term' not in {'term': 'old'}`
- Post-restore SHA-256: `09b72b4f79dab7f7e88fe1a67b6db10e6282fe60e8559984b20439e3a640a504` (baseline `09b72b4f79dab7f7e88fe1a67b6db10e6282fe60e8559984b20439e3a640a504`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_epoch_pop/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/status_monitor.py	2026-07-13 19:33:34.575208541 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_epoch_pop/src/cli_agent_orchestrator/services/status_monitor.py	2026-07-13 19:55:30.160880179 -0400
@@ -828,7 +828,7 @@
             self._input_gen.pop(terminal_id, None)
             self._processing_gen.pop(terminal_id, None)
             self._status_gen.pop(terminal_id, None)
-            self._observation_epoch.pop(terminal_id, None)
+            # mutant omitted observation epoch pop
             self._observation_seq.pop(terminal_id, None)
             self._last_non_ready_seq.pop(terminal_id, None)
             self._last_ready_seq.pop(terminal_id, None)
```
| h7_seq_pop | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_epoch_maps_popped_on_terminal_free` |

## h7_seq_pop

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_seq_pop/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_epoch_maps_popped_on_terminal_free`
- Failure: `E           AssertionError: assert 'term' not in {'term': 9}`
- Post-restore SHA-256: `09b72b4f79dab7f7e88fe1a67b6db10e6282fe60e8559984b20439e3a640a504` (baseline `09b72b4f79dab7f7e88fe1a67b6db10e6282fe60e8559984b20439e3a640a504`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_seq_pop/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/status_monitor.py	2026-07-13 19:33:34.575208541 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_seq_pop/src/cli_agent_orchestrator/services/status_monitor.py	2026-07-13 19:55:31.226179077 -0400
@@ -829,7 +829,7 @@
             self._processing_gen.pop(terminal_id, None)
             self._status_gen.pop(terminal_id, None)
             self._observation_epoch.pop(terminal_id, None)
-            self._observation_seq.pop(terminal_id, None)
+            # mutant omitted observation seq pop
             self._last_non_ready_seq.pop(terminal_id, None)
             self._last_ready_seq.pop(terminal_id, None)
             self._fifo_frame_seq.pop(terminal_id, None)
```
| h7_non_ready_pop | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_epoch_maps_popped_on_terminal_free` |

## h7_non_ready_pop

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_non_ready_pop/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_epoch_maps_popped_on_terminal_free`
- Failure: `E           AssertionError: assert 'term' not in {'term': 7}`
- Post-restore SHA-256: `09b72b4f79dab7f7e88fe1a67b6db10e6282fe60e8559984b20439e3a640a504` (baseline `09b72b4f79dab7f7e88fe1a67b6db10e6282fe60e8559984b20439e3a640a504`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_non_ready_pop/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/status_monitor.py	2026-07-13 19:33:34.575208541 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_non_ready_pop/src/cli_agent_orchestrator/services/status_monitor.py	2026-07-13 19:55:32.396180323 -0400
@@ -830,7 +830,7 @@
             self._status_gen.pop(terminal_id, None)
             self._observation_epoch.pop(terminal_id, None)
             self._observation_seq.pop(terminal_id, None)
-            self._last_non_ready_seq.pop(terminal_id, None)
+            # mutant omitted last non-ready pop
             self._last_ready_seq.pop(terminal_id, None)
             self._fifo_frame_seq.pop(terminal_id, None)
             self._screens.pop(terminal_id, None)
```
| h7_ready_pop | KILLED | 1 | `test/services/test_wpm3_delivery_hardening.py::test_wpm3_epoch_maps_popped_on_terminal_free` |

## h7_ready_pop

- Result: **KILLED**
- Exit: `1`
- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_ready_pop/src uv run pytest -q -c /dev/null test/services/test_wpm3_delivery_hardening.py::test_wpm3_epoch_maps_popped_on_terminal_free`
- Failure: `E           AssertionError: assert 'term' not in {'term': 8}`
- Post-restore SHA-256: `09b72b4f79dab7f7e88fe1a67b6db10e6282fe60e8559984b20439e3a640a504` (baseline `09b72b4f79dab7f7e88fe1a67b6db10e6282fe60e8559984b20439e3a640a504`)
- Full output: `/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_ready_pop/pytest.txt`

```diff
--- /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/status_monitor.py	2026-07-13 19:33:34.575208541 -0400
+++ /home/chao/VScode_projects/cli-subagents/tmp/orch/wpm3-mutations/h7_ready_pop/src/cli_agent_orchestrator/services/status_monitor.py	2026-07-13 19:55:33.419076084 -0400
@@ -831,7 +831,7 @@
             self._observation_epoch.pop(terminal_id, None)
             self._observation_seq.pop(terminal_id, None)
             self._last_non_ready_seq.pop(terminal_id, None)
-            self._last_ready_seq.pop(terminal_id, None)
+            # mutant omitted last ready pop
             self._fifo_frame_seq.pop(terminal_id, None)
             self._screens.pop(terminal_id, None)
             self._screen_size_deferred_warned.discard(terminal_id)
```
