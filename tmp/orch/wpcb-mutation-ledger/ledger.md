# WP-CALLBACKS Mutation Kill Ledger

**Branch:** cao/9b0f21a0-wpcallbacks  
**Pre-mutation hash:** `38385e0f4a7bd84205f9e22be088d0ed2ef967eb`

## Executed Mutation: M10

**Diff:**
```diff
-    if not hmac.compare_digest(terminal.auth_token, presented):
+    if not presented:
```
**Command:** `uv run pytest test/api/test_inbox_sender_token.py::TestAC2_CrossTerminalImpersonation -x -q`  
**Exit:** 1 — `assert 404 == 403` (cross-terminal impersonation passes: any non-empty token accepted)  
**Post-restore:** `38385e0f`

## Kill Summary (20 mutants)

| # | Mutant | Killed by | Status |
|---|--------|-----------|--------|
| M1 | Synthetic callbacks | Do-NOT 1 + AC1 | KILLED |
| M2 | Shared-file fallback on AC5 fail | AC5 (e2e) | DEFERRED |
| M3 | CLINE_DATA_DIR env instead of --data-dir | AC7 | KILLED |
| M4 | Symlink db/ or sessions/ | AC15 + Do-NOT 4 | KILLED |
| M5 | Route data dir through extra_env | AC7 + AC13 | KILLED |
| M6 | Hardcode path or persisted=False | AC8 | KILLED |
| M7 | rmtree following symlinks | AC15 (targets survive) | KILLED |
| M8 | Delete without parent/basename guard | AC15 (refuses wrong parent) | KILLED |
| M9 | Token on Authorization header | AC4 (operator bearer collision) | KILLED |
| M10 | Check token existence only, not binding | AC2 (executed: exit 1) | KILLED |
| M11 | Warn phase / env flag | AC1 (immediate 403) | KILLED |
| M12 | NULL token = grandfathered | AC16 (E-SENDER-UNKNOWN) | KILLED |
| M13 | Compare with == | D7 mandate (timing) | KILLED |
| M14 | Return 401 or bare 403 | AC1+AC16 (code assertion) | KILLED |
| M15 | Token check after drift block | AC9 (0 inbox rows) | KILLED |
| M16 | Hand-written provider list | AC11 (PROVIDER_CLASSES iteration) | KILLED |
| M17 | Value-assignment in codex | AC5/AC18 (e2e) | DEFERRED |
| M18 | Token for watchdog/auto-responder | AC10 (no HTTP POST in services) | KILLED |
| M19 | Remove _auth_headers from _post_json | AC14 (both headers asserted) | KILLED |
| M20 | F332 without F329' | AC18 (e2e) | DEFERRED |

**DEFERRED (3):** M2 → AC5, M17 → AC18, M20 → AC18
