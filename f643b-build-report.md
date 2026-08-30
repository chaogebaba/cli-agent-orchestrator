# F643b (#498) — build report

**Git-SHA-fork:** f0fe570d1e85070ebaa028b8e58361bffb67f069 (fork main; branch `cao/f868fe8d-2`; worktree `/data/cao-scratch/worktrees/cli-agent-orchestrator/f868fe8d`)
**Base subject:** `Merge 'cao/f868fe8d' into main (F244 gated)` — includes the F643 fix (3899af0f) in history.
**Scope:** second-order follow-up to F643. Add a codex-0.151.0 SQLite confirmation path (A) and a scoped pane safety net (B) to F435 submit-verify; keep the F643 forked-rollout fallback. Branch cut from f0fe570d per supervisor direction.

> Report format: written in the accepted F643 structure. If the gate brief names a different canonical format, I will reformat on request — content maps 1:1.

---

## 0. Why F643 (merged) did not fix the live probe

The post-merge live probe (codex_general terminal `c4c837b8`, spawned 2026-08-30 ~19:37 EDT, **codex-cli 0.151.0**) failed the SAME "structurally unconfirmed → composer-unreadable → teardown" way. Forensics (journal + `~/.codex/sessions` + `~/.codex/*.sqlite`, read-only) showed a DIFFERENT substrate:

- The seed rollout `rollout-…19-37-02-01a05508….jsonl` (49705 bytes) was created by `codex exec` (`originator=codex_exec, source=exec, cli_version=0.151.0`) and **closed at 19:37:07** with `task_complete`/`SEED_OK`.
- The interactive `codex resume 01a05508` process wrote **no rollout record at all** — no append, **no forked JSONL** (F643's modeled shape did not occur).
- The probe task text is absent from **every** `rollout-*.jsonl`.
- What codex actually wrote at 19:37:07-08: **SQLite** — `~/.codex/thread_history_1.sqlite` (45 MB) plus `memories/state/queue` sqlite.

**Root cause (the real one):** codex-cli 0.151.0 records interactive turns in a SQLite store at the CODEX_HOME root (`thread_history_<shard>.sqlite`), NOT in the rollout JSONL. The JSONL rollout is now only the legacy `codex exec` seed artifact. F435's structural signal (a user-turn record in the rollout JSONL after baseline offset) is therefore **blind** on 0.151.0: the pinned seed never grows, no forked JSONL is created, verify always fails, and deferred_init tears down every fresh codex worker. F643's fork-follow is correct for the older fork-to-new-JSONL world (still shipped, still cheap/fail-safe) but that path is dead on 0.151.0.

(Secondary: on this specific probe the task never committed to SQLite either — thread `01a05508` holds only the 3 seed items — because the submit itself did not complete before the ~10s verify-blind teardown. That is exactly what safety net (B) is for.)

---

## 1. Empirically confirmed SQLite schema (from the real 45 MB DB)

`~/.codex/thread_history_1.sqlite`, read-only queries:

```
thread_items(thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INT,
             created_at_ms INT, item_json TEXT, item_type TEXT, updated_at_ordinal INT)
```
- `item_type` histogram includes `userMessage` (441 rows), `agentMessage`, `reasoning`, …
- **`thread_id` == the codex session uuid CAO pins as `provider_session_id`** — verified: thread `01a05508-9adc-73e0-a0bb-5c0da078415c` is exactly the probe's pinned uuid. A resume writes to the SAME thread it resumed → **no "forked-id" pinning gap** like the JSONL world.
- userMessage shape: `{"type":"userMessage","content":[{"type":"text","text":"…","text_elements":[]}]}`.
- `created_at_ms` bounds the dispatch (the seed's `Reply … SEED_OK` turn is stamped at seed time, before baseline).
- File carries a `-wal`; opened read-only, WAL tolerated.

---

## 2. The fix

File: `src/cli_agent_orchestrator/providers/codex.py` (+229 / −7)

### (A) SQLite confirmation path — the primary repair
- `_thread_history_dbs()` — globs `thread_history_*.sqlite` under the per-terminal codex home, newest-first (shard-robust). `[]` when none → additive/fail-safe.
- `_sqlite_item_json_matches(item_json, message)` — parses the userMessage JSON, extracts `content[].text` (plus bare `text`/`message`), and applies the SAME whitespace-normalized containment test as `_rollout_has_user_event` (identical match semantics across substrates).
- `_sqlite_has_user_event(session_uuid, message, baseline_wall)` — queries `thread_items` for a `userMessage` row on `thread_id == session_uuid` with `created_at_ms >= baseline_wall*1000` and a content match. Opens `mode=ro`, tolerates WAL, catches all `sqlite3.Error` → False. Never false-confirms.
- Wired into `_rollout_confirms()` as the ordered chain: **pinned JSONL → forked JSONL (F643) → SQLite**. Any one confirming suffices; all bounded by the dispatch baseline.

### (B) Scoped pane safety net — OPTION 2 (supervisor ruling)
A healthy terminal must not be torn down on verify blindness, but we must not false-confirm an unsubmitted paste, and we must not relax the existing F435 B1 anti-false-confirm guard in the JSONL world.

- `_pane_submission_verdict(backend, session, window, message, baseline)` — returns `SUBMITTED` **only** on POSITIVE evidence: `_pane_shows_new_submitted_task` (dispatch-relative, vs the pre-send baseline) shows a NEW submitted turn. An idle/empty/unreadable composer or a still-drafted task returns `""` (do not confirm).
- At final exhaustion, (B) fires **only when `_thread_history_dbs()` is non-empty** (⇒ codex ≥0.151.0, the world we proved moved) AND the SQLite store is silent AND the pane verdict is SUBMITTED. In the old JSONL world (no SQLite DB) pane-silence still RAISES — exactly as F435 B1 / the r7 test encode.
- The (B) confirm logs a WARNING naming all THREE facts — `sqlite-present=True, sqlite-silent=True, pane-submitted=True` — so a future forensics pass can count these.
- If the pane does NOT show submission (task still drafted), it still RAISES → the deferral path re-attempts the SUBMIT (never a confirm of an unsent paste).

The F643 `baseline_wall` field is reused as the dispatch cursor for both the forked-JSONL scan and the SQLite `created_at_ms` bound.

---

## 3. Tests

New file `test/providers/test_codex_submit_verify_f643b.py` (8 tests):

(A) SQLite confirmation:
- `test_task_in_sqlite_confirms` — 0.151.0 world (JSONL silent, task turn in SQLite after baseline) → confirm, no Enter.
- `test_sqlite_row_before_baseline_does_not_confirm` — the seed turn (pre-baseline) must not confirm.
- `test_sqlite_wrong_thread_does_not_confirm` — a match on a different `thread_id` must not confirm.
- `test_sqlite_content_mismatch_does_not_confirm` — different content must not confirm.
- `test_sqlite_helper_direct` — unit positive + guards (empty msg / unset wall / None uuid / wrong content).

(B) Scoped pane safety net:
- `test_sqlite_present_silent_pane_submitted_confirms_by_pane` — the residual-risk case: SQLite exists, stays silent through exhaustion, pane shows submitted → confirm-by-pane WARNING fires; asserts the three-fact log line.
- `test_no_sqlite_pane_submitted_still_raises_oldworld` — old-world (no SQLite DB): pane-submitted + JSONL silent → STILL RAISES (r7 B1 preserved).
- `test_sqlite_present_pane_unsubmitted_still_raises` — SQLite exists + silent but task still drafted → RAISES (defer/retry the submit).

### Mutants (both requested; both kill)
- **Mutant 1 — remove the sqlite-exists gate** (`sqlite_present = … or True`): flips BOTH `test_no_sqlite_pane_submitted_still_raises_oldworld` AND the original `test_codex_submit_verify_f435_r7.py::test_pane_submitted_turn_no_rollout_raises` from raise→confirm. Proves the scoping is load-bearing and is exactly what preserves the r7 invariant. Restored → green.
- **Mutant 2 — disable the SQLite confirm** (`_sqlite_has_user_event` returns False): kills `test_task_in_sqlite_confirms` and `test_sqlite_helper_direct`. Restored → green.

---

## 4. Verification (uv run pytest, in-worktree)

| Suite | Result |
|---|---|
| `test_codex_submit_verify_f643b.py` (new) | **8 passed** |
| `test_codex_submit_verify_f643.py` (F643) | **5 passed** |
| F435 r0/r4/r5/r6/r7 + f598 + send-seam | passed, r7 B1 invariant PRESERVED |
| `test_codex_provider_unit.py` | passed |
| Aggregate run (all above) | **533 passed, 3 skipped, 6 xfailed** |
| `py_compile` | OK |
| Mutant 1 (remove sqlite gate) | old-world test + r7 flip to fail → restored |
| Mutant 2 (disable sqlite confirm) | sqlite tests fail → restored |

No regression. `ruff` is not present in the worktree venv; added code follows surrounding style and `py_compile` is clean.

---

## 5. Residual risk & the live re-probe

- (B) relaxes the anti-false-confirm guard ONLY in the 0.151.0 (SQLite-present) world: a turn that appears in pane history but was actually rejected/lost could be confirmed-by-pane there. This is the accepted tradeoff (fail toward keeping the terminal alive), bounded to the proven-moved world and logged with three facts for auditing.
- The definitive check is the live re-probe (a fresh codex worker either initializes or doesn't). Per instruction, I did NOT spawn codex; you run the probe after gate+merge. Expectation: the resumed worker's first real task now confirms via the SQLite path (turn on `thread_id == provider_session_id`, `created_at_ms >= baseline`); if codex ever fails to commit to SQLite yet submits, (B) keeps the terminal alive with the three-fact WARNING.

## Files changed
- `src/cli_agent_orchestrator/providers/codex.py` (+229 / −7)
- `test/providers/test_codex_submit_verify_f643b.py` (new, 8 tests)
