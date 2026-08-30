# F635 (#490) — Build Report

**Lane:** `cao/eeb6fbe7` (worktree `.cao/worktrees/eeb6fbe7`) off fork main `30a9a0cc`
**Tip:** `166cfa9f`
**Issue:** #490 — *auto-responder: rule fires (outcome=matched) but keys never reach the codex resume-cwd dialog* (family of #386, but a DISTINCT failure mode).

---

## 1. Defect

### Symptom (from #490)
For a `codex resume <uuid>` "Choose working directory to resume this session"
card, the auto-answers decisions log for the stuck worker showed
`outcome=matched reason=firing rule=codex-resume-workdir-card` **repeatedly
(~2/s)**, yet the dialog never advanced and the terminal stayed
`waiting_user_answer`. Manual `tmux send-keys 2` + `Enter` dismissed it instantly.

This is **distinct from #386/F530**: there the rule reports *no-match*; here the
rule reports *firing* on every eval and still nothing reaches the pane.

### Root cause — a capture-domain mismatch in the fire barrier
`AutoResponder._fire` → `_effect_barrier` gates every send. Its settle check was:

```python
settle_ok = (not pending_region.settle_digest) or (
    _digest_normalized(match_region.normalized) == pending_region.settle_digest
)
```

- `pending_region.settle_digest` is **seeded from the pyte-COMPOSITE screen**
  (`status_monitor.get_rendered_screen`, via `_on_screen`'s `lines` and
  `_settle_capture`).
- `match_region` in the barrier was derived from `_capture_for_analysis` →
  `_capture_fresh` → **`capture_viewport`** (the RAW tmux viewport).

Those are **two different capture domains**:

| Domain | Source | Long line behaviour |
|--------|--------|---------------------|
| pyte composite | `get_rendered_screen` | retains the **full** logical line |
| tmux viewport  | `capture_viewport` (`capture-pane`) | **truncates** at the pane width |

The resume-cwd card's options carry long absolute worktree paths
(`/home/.../.cao/worktrees/59c6884c`). The composite keeps the whole path; the
viewport truncates it at the terminal width. `canonicalize`/`normalize_screen`
folds punctuation and collapses whitespace but **cannot recover the truncated
tail**, so the two canonical strings — and thus `_digest_normalized` — differ
even though it is the **same static dialog** and `rule.matches` is `True` on
both.

Consequence: `settle_ok` **never agreed** → the barrier returned `False` on
every eval → `_send_answer` was never reached → **no keys ever hit the pane**.
Because the per-rule cooldown (`state.cooldown_until`) is set **only after a
successful `_fire`**, the responder re-entered the fire path every detection
tick, re-logging `matched/firing` ~2/s forever. That is the exact #490 signature.

Why the trust-dir card (`codex-trust-dir`, `answer:["Enter"]`) never hit this:
its text is short and width-independent, so its composite and viewport digests
agree.

### Empirical proof of the mechanism
`dialog_region` over the composite vs a width-truncated viewport of the same
card:

```
composite digest: 76c8ffa70db2b1d5
viewport  digest: 7de17fda372148cf
equal: False
matches composite: True   matches viewport: True
```

Driving the real integrated fire path (mock only the outermost boundaries:
DB metadata, `get_rendered_screen`, backend `send_special_key`/`capture_viewport`)
reproduced the deadlock exactly:

```
eval 0: decisions=[('matched','firing'),('matched','settled')]  sends=[]
eval 1: decisions=[('matched','firing')]                         sends=[]
eval 2: decisions=[('matched','firing')]                         sends=[]
...  TOTAL sends: []
```

The codex classifier returns `WAITING_USER_ANSWER` for this card
(`RESUME_CWD_CHOOSER_PATTERN` / `DIALOG_ACTION_FOOTER_PATTERN`), so the D2
fast-path is NOT the problem, and busy-veto is NOT involved — the barrier
withhold is the sole cause. (Note: the codex resume chooser is an arrow-key +
Enter list; the numeric labels are display-only. That is orthogonal to this
fork-side defect — the fork simply never delivered the configured keys at all.)

---

## 2. Fix

`_effect_barrier` now derives its **match/settle region from the composite
screen** — the SAME domain the settle-digest is seeded in — via a dedicated
`_barrier_composite_region(terminal_id, chrome_patterns)` seam that reads
`status_monitor.get_rendered_screen` **directly**. The raw viewport capture
(`fresh`/`region`) is still used for the provider `status` classify (unchanged).

```python
composite_match_region = self._barrier_composite_region(terminal_id, chrome_patterns)
if composite_match_region is not None:
    match_region = composite_match_region
else:
    match_region = (
        self._region_from_capture(fresh, chrome_patterns) if chrome_patterns else region
    )
```

Design points:
- **Same-domain comparison.** Composite-seed vs composite-barrier: a genuinely
  changed/torn frame still differs (settle correctly withholds), but a stable
  dialog that merely truncates differently in the viewport no longer produces a
  spurious mismatch.
- **Dedicated seam, not `_current_normalized`.** A first cut routed the composite
  capture through `_current_normalized_filtered`, but several existing tests stub
  `_current_normalized` as the retry-loop's "dialog cleared" signal, so that
  collision withheld the send in those tests. `_barrier_composite_region` reads
  `get_rendered_screen` directly, staying in the seed's domain without touching
  the retry-loop stubs.
- **Safe fallback.** When no composite is available (`get_rendered_screen` →
  `None`), the barrier keeps its prior viewport-region behaviour, so no path
  regresses.

**File:** `src/cli_agent_orchestrator/services/auto_responder.py`
(+41 / −17 across the two commits; net: one new method + a 6-line barrier branch).

---

## 3. Tests

**New:** `test/services/test_f635_barrier_capture_domain.py` (5 tests). Mocks the
tmux boundary (`send_special_key`) and both capture paths (`get_rendered_screen`
= composite, `capture_viewport` = truncated viewport), and drives the real fire
path:

1. `test_matched_rule_delivers_keys_despite_capture_domain_divergence` —
   **regression**: composite retains the full path, viewport truncates it; the
   firing rule MUST deliver `["2","Enter"]` to the pane on the correct
   session/window. (Pre-fix: zero sends.)
2. `test_precondition_composite_and_viewport_digests_differ` — guard that the two
   domains really produce different digests while both match the rule (so the
   regression is not vacuously green).
3. `test_mutant_barrier_matches_on_viewport_deadlocks` — mutant that forces the
   barrier back onto the raw viewport (composite unavailable) must deadlock (zero
   keys) = the #490 defect.
4. `test_mutant_dropping_send_goes_red` — mutant neutralising `_send_answer`;
   asserts no key reaches the pane and that the regression's own assertion fails.
5. `test_short_dialog_unaffected_still_fires` — control: a width-independent
   dialog (trust-dir style) keeps firing exactly as before.

---

## 4. Mutant recipes (verified RED)

- **M1 — revert the fix (barrier matches on the truncated viewport).** Replace
  the `_barrier_composite_region` branch in `_effect_barrier` with the old
  `match_region = self._region_from_capture(fresh, chrome_patterns) ...`.
  → `test_matched_rule_delivers_keys_despite_capture_domain_divergence` FAILS
  with `got []` (verified locally before restoring:
  `1 failed, 4 passed`). Also covered in-file by
  `test_mutant_barrier_matches_on_viewport_deadlocks`.
- **M2 — drop the send.** Stub `AutoResponder._send_answer` to a no-op returning
  `True`. → keys never reach `send_special_key`; asserted RED by
  `test_mutant_dropping_send_goes_red`.

---

## 5. Verification (grok boxes, per box-ops)

All authoritative runs on **box@grok-box-002**, same box for base/head parity.
Tip checked out on the box: `166cfa9f`.

### pytest (`-m "not live and not e2e"`)
Files: `test_f635_barrier_capture_domain.py`, `test_f597_pt2_settle_rearm.py`,
`test_f516_d2.py`, `test_auto_responder.py`, `test_f55_auto_responder_hardening.py`,
`test_auto_responder_f516_d5.py`, `test_auto_responder_f516_d6.py`.

```
166cfa9f
... 1 failed, 112 passed in 6.51s
```

The single failure — `test_f516_d2.py::test_d2_fast_path_waiting_fires_on_first_eval`
— is **PRE-EXISTING and unrelated to this change**. Proven to fail **identically
at base `30a9a0cc`** on the same box:

```
30a9a0cc
... 1 failed in 2.04s   (same test, same assertion)
```

It uses the `resume-chooser-61e1b848` DialogReplay fixture and does not exercise
the barrier capture-domain path this fix touches. Left untouched (scope
discipline); flagged here for the gate.

### mypy --strict parity (touched file)
`src/cli_agent_orchestrator/services/auto_responder.py`:

```
HEAD 166cfa9f: Found 9 errors in 1 file
BASE 30a9a0cc: Found 9 errors in 1 file
```

Identical pre-existing errors (`[type-arg]` L612, `[arg-type]` L749, three
`[no-any-return]`), differing only in the line numbers shifted by the added
method. **Zero new mypy errors introduced.**

---

## 6. Box-actions ledger

**Box:** `box@grok-box-002` (only box used; `grok-box-1` FROZEN — never touched).

| # | Kind | Command / detail |
|---|------|------------------|
| 1 | box-run.sh | label `f635-pytest` (first attempt, `--expect-head 30a9a0cc`) → exit 78 HEAD-mismatch (box was at a9ac57de; guard is pre-fetch, so not applicable — re-run without it). No slot work done beyond acquire/release. |
| 2 | box-run.sh | label `f635-pytest`: `git fetch origin cao/eeb6fbe7 && git checkout 9b15198b && pytest <7 files>` — surfaced 5 failures from fix-v1 (informed fix-v2). |
| 3 | box-run.sh | label `f635-pytest` (`CAO_BOXES=box@grok-box-002`): `git fetch && git checkout 166cfa9f && pytest <7 files>` → 112 passed, 1 pre-existing failed. Output tee'd to `/tmp/f635-pytest-run.txt`. |
| 4 | box-run.sh | label `f635-base-parity` (`CAO_BOXES=box@grok-box-002`): `git checkout 30a9a0cc && pytest test_f516_d2.py::test_d2_fast_path...` → 1 failed; then `git checkout 166cfa9f` (restore). |
| 5 | box-run.sh | label `f635-mypy` (`CAO_BOXES=box@grok-box-002`): `mypy --strict auto_responder.py` at 166cfa9f then 30a9a0cc, then `git checkout 166cfa9f` (restore). |

- **Raw ssh:** none (all box interaction via `box-run.sh`).
- **Checkout state left on box@grok-box-002:** `166cfa9f`, clean (each mutating
  run restored the tip as its last step).
- **Environment mutations:** none (no apt/pip/uv installs, no lockfile changes;
  `uv run` used the box's existing env).
- **Temp files on box:** `/tmp/f635-pytest-run.txt` (capture-once log). No other
  artifacts left outside `/tmp`.
- **Deviations:** attempt #1 used `--expect-head` incorrectly (it validates the
  box's PRE-fetch checkout, not the post-checkout target); harmless (exit 78
  before any payload), corrected by dropping the flag. No other deviations.

**Laptop:** worktree build/edit only + quick single-file/unit repro checks
(permitted by box-ops "quick single unit tests may still run locally"); no
suite/perf runs on the laptop. Scratch under `/data/cao-scratch/eeb6fbe7/`
(repro scripts, source backup) — throwaway, outside the committed tree.

---

## 7. Lineage (2 commits off base `30a9a0cc`)

```
166cfa9f  F635 (#490) fix2: barrier composite capture via dedicated seam
9b15198b  F635 (#490): auto-responder — barrier settle-digest capture-domain mismatch
```

(Report committed on top as its own final commit.)
