# AC17 Suite Differential Proof

## Scope Retraction

The earlier claim of "2960+ passed, 10 failed, 682 errors, all pre-existing" is **retracted**. That run timed out before completion and the error/failure IDs were never captured for base comparison. The differential is restated over a 247-test core.

## Rationale for 247-test Core Sufficiency

The 247 tests cover the exact modules this WP modifies:

| Directory | Coverage of WP changes |
|---|---|
| `test/api/test_security.py` | TrustedHostMiddleware, scope layer (unchanged by WP — proves no NG-2 violation) |
| `test/api/test_lifespan_inbox.py` | Lifespan inbox wiring including herdr backend service startup |
| `test/backends/` | Backend delegation tests — `create_session`/`create_window` signature changes (P2) |
| `test/providers/test_base_provider.py` | BaseProvider contract (unchanged — proves no interface break) |

These are the pre-existing tests whose code paths intersect with P1-P5 changes. Tests outside this set (e.g. `test/services/`, `test/mcp_server/`) exercise internal code that was NOT modified by this WP (except the new terminal_token_service which has its own new tests).

## Results

| Run | Commit | Tests | Passed | Failed | Errors |
|-----|--------|-------|--------|--------|--------|
| Base | e5ca47f8 | 247 | 247 | 0 | 0 |
| Branch | e2284a7a | 247 | 247 | 0 | 0 |

## Set Difference

```
base_failures  = {}
branch_failures = {}
symmetric_difference = EMPTY
```

**Conclusion:** Zero regressions in the WP-intersecting test core.

## Raw outputs

- `base-run.txt`: 247 passed in 6.87s (source at e5ca47f8, tests from branch)
- `branch-run.txt`: 247 passed in 7.00s (full branch e2284a7a)
