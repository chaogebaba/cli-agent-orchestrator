# F627 build report — hotfix lane (two test-only fixes), 2026-08-30

Base: 718849a4. Worktree branch `cao/42c4a6fe`, fix commit **61ad9f40**
("F244 followup: SEED_OK in wp2s3 stub stdout + redact /home/chao in codex
busy_marker fixtures"). Tests/fixtures only — no src/, doctrine, or blueprint changes.

## What changed per file (commit 61ad9f40)

1. `test/services/test_wp2s3_start_status_bootstrap.py` (lines 604–608)
   - Stubbed `subprocess.run` stdout for `test_codex_seed_and_interactive_share_resolved_model_config`
     now emits `SEED_OK` after the session-id line, matching real codex 0.151.0
     behavior. Fixes `RuntimeError: seed_marker_missing` from the marker gate at
     `src/cli_agent_orchestrator/providers/codex.py:~1563`.
   - Marker gate NOT touched. Assertions on the shared resolved model config
     (`--model default-model`, `service_tier="fast"`, `features.fast_mode=true`,
     `model_reasoning_effort="high"` in both interactive + seed argv; no `env`
     in kwargs) unchanged and still passing.

2. `test/providers/fixtures/busy_marker/codex/busy-1.json:6` — changed line:
   - `"source_path": "/home/chao/.aws/cli-agent-orchestrator/logs/terminal/16073a9e.scrollback",`
   - → `"source_path": "/home/user/.aws/cli-agent-orchestrator/logs/terminal/16073a9e.scrollback",`

3. `test/providers/fixtures/busy_marker/codex/busy-2.json:6` — changed line:
   - `"source_path": "/home/chao/.aws/cli-agent-orchestrator/logs/terminal/c24be4dc.scrollback",`
   - → `"source_path": "/home/user/.aws/cli-agent-orchestrator/logs/terminal/c24be4dc.scrollback",`

Redaction is ONLY the matched bytes: `chao` → `user` (4→4 chars, allowlisted
synthetic name, width-preserving). The `.txt` pane captures and all other fixture
bytes untouched (no length assertion exists on `source_path`; length preserved
regardless). The PII test file itself unchanged.

## diff --stat vs 718849a4

Whole branch (718849a4..HEAD) includes PRE-EXISTING worktree commits (F618
inbox-expiry migration, database.py + test_f618 + f618-box-*.txt +
f618-build-report.md — 6 files, not from this task):

```
 f618-box-base.txt                                  |  10 ++
 f618-box-head.txt                                  |  16 +++
 f618-box-mutant.txt                                |  39 +++++
 f618-build-report.md                               | 145 ++++++++++++++++++
 src/cli_agent_orchestrator/clients/database.py     |  24 ++++
 test/clients/test_f618_inbox_expiry_migration.py   | 159 ++++++++++++++++++
 test/providers/fixtures/busy_marker/codex/busy-1.json | 2 +-
 test/providers/fixtures/busy_marker/codex/busy-2.json | 2 +-
 test/services/test_wp2s3_start_status_bootstrap.py |  4 +++-
 9 files changed, 398 insertions(+), 3 deletions(-)
```

This task's commit 61ad9f40 only:

```
 test/providers/fixtures/busy_marker/codex/busy-1.json | 2 +-
 test/providers/fixtures/busy_marker/codex/busy-2.json | 2 +-
 test/services/test_wp2s3_start_status_bootstrap.py    | 4 +++-
 3 files changed, 5 insertions(+), 3 deletions(-)
```

## Box evidence

- Host: box@grok-box-005 (slot via scripts/box-run.sh; 002 busy f619-gate,
  003 unreachable, 004 busy f476r4 on first attempt).
- Checkout: fork at HEAD=61ad9f4079d2d11762b42560a110f45624aa0aa5, DIRTY=0.
- Targeted run @ 61ad9f40 (f627-verify2):
  `test/services/test_wp2s3_start_status_bootstrap.py::test_codex_seed_and_interactive_share_resolved_model_config`
  + `test/providers/test_codex_busy_marker.py` + `test/test_fixtures_no_personal_pii.py`
  → **13 passed in 2.69s** (earlier first run at same SHA: 13 passed in 5.25s).
- Negative control @ e3198d27 (pre-fix checkout, box-002): exactly the two target
  tests FAILED (`...wp2s3...seed_and_interactive...` + `test_no_real_home_paths_in_fixtures`),
  other 11 passed — 2 failed, 11 passed in 2.50s. Fix proven causally.
- Laptop only: py_compile OK, JSON validity OK on both fixtures; black/isort
  --check on the wp2s3 test file FAILS at HEAD baseline too (pre-existing, not
  introduced by this change — left untouched, out of scope).

## BOX-ACTIONS LEDGER

- box-run.sh f627-checks (box@grok-box-005): fetch origin cao/42c4a6fe +
  checkout 61ad9f40 + pytest of the 3 test files, tee /tmp/f627-checks.txt.
  Wrapper killed by a 30s tool timeout; remote payload survived and completed
  (13 passed). FLAGGED deviation (same incident as F244 ledger): rm -rf
  ~/.cao-slot.lock (orphaned lock of my own completed run) — cleared before re-runs.
- box-run.sh f627-verify (box@grok-box-002): negative-control pytest at box's
  existing checkout e3198d27 (no checkout mutation); 2 failed/11 passed as expected.
- box-run.sh f627-verify2 (box@grok-box-005, CAO_BOXES pinned): pytest at 61ad9f40,
  tee /tmp/f627-verify2.txt, 13 passed.
- Raw ssh READ-ONLY: git rev/log/status probes, pytest-log tail/cat, lock checks.
- Raw ssh MUTATIONS (flagged, per F244 precedent):
  1. rm -rf ~/.cao-slot.lock on 005 — orphaned lock of my own completed
     f627-checks run (wrapper died via tool timeout; payload had completed).
  2. rm -f /tmp/f627-checks.txt, /tmp/f627-verify.txt, /tmp/f627-verify2.txt —
     leave-box-clean. NOTE: f627-checks.txt was removed before its scp copy was
     verified (empty local copy) — evidence reconstructed via the documented
     f627-verify2 re-run at the same SHA; no box state altered by the loss.
- Box left: 005 at 61ad9f40 (detached), clean, slot free, /tmp cleaned.
  002 untouched at e3198d27 (verify run checkout-neutral). Env mutations: none.
- Scratch: /data/cao-scratch/42c4a6fe/ (f627-box.log, f627-verify.log,
  f627-verify2.log, f627-checks.txt[empty]). No suites on the laptop.
