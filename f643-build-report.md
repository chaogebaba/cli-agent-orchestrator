# F643 (#498) — build report

**Git-SHA-fork:** e473e74a50ec4212f7372dd9b69a1340f06b7957 (fork main, branch `cao/f868fe8d`, worktree `.cao`/`/data/cao-scratch/worktrees/cli-agent-orchestrator/f868fe8d`)
**Base subject:** `Merge 'cao/5f0dcd8e' into main (F244 gated)`
**Scope:** root-cause + minimal fix + regression test for fresh codex terminals dying at init on e473e74a via a mechanism DISTINCT from F640.

---

## 0. Sanity check — is the running build stale? (NO)

Requested first because a stale server would invalidate everything else.

- Installed: `~/.local/share/uv/tools/cli-agent-orchestrator/lib64/python3.14/site-packages/cli_agent_orchestrator/services/auto_responder.py`
- Worktree (fork main e473e74a): `src/cli_agent_orchestrator/services/auto_responder.py`
- **Both sha256 = `49eeff1c755c4f04aa8240c1f25fbe2d5ed0c3bbcbc463b9166b7cf955c39623`** — byte-identical; `diff` empty (exit 0).

The build is **current** and **contains F640** (`4c5c93de F640 (#495): barrier fire-gate requires live-viewport corroboration`). F640 touched the auto-responder barrier fire-gate — a **different subsystem** from this init-delivery failure. **F643 is genuinely a different bug.** Not stale; proceeding.

---

## 1. Root cause

### The observed chain (journal, terminal `acc543b1`, 2026-08-30 18:40)
```
18:40:47  pane_liveness rule-3a vetoed (seat idle, no TUI spinner, children=0)
18:40:53-56  F435 submit-verify: no stuck chip visible (attempt 1/3,2/3,3/3); re-checking rollout
18:40:59  ERROR ... the rollout JSONL has no matching user-turn record after offset 49753,
          so delivery is structurally unconfirmed
18:40:59  deferred_init_delivery_deferred attempt=1/3
18:41:01  ERROR Composer state is unreadable for terminal acc543b1  (attempt=2/3)
18:41:03  ERROR Composer state is unreadable ... attempt exhausted
          → DeliveryDeferredError (draft_guard.py:322) → exposure_crossed=True
          → deferred_init_internal teardown
```

### Why (the actual defect)

A **"fresh" codex terminal in CAO is not fresh — it is a RESUME of a seed session.**

1. `CodexProvider.seed_resume_identity()` (`providers/codex.py:1502`) runs
   `codex exec ... "Reply with exactly the text SEED_OK and nothing else."`, which
   mints a native Codex rollout containing the full agent-profile preamble +
   `base_instructions` + the SEED_OK exchange. That rollout is **~49 KB** (matches
   the observed `offset 49753`).
2. The seat is then launched via `build_resume_command(seed_uuid)` → `codex --resume <seed_uuid>`.
   At spawn, `provider_session_id` is pinned to the **seed UUID**
   (`terminal_service.py:2944`, `provider_session_id=resume_uuid or allocated_uuid`),
   set **before** any live capture.
3. F435 submit-verify resolves the rollout by that pinned uuid:
   `capture_submission_baseline()` → `_resolve_rollout_file(seed_uuid)`
   (`providers/codex.py:_resolve_rollout_file`) globs `rollout-*{seed_uuid}*.jsonl`
   and returns the **stale seed file** at offset ≈ 49 753.
4. **Modern Codex resume can FORK the transcript into a brand-NEW rollout file with a
   NEW uuid** (copies history in), rather than appending to the seed file. Confirmed
   from OpenAI Codex's own source via the Firecrawl developer index:
   - openai/codex#3444: *"the previous implementation always converted the transcript
     into `InitialHistory::Forked`, ensuring a new id and file"*; `InitialHistory::Resumed`
     reuses the id/path, `Forked` creates a new one.
   - openai/codex#2736: experimental resume is *"getting the history items and recording
     them in a brand new conversation."*
   - Session files: `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`.
5. So the dispatched **task turn is written to the NEW forked file** (new uuid), while
   `_rollout_has_user_event()` scans the **stale seed file** after offset 49 753 —
   where the turn never appears → "structurally unconfirmed" after all 3 F435 retries.

### The two downstream symptoms are FALLOUT, not separate bugs

- **rule-3a veto (18:40:47)** — `services/pane_liveness.py:529`. This is a **diagnostic
  only**: the code path explicitly "admits published with `pane_delta_vetoed`; nothing
  is withheld, so no hold episode." It fires because the codex seat looks idle (no TUI
  spinner, children=0) while sitting at its prompt. It does **not** swallow or delay the
  submit — it is a *symptom* of the same idle state, not a cause. (Sub-hypothesis 3: disproved as causal.)
- **"Composer state is unreadable" (draft_guard.py:322)** — the deferred-init retry
  calls `send_input` again → `preserve_draft_before_send` → `_read_provider_draft` →
  `CodexProvider.read_composer_draft`, which returns `None` when no `›` composer prompt
  is visible (the paste already submitted / codex is now processing, so the composer box
  isn't in its idle shape). The retry then destroys a **healthy** terminal whose task
  had, in fact, already submitted. (Sub-hypothesis 4: the deferral re-reading a composer
  that "never received text" is a red herring — the composer DID receive and submit the
  text; F435 just looked at the wrong file. Fixing rollout resolution fixes the whole chain,
  so no change to the deferral leg is needed or made.)

**Primary defect:** F435 structural verification is pinned to the seed-uuid rollout and
does not follow a resume-fork into the new rollout file.

---

## 2. The fix (minimal, field-name-independent)

File: `src/cli_agent_orchestrator/providers/codex.py` (+91 / −1)

1. `CodexSubmitBaseline` gains a `baseline_wall: float = 0.0` field — the wall-clock
   instant the baseline was captured.
2. `capture_submission_baseline()` sets `baseline_wall = time.time()` **first thing**
   (before resolving the pinned rollout), so it is ≤ the paste time.
3. New helper `CodexProvider._forked_rollout_match(pinned_path, message, baseline_wall)`:
   when the pinned rollout has no match, it scans sibling `rollout-*.jsonl` files in the
   **same per-terminal codex sessions dir** and confirms delivery in any file that
     - has `mtime >= baseline_wall` (written for THIS dispatch — excludes the stale seed), **and**
     - contains the **exact content-matched** user turn (scanned from byte 0).
   Fail-safe: returns `False` on unset `baseline_wall` (0.0), unresolvable dir, or no match.
4. `_rollout_confirms()` inside `verify_submission_after_send()` now falls back to
   `_forked_rollout_match(...)` after the pinned-file check misses.

**Why this shape:** it does **not** depend on the exact `session_meta` lineage field name
(codex's `source`/`forked_from_id`/`originator` serialization varies by version, and I did
not have a real forked-rollout sample — containment forbade scanning `~/.codex`). Instead it
relies on two robust, version-independent invariants: (a) the forked file is *newer than the
dispatch*, and (b) the *dispatched message content* (the supervisor's callback preamble makes
initial-task text effectively unique). The per-terminal codex-home scoping plus content match
prevents confirming an unrelated turn.

---

## 3. Regression test

File: `test/providers/test_codex_submit_verify_f643.py` (5 tests, new).

Reproduces the F643 trigger at the `verify_submission_after_send` seam:
- `test_task_delivered_to_forked_file_confirms` — seed rollout pinned & stale (~49 KB),
  task turn written to a NEW forked file with mtime after baseline → **must confirm, no Enter**.
  (This is the incident.)
- `test_pinned_seed_only_still_raises` — control: no forked file → genuine failure still
  raises (fallback must not mask a real never-delivered dispatch).
- `test_preexisting_sibling_before_baseline_does_not_confirm` — Guard 1: a sibling with the
  same text but mtime *before* baseline must NOT confirm.
- `test_unset_baseline_wall_disables_fallback` — Guard 2: `baseline_wall==0.0` → fallback off.
- `test_direct_forked_match_helper` — unit-level positive + negative (empty msg / unset wall /
  wrong content).

### Mutant / negative control (proves the test kills THIS bug)
Neutralizing the fallback (`_rollout_confirms` returns `False` instead of calling
`_forked_rollout_match`) makes `test_task_delivered_to_forked_file_confirms` FAIL, reproducing
the **exact** incident log lines:
```
F435 submit-verify: no stuck chip visible (attempt 1/3, 2/3, 3/3); re-checking rollout
CodexSubmitStuckError: ... no matching user-turn record after offset 49163, so delivery is structurally unconfirmed
```
Restoring the fix → 5/5 pass. (Distinct from F640: F640's regression test asserts barrier
fire-gate live-viewport corroboration in `auto_responder.py`; this asserts codex rollout
fork-follow — different file, different subsystem, different signal.)

---

## 4. Verification (all in-worktree, `uv run pytest`)

| Suite | Result |
|---|---|
| `test_codex_submit_verify_f643.py` (new) | **5 passed** |
| F435 r0/r4/r5/r6/r7 + f598 dialog-rearm + send-seam | **93 passed, 6 xfailed** (unchanged) |
| `test_codex_provider_unit.py` + `test_auto_responder.py` | **495 passed, 3 skipped** |
| `py_compile` codex.py + test | OK |
| Mutant (fallback disabled) | f643 fails as designed → restored → pass |

No regression. `ruff` is not present in the worktree venv, so formatting was not auto-run;
the added code follows the surrounding style and `py_compile` is clean.

---

## 5. What I could NOT verify (needs your call — live repro)

I did **static analysis + the journal + fixtures only**; I spawned nothing (no terminals,
no codex processes) per your instruction, and did not scan `~/.codex` per containment.

- **Unconfirmed:** the *exact* fork-vs-reuse behaviour of the codex version installed on
  this laptop, and the precise `session_meta` field a forked rollout writes for its parent
  lineage. The fix is deliberately built to not depend on that field name, but a **controlled
  live repro you run** would let us (a) confirm the fork actually produces a new rollout file
  here, and (b) optionally tighten the fallback from content-match to lineage-match if the
  field is stable. If you want that, spawn one codex worker on a worktree seat and capture
  `~/.codex/sessions/**/rollout-*.jsonl` before/after the first task paste; I'll fold the
  result in.

## Files changed
- `src/cli_agent_orchestrator/providers/codex.py` (+91 / −1)
- `test/providers/test_codex_submit_verify_f643.py` (new, 5 tests)
