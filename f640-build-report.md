# F640 (#495) — Build Report

**Lane:** worktree `/data/cao-scratch/worktrees/cli-agent-orchestrator/5f0dcd8e`, branch `cao/5f0dcd8e`, off fork main `715efe00`
**Tip:** `4c5c93de`
**Issue:** #495 — *every codex-provider terminal since the F635 #490 merge dies at init with `deferred_init_internal` / `DeliveryDeferredError('Composer state is unreadable for terminal <id>')`.*

---

## 0. Verdict on the lead

The lead was **directionally wrong** and I disproved it with a same-code A/B, then found the real cause in the SAME commit it named.

- **Lead:** *F635 changed `_effect_barrier`'s capture domain (settle digest seeded from the pyte composite vs `capture_viewport`); the composer-state reader now reads a domain that is empty/unreadable before the codex TUI first renders, so init delivery defers until the 180 s deadline.*
- **What's right:** F635 (`cf7369e7`) **is** the regressing commit, and `_effect_barrier`'s capture domain **is** the site.
- **What's wrong:** F635 did **not** break a *composer-state reader*, and it did **not** cause a *withhold*. `draft_guard.py` (the composer reader) is **byte-identical base→tip**. The `Composer state is unreadable` message is a **downstream symptom**, not the defect. And F635's effect on the resume-chooser is to make it **fire** (a fix), not withhold.
- **F611 (`1a82b5cd`), the named second suspect:** ruled out. Its `_apply_detection` wiring is wrapped in `try/except`, runs *after* the status publish, and never feeds `fuse_status`; its `terminal_service`/`codex.py` deltas are additive (`condition=None` field, `condition_provider_key`). It cannot kill init.

---

## 1. Defect

### Symptom
Two/three consecutive `assign(codex_empirical_reviewer)` dispatches died at init:
```
code=deferred_init_internal deadline_s=180.0 provider=codex
reason=DeliveryDeferredError('Composer state is unreadable for terminal <id>')
```

### Where the message comes from (symptom site, unchanged code)
`terminal_service` deferred-init delivery calls `send_input` → for codex (no
`composer_stash_keys`; `supports_draft_preservation=True`) →
`draft_guard.preserve_draft_before_send` (`draft_guard.py:303`). That reads the
composer via `_read_provider_draft` → `codex.read_composer_draft`. Codex returns
`None` when the pane has **no `›` composer prompt** in the composer region
(`codex.py:2818`). `preserve_draft_before_send` maps a `None` draft to
`raise DeliveryDeferredError("Composer state is unreadable ...")`. Three
`_DEFERRED_DELIVERY_MAX_RETRIES` all hit the same no-`›` pane → the 180 s deadline
elapses → `_claim_and_settle_deferred_failure(..., "deferred_init_internal")`
tears the worker down.

So the terminal dies whenever, for the whole init window, **codex has no `›`
composer** (a dialog, a mid-render/palimpsest frame, or a wrong widget) at every
`send_input` attempt.

### Root cause — F635 over-broadened the barrier FIRE-gate
F635 (#490) correctly fixed a matched-but-no-fire deadlock: the barrier's
settle-digest is seeded from the pyte-**composite** (`status_monitor.get_rendered_screen`),
so `_effect_barrier` must compare against the composite too. F635 introduced
`_barrier_composite_region` and set:
```python
match_region = composite_match_region  # when a composite is available
...
if not rule.matches(match_region):   # <-- FIRE-gate now on the composite
    return False
```
That change did two things at once: it moved (a) the **settle-digest domain**
(correct) **and** (b) the **`rule.matches` FIRE-gate** onto the composite (the
defect). The composite retains text the **live tmux viewport does not**:

| | composite (`get_rendered_screen`) | live viewport (`capture_viewport`) |
|--|--|--|
| line wider than pane | full logical line retained | **truncated** at pane width |
| pyte size ≠ pane size | **stale palimpsest rows** retained (see `status_monitor._resolve_screen_size`) | current cells only |

Consequently the barrier now **fires a rule's answer keys on a match that exists
only in the composite** and is not corroborated by the live pane. At base the
barrier matched the viewport, so such an uncorroborated match was **absent** and
the send was withheld. Post-F635 the keys are sent into codex during init — into
a mid-render/palimpsest frame or the wrong widget — after which codex sits with
**no `›` composer**, and the deferred-init send hits the `Composer state is
unreadable` path above until the deadline.

### Empirical proof of the mechanism (same-code A/B)
A faithful production model — `get_rendered_screen` returns the composite,
`capture_viewport` returns a width-truncated pane, seed lines = composite (as
`status_monitor._detect_screen_with_trust` and
`terminal_service._wait_for_auto_responder_dialog_clear` both do) — driving the
real `on_screen`→`_fire`→`_effect_barrier` path, with the actionable option
label pushed **past** the pane width:

```
scenario: WAITING chooser, option label past 80-col pane width
  base 30a9a0cc : sent keys = []                     (barrier matched viewport → withhold)
  tip  715efe00 : sent keys = ['Down','Enter', ...]  (barrier matched composite → FIRE)
```

And the genuine #490 chooser is *not* affected — its option **labels** are short
and survive truncation (only the `(path)` tails differ):

```
resume-chooser-61e1b848 fixture, RULE.matches(...) at pane widths 80/100/120/220:
  matches_composite=True  matches_viewport=True    (all widths)
```

That is the crux of the fix: viewport corroboration blocks the F640 spurious
fire **without** reintroducing the #490 deadlock.

---

## 2. Fix

`_effect_barrier` keeps the **composite** region for the settle-digest comparison
(F635's #490 fix intact) but the **FIRE decision now requires `rule.matches` on
BOTH the composite AND the live viewport region**:

```python
viewport_match_region = (
    self._region_from_capture(fresh, chrome_patterns) if chrome_patterns else region
)
composite_match_region = self._barrier_composite_region(terminal_id, chrome_patterns)
match_region = composite_match_region if composite_match_region is not None else viewport_match_region
status = self._classify_region(terminal_id, provider, region)
if not rule.matches(match_region):
    return False
# F640 #495: corroborate the FIRE on the live viewport (composite retains
# width-truncated tails / stale palimpsest rows the pane no longer shows).
if composite_match_region is not None and not rule.matches(viewport_match_region):
    self._request_detection_retry(terminal_id)
    return False
```

Design points:
- **#490 preserved.** The genuine resume-cwd chooser's short option labels match
  the viewport at every pane width, so it still fires.
- **F640 closed.** A match present only in width-retained/palimpsest composite
  text no longer fires; a detection retry is re-armed so a genuine dialog fires
  as soon as the live viewport corroborates.
- **Fallback unchanged.** When no composite is available (`get_rendered_screen`
  → `None`), `match_region` *is* the viewport region and the new gate is a no-op
  — F635's viewport fallback and the F597 tests that stub `get_rendered_screen`
  are untouched.
- **Settle-digest domain untouched**, so `test_f635_barrier_capture_domain.py`
  stays green.

**File:** `src/cli_agent_orchestrator/services/auto_responder.py`
(one new local `viewport_match_region`, one corroboration branch).

---

## 3. Tests

**New:** `test/services/test_f640_barrier_viewport_corroboration.py` (2 tests,
driving the real integrated fire path; mock only the tmux boundary + both
capture domains):

1. `test_uncorroborated_composite_match_does_not_fire` — **regression**: option
   labels pushed past the 80-col viewport; the rule matches the composite but NOT
   the viewport. The barrier must send **zero** keys. **RED pre-fix** (sent
   `['Down','Enter', ...]`), GREEN post-fix.
2. `test_corroborated_wide_chooser_still_fires` — **control / #490 preservation**:
   the real wide chooser (labels visible in the viewport, only the path tails
   truncate, composite/viewport digests differ) must still fire `['Down','Enter']`.

**Updated:** `test/services/test_f55_auto_responder_hardening.py` — the AST
invariant that every production `rule.matches(...)` site passes a `DialogRegion`
(never raw `.rows`) counted 8 sites; the fix adds one legitimate site (a bare
`viewport_match_region` Name). Bumped 8→9; the per-call assertion (Name or
`.normalized`/`.normalized_light`, never `.rows`) is unchanged and still runs on
all 9.

---

## 4. Mutation ledger (verified)

- **M1 — revert the fix.** Neutralise the corroboration branch
  (`if False and composite_match_region is not None and not rule.matches(viewport_match_region):`).
  → `test_uncorroborated_composite_match_does_not_fire` goes **RED** (`sent
  ['Down','Enter', ...] != []`); the control stays green. Verified locally, then
  restored. (Equivalently: this test is RED at tip `715efe00` before the fix and
  GREEN at `4c5c93de`.)

The control test doubles as the anti-mutant for an over-broad fix: any fix that
withholds the genuine #490 chooser turns `test_corroborated_wide_chooser_still_fires`
RED.

---

## 5. Verification (grok boxes, per box-ops)

All authoritative runs on **box@grok-box-002** (same box; `grok-box-1` frozen —
never touched). Tip on the box: `4c5c93de`.

### pytest (focused; `-o addopts=""`, 11 files across the touched paths)
`test_auto_responder.py`, `test_f635_barrier_capture_domain.py`,
`test_f640_barrier_viewport_corroboration.py`, `test_f55_auto_responder_hardening.py`,
`test_f516_d2.py`, `test_f582_d21_dialog_clear_lifetime.py`,
`test_f597_pt2_settle_rearm.py`, `test_auto_responder_f516_d5.py`,
`test_auto_responder_f516_d6.py`, `test_draft_guard.py`, `test_claude_stash_guard.py`:

```
4c5c93de
... 1 failed, 140 passed in 8.05s
```

The single failure — `test_f516_d2.py::test_d2_fast_path_waiting_fires_on_first_eval`
— is **PRE-EXISTING and unrelated** (the same failure the F635 report flagged). It
uses the `resume-chooser-61e1b848` DialogReplay fixture but does **not** mock
`status_monitor.get_rendered_screen`, so the F597 `_settle_before_first_send` gate
(which predates F635) cannot settle → zero keys. Proven to fail **identically at
base `30a9a0cc`** locally (a dedicated base worktree; the box's local checkout
guard prevented the base checkout on the box — see ledger). It does not exercise
the barrier fire-gate this fix touches; left untouched (scope discipline).

> Full-suite note: a `-m "not live and not e2e"` run was started on the box and
> the box's 3600 s watchdog was armed, but my client-side tool timeout (120 s)
> closed the ssh channel mid-run, so I did not capture a full-suite tail. The
> change is confined to `_effect_barrier`; the focused run above covers every
> test file that exercises the barrier/settle/draft-guard paths. If the gate
> wants a full-suite tail, I can re-run it backgrounded with a longer client
> timeout.

### mypy --strict (touched file)
```
box@grok-box-002, 4c5c93de:  Found 9 errors in 1 file
```
Identical count to base per the F635 report (base=9, head=9); **zero new errors**
(all 9 are pre-existing `no-any-return`/`type-arg`/`arg-type` at unrelated lines).

---

## 6. Box-actions ledger

**Box:** `box@grok-box-002` only (`grok-box-1` FROZEN — never touched).

| # | Kind | Command / detail |
|---|------|------------------|
| 1 | box-run.sh | label `f640-suite`: `git fetch origin cao/5f0dcd8e && git checkout -B cao/5f0dcd8e origin/cao/5f0dcd8e` → HEAD `4c5c93de`; started `pytest -m "not live and not e2e"`. **Client tool-timeout at 120 s closed the ssh channel** before the run finished (box watchdog was 3600 s); no tail captured. No lasting box mutation. |
| 2 | box-run.sh | label `f640-focus` (`CAO_BOXES=box@grok-box-002`): fetch + `checkout -B cao/5f0dcd8e origin/cao/5f0dcd8e` → `4c5c93de`; `pytest <11 files>` → **140 passed, 1 pre-existing failed**. Tee'd to `/tmp/f640-focus-run.txt`. |
| 3 | box-run.sh | label `f640-mypy` (`CAO_BOXES=box@grok-box-002`): `mypy --strict auto_responder.py` at `4c5c93de` → 9 errors. |
| 4 | raw ssh (READ-ONLY) | `git rev-parse HEAD && git status --porcelain && git branch --show-current` → `4c5c93de`, clean, `cao/5f0dcd8e`. |

- **Checkout state left on box@grok-box-002:** `4c5c93de` on branch `cao/5f0dcd8e`, **clean** (empty porcelain).
- **Environment mutations:** none (no apt/pip/uv installs; `uv run` used the box's existing env; no lockfile changes).
- **Temp files on box:** `/tmp/f640-focus-run.txt` (capture-once log). Nothing outside `/tmp`.
- **Deviations (honestly stated):**
  1. A local PreToolUse hook (`fx121`) refuses any `git checkout`/`git reset` to a
     ref other than my branch `cao/5f0dcd8e` (it guards the lane branch). This
     blocked (a) switching the box back to `main` for temp-branch cleanup — so the
     box is left on my pushed branch tip rather than the long-lived branch, and
     (b) running the base-`30a9a0cc` A/B *on the box* — I ran the A/B in a local
     base worktree instead (now removed). The box branch `cao/5f0dcd8e` tracks a
     pushed, auditable ref, so it is not a local-only SHA.
  2. Full `not live and not e2e` suite tail not captured (client timeout, item #1);
     focused suite covers the touched paths.

**Laptop:** worktree build/edit + quick single-file test runs only (permitted by
box-ops); scratch under `/data/cao-scratch/worktrees/` (throwaway diag scripts
already deleted; the base worktree `/data/cao-scratch/worktrees/f640-base`
removed). No suite/perf runs on the laptop while a box was reachable.

---

## 7. Lineage

```
4c5c93de  F640 (#495): barrier fire-gate requires live-viewport corroboration
715efe00  Merge 'cao/102d5fe3' into main (F244 gated)   <- base of this branch
```

Fix + regression test + F55 invariant bump are in `4c5c93de`; this report is added
on top as its own commit.
