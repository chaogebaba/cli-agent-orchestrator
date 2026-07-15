# WPM4-E actual mutation and coverage ledger

Generated from isolated copies of the current dirty `src/`. A mutant is
`KILLED` only when its exact production-path pytest command exits nonzero.
The live source was never mutated; the post-run hashes equal the pre-run hashes.

## Acceptance coverage

| Blueprint row | Load-bearing tests |
|---|---|
| E1 ⚑ refresh A/B + timeout | `test_e1_planted_token_refresh_a_b_precedes_preamble_bake`, `test_e1_real_git_planted_token_refresh_resets_row_fresh`, `test_e1_hung_refresh_dispatch_completes_with_stale_preamble` |
| E1 async/coalesce ⚑ | `test_e1_stale_fork_is_deferred_with_refresh_base`, `test_e1_deferred_schedule_returns_while_refresh_is_running`, `test_e1_two_concurrent_stale_forks_dispatch_once_and_reset_baseline`, `test_e1_refresh_wait_is_cancelled_by_terminal_quiesce` |
| E2 ⚑ defaults/cold/fallback | `test_e2_default_base_creates_fork_context`, `test_e2_explicit_cold_and_absent_key_remain_cold`, `test_e2_retired_default_falls_back_cold_with_warning`, `test_e2_anchor_default_falls_back_without_refresh_target`, `test_e2_mark_ready_rejects_reserved_cold_before_terminal_lookup` |
| E3 ⚑ kind/anchor semantics | `test_e3_anchor_kind_round_trips_and_cold_name_is_reserved`, `test_e3_kind_migration_backfills_existing_rows_as_base`, `test_e3_anchor_is_typed_unforkable_and_absent_from_forkable_listing`, `test_e3_keep_bases_retires_anchor_but_preserves_base`, `test_e3_mark_base_ready_threads_anchor_kind` |
| E4 mock-only restart gate | all five tests in `test/cli/commands/test_redeploy.py` |

## Mutation summary

| Mutant | Result | Exit | Killing test |
|---|---|---:|---|
| `e1_skip_refresh` | KILLED | 1 | `test_e1_planted_token_refresh_a_b_precedes_preamble_bake` |
| `e1_drop_coalesce` | KILLED | 1 | `test_e1_two_concurrent_stale_forks_dispatch_once_and_reset_baseline` |
| `e2_drop_default` | KILLED | 1 | `test_e2_default_base_creates_fork_context` |
| `e2_ignore_cold` | KILLED | 1 | `test_e2_explicit_cold_and_absent_key_remain_cold` |
| `e3_allow_anchor` | KILLED | 1 | `test_e3_anchor_is_typed_unforkable_and_absent_from_forkable_listing` |
| `e3_keep_anchor` | KILLED | 1 | `test_e3_keep_bases_retires_anchor_but_preserves_base` |
| `e3_bad_backfill` | KILLED | 1 | `test_e3_kind_migration_backfills_existing_rows_as_base` |
| `e4_nontty_restart` | KILLED | 1 | `test_e4_non_tty_without_yes_fails_closed_after_install` |

## e1_skip_refresh

- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e1_skip_refresh/src uv run pytest -q -c /dev/null test/services/test_wpm4e_fork_refresh.py::test_e1_planted_token_refresh_a_b_precedes_preamble_bake`
- Failure: `E AssertionError: assert 'OLD-TOKEN' == 'NEW-TOKEN'`
- Full output: `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e1_skip_refresh/pytest.txt`
- Post-run SHA-256: `121b82c1609cab5e984961be7c01107a9f17978b0b7adca71b52e260c8be169a` (baseline identical)

```diff
-    if refresh_base_name is not None:
+    if False and refresh_base_name is not None:
```

## e1_drop_coalesce

- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e1_drop_coalesce/src uv run pytest -q -c /dev/null test/services/test_wpm4e_fork_refresh.py::test_e1_two_concurrent_stale_forks_dispatch_once_and_reset_baseline`
- Failure: `E AssertionError: dispatches=2 and writes=2, expected one of each`
- Full output: `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e1_drop_coalesce/pytest.txt`
- Post-run SHA-256: `121b82c1609cab5e984961be7c01107a9f17978b0b7adca71b52e260c8be169a` (baseline identical)

```diff
-    lock = _fork_refresh_locks.get(key)
+    lock = None
```

## e2_drop_default

- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e2_drop_default/src uv run pytest -q -c /dev/null test/mcp_server/test_wpm4e_fork_ergonomics.py::test_e2_default_base_creates_fork_context`
- Failure: `E assert None is not None`
- Full output: `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e2_drop_default/pytest.txt`
- Post-run SHA-256: `fbf6c9bc13b97e77f9aeb5618b301a3e5488047ec540efa1bd435b300be3f90d` (baseline identical)

```diff
-            fork_from = _configured_default_fork_base(agent_profile)
+            fork_from = None
```

## e2_ignore_cold

- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e2_ignore_cold/src uv run pytest -q -c /dev/null test/mcp_server/test_wpm4e_fork_ergonomics.py::test_e2_explicit_cold_and_absent_key_remain_cold`
- Failure: `E AssertionError: ForkContext(...) is not None`
- Full output: `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e2_ignore_cold/pytest.txt`
- Post-run SHA-256: `fbf6c9bc13b97e77f9aeb5618b301a3e5488047ec540efa1bd435b300be3f90d` (baseline identical)

```diff
-        if fork_from == "cold":
+        if False and fork_from == "cold":
```

## e3_allow_anchor

- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e3_allow_anchor/src uv run pytest -q -c /dev/null test/services/test_base_retirement.py::test_e3_anchor_is_typed_unforkable_and_absent_from_forkable_listing`
- Failure: `E Failed: DID NOT RAISE ForkContextError`
- Full output: `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e3_allow_anchor/pytest.txt`
- Post-run SHA-256: `1202d02f564a13fde14d311e68a6701cde9a9ba29c811e74285a647cf693f216` (baseline identical)

```diff
-    if row.get("kind", "base") == "anchor":
+    if False and row.get("kind", "base") == "anchor":
```

## e3_keep_anchor

- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e3_keep_anchor/src uv run pytest -q -c /dev/null test/services/test_session_close_service.py::test_e3_keep_bases_retires_anchor_but_preserves_base`
- Failure: `E AssertionError: assert [] == ['anchor']`
- Full output: `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e3_keep_anchor/pytest.txt`
- Post-run SHA-256: `a35a04d436b572e1c49da367d0673fa321ad4bc14163f5596298001bab031aa8` (baseline identical)

```diff
-                elif keep_bases and registration.get("kind", "base") == "base":
+                elif keep_bases:
```

## e3_bad_backfill

- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e3_bad_backfill/src uv run pytest -q -c /dev/null test/clients/test_provider_sessions.py::test_e3_kind_migration_backfills_existing_rows_as_base`
- Failure: `E AssertionError: [('legacy', 'anchor')] != [('legacy', 'base')]`
- Full output: `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e3_bad_backfill/pytest.txt`
- Post-run SHA-256: `5d13cf7f09a5dba5319afa3fdc0b40ffbd00a22c77bbe57feaf64db71f2fc25b` (baseline identical)

```diff
-                    "DEFAULT 'base'"
+                    "DEFAULT 'anchor'"
```

## e4_nontty_restart

- Command: `PYTHONPATH=/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e4_nontty_restart/src uv run pytest -q -c /dev/null test/cli/commands/test_redeploy.py::test_e4_non_tty_without_yes_fails_closed_after_install`
- Failure: `E assert 1 == 0`
- Full output: `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/tmp/orch/wpm4e-mutations/e4_nontty_restart/pytest.txt`
- Post-run SHA-256: `88ae388e36ae8f4e4f77dec12ff35081b8fff4ee5152bbd57ec2cbd743a7cbf3` (baseline identical)

```diff
-        if not _stdin_is_tty():
+        if False and not _stdin_is_tty():
```
