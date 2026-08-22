**Artifact-Path:** /data/cao-scratch/f295h2-worktree/tmp/orch/f295h2-build-report.md
**Artifact-SHA256:** 50a11476bc7f9918547df84713595d0bbf25c4c95af60f483b8c36932219738d
**Artifact-Repo-Path:** tmp/orch/f295h2-build-report.md
**Git-SHA:** b0bcfb05d164e6d647e0eda91109e666b4d2c01a
**Git-SHA-fork:** b0bcfb05d164e6d647e0eda91109e666b4d2c01a
**Ruling:** GATE-YES — 0 BLOCKER / 1 SHOULD / 2 NIT

---

## Verdict Header

| # | Severity | Claim | Amendment |
|---|----------|-------|-----------|
| S1 | SHOULD | AC15 root-side patch uses semantic diff headers (`@@ providers.toml.default [grok_cli] section @@`) instead of standard unified-diff; `git apply --check` rejects it with "No valid patches in input" | Regenerate as a proper unified diff so it can be validated with `git apply --check` and applied atomically — or document in the build report that it is a manual-apply semantic patch requiring human placement |
| N1 | NIT | Build report claims CI run 32200843394 at headSha c633326f; dispatch cites run 32201611360 at headSha 98c76ab2 | These are distinct runs (the second is the tip after the build-report commit itself). Both show the same 4-known pattern. No functional issue, but the report could note the second run for completeness |
| N2 | NIT | `_tick_wedge_inner` line 1951: `if wedge_age_s == 0: return` is dead code because the preceding clamp `max(300.0, ...)` ensures the value is always >= 300 | Remove the dead branch or move it above the clamp to honor the "0 disables" D14 escape |

---

## Empirical Checks

| # | Check | Observed Result |
|---|-------|-----------------|
| E1 | CI run 32201611360 (headSha 98c76ab2, exact tip): 4 failures adjudicated | All 4 match F303 known-pre-existing: MagicMock truthiness ×2 (Py3.14.7), quarantine `expires=''` ×2. **Zero F295h2-introduced failures.** |
| E2 | `make test-quick` — 3 new test files (33 tests) | 33 passed in 3.56s |
| E3 | `make test-quick` — 6 pre-existing suites (AC14) | 252 passed, 1 xfailed in 5.76s |
| E4 | `make test-quick` — AC12 pinned tests (`TestWorktreeInfoImmutability` + `test_update_terminal_metadata`) | 3 passed in 3.29s |
| E5 | `make test-quick` — trace manifest count assertions (37→38) | 46 passed in 6.95s |
| E6 | Source grep AC9: `send_key\|Escape\|\\x1b\|status.*ERROR\|respawn` in wedge arm diff | CLEAN — zero hits (only `delete_terminal` in notice TEXT, not code call) |
| E7 | `git apply --check` root-side patch against `/home/chao/VScode_projects/cli-subagents` | Rejected: "No valid patches in input" — semantic diff format, not standard unified diff |
| E8 | Patch content review vs AC15 | Content correct: commented `relay_preflight`/`relay_preflight_timeout_s` for providers.toml.default + full USAGE.md paragraph naming all 4 knobs |
| E9 | Working tree integrity: `git status --short` at review end | Clean — zero modifications to the working tree |
| E10 | Frozen authority pin verified pre- and post-review | VALID at both checkpoints |

---

## AC/Decision Verification

| AC/D | Verdict | Evidence |
|------|---------|----------|
| AC1 (failed preflight → no row/window) | PASS | Step 1a at `terminal_service.py:1192-1196` BEFORE F138 incarnation block; `TestConnectionRefused::test_connection_refused_raises` confirms |
| AC2 (official route → zero network calls) | PASS | `grok_preflight.py:167-168` returns immediately when no `base_url`; `TestOfficialRouteNoProbe` confirms |
| AC3 (exactly one probe at creation) | PASS | `TestExactlyOneProbe::test_one_request_on_responses_backend` asserts `call_count == 1` |
| AC4 (key redaction) | PASS | `_redact_key` at `grok_preflight.py:95-99`; `TestKeyRedaction::test_key_in_502_response` |
| AC5 (escapes: disabled + sandbox) | PASS | `grok_preflight.py:156-159` (sandbox), `:161-163` (providers.toml knob); both tests pass |
| AC6 (probe shape per api_backend) | PASS | `TestProbeShapes` covers responses/chat/unknown-degrade/unknown-connrefused |
| AC7 (wedge fires once, F228-b silent) | PASS | `TestWedgeArmFiringAndDedup::test_fires_once_on_grok_cli_after_age_threshold` + `test_f228b_arm_does_not_fire_while_spinner_animating` |
| AC8 (liveness_exclude_patterns) | PASS | `liveness_exclude_patterns = [PROCESSING_PATTERN]` at `grok_cli.py:112`; `TestLivenessExcludePatterns` confirms |
| AC9 (flag+notify ONLY) | PASS | Source grep clean; `TestFlagAndNotifyOnly::test_no_send_keys_no_status_write_no_reap` |
| AC10 (wedge_suspect in fleet + clears) | PASS | `fleet_service.py:219-220` projects; `TestWedgeFlagProjection` (3 tests) |
| AC11 (reaped terminal safety) | PASS | `_evaluate_wedge` checks `get_terminal_metadata(terminal_id) is None`; `TestReapedTerminalNotFlagged` |
| AC12 (worker cannot erase system flag) | PASS | `update_terminal_metadata` preserves `cao` key, strips worker-provided `cao`; `TestSystemMetadataProtection` (4 tests) + pre-existing 3 tests green |
| AC13 (Half 1 stamps survive — legacy fallback) | PASS | Both `fleet_service._is_config_stale` and `grok_config_watcher._count_stale_grok_terminals` read `cao` namespace first, fall back to top-level; `TestLegacyFallback` (3 tests) |
| AC14 (pre-existing suites unmodified) | PASS | 252 passed, 1 xfailed — all pre-existing test files byte-identical to baseline except mock target repoint in `test_f295_grok_config_lifecycle.py` |
| AC15 (root-side knobs documented) | PASS (content) | Patch content satisfies AC15. Format is semantic-diff, not git-applicable (see S1) |
| D1 (generic hook, Step 1a before resources) | PASS | `base.py:93` classmethod + `terminal_service.py:1192-1196` before F138 block |
| D2 (probe only on base_url) | PASS | Returns None when no `base_url` |
| D3 (probe shape per api_backend) | PASS | Three branches: responses, chat/openai, else transport-only |
| D4 (fail-closed + structured) | PASS | `RelayPreflightFailed` mirrors `NativeHomeIsolationUnavailable` |
| D5 (reads canonical only, never writes) | PASS | `_canonical_config_path()` → reads `~/.grok/config.toml`, no writes |
| D6 (key never surfaced) | PASS | `_redact_key` strips before detail construction |
| D7 (wedge arm inside F228-b) | PASS | `tick_wedge()` added to the same watchdog class, called in same run loop |
| D8 (liveness_exclude_patterns) | PASS | `grok_cli.py:112` |
| D9 (absolute time, grok_cli only) | PASS | `meta.get("provider") != "grok_cli"` guard + age criterion |
| D10 (flag+notify only) | PASS | No send_keys, no status write, no reap in the arm |
| D11 (recipient fallback + reaped safety) | PASS | Caller → supervisor fallback; terminal existence check before flag |
| D12 (reserved `cao` key, preserved on replace) | PASS | `_SYSTEM_KEY = "cao"`, stripped from worker writes, preserved on replace |
| D13 (knobs location) | PASS | Preflight in providers.toml, wedge in ConfigService |
| D14 (defaults: 20s preflight, 900s wedge, clamp [300,7200]) | PASS | `grok_preflight.py:153` default 20; `stalled_callback_watchdog.py:1949-1950` default 900, clamp |

---

## Zero-Decision Ruling

**Zero-decision buildable: YES** — the implementation matches the decision wall D1-D14 verbatim. A builder implementing from this pack would not need to invent any decision.

---

## Appendix

### S1 Detail — Root-Side Patch Format

The patch at `tmp/orch/f295h2-root-side.patch` uses non-standard hunk headers:
```
@@ providers.toml.default [grok_cli] section (add after existing keys) @@
@@ USAGE.md grok section (add one paragraph) @@
```
These are human-readable placement instructions, not valid unified-diff syntax. `git apply --check` rejects them. The content is correct per AC15. The recommendation is to either:
- Regenerate as a proper unified diff (verify line numbers against current root HEAD), or
- Explicitly document in the build report that this is a manual-apply semantic patch.

This is SHOULD rather than BLOCKER because: (a) the content satisfies AC15, (b) the blueprint explicitly states root-side changes are parked and verified in the root worktree, and (c) the builder declared AC15 as PARKED status in the build report.

### N2 Detail — Dead Code in Wedge Bound

```python
wedge_age_s = float(ConfigService.get("supervisor.watchdog.grok_wedge_age_s", 900.0))
wedge_age_s = max(300.0, min(7200.0, wedge_age_s))
if wedge_age_s == 0:
    return  # 0 disables (D14)
```

After `max(300.0, ...)`, `wedge_age_s` can never be 0. The D14 "0 disables" escape is unreachable. Moving the `== 0` check BEFORE the clamp would honor the documented escape. Not a correctness issue (the boolean knob `supervisor.watchdog.grok_wedge` provides the disable mechanism), but the dead code misleads readers about the D14 contract.
