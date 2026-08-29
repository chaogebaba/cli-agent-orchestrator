# F530 build report — auto-responder resume-cwd deadlock (#386)

**Lane:** `cao/636f77b5` (fork; merge-base `156323d5`). Resume of a builder that
died on a Kiro auth expiry after committing WIP at tip `899ae826`.
**Status:** COMPLETE — root cause proven, fix in place, regression tests with
mutation notes, report written.

## 1. Proven root cause

The reported symptom ("rule textually matches yet the responder reports no-fire;
hot-reload never re-arms; only manual tmux Down+Enter unblocks", 15 occurrences,
all post-F516) is a **classification demotion**, not a rule-coverage or
rule-matching-window defect.

On `main`, `CodexProvider.emit_screen_signals` emitted the resume-cwd chooser's
`waiting` signal behind a guard:

```python
if not progress_rows and RESUME_CWD_CHOOSER_PATTERN.search(row):
    signals.append(ScreenSignal("waiting", "RESUME_CWD_CHOOSER_PATTERN", index))
```

Codex renders a persistent `• Working (…s • esc to interrupt)` footer spinner on
the SAME frame as the still-active chooser. That spinner makes `progress_rows`
truthy, so the `not progress_rows` guard **suppressed the chooser's `waiting`
signal entirely**. The frame then resolved to `PROCESSING` (progress wins). In
`AutoResponder.on_screen`, the whitelist rule DID textually match, but a matched
rule under a PROCESSING classification hits `_busy_veto(...)` → the fire is
vetoed and the responder falls through to the `unknown blocking dialog` push.
Because the classification demotes on every subsequent eval too, appending a
fresh rule and letting the mtime hot-reload fire also never fires — the new rule
is vetoed for the same reason. Hence "hot-reload no re-match."

### Empirical proof (self-contained, no out-of-containment file read)

Simulated main's guard vs. the fix against the chooser + `• Working` spinner
frame (scratch: `/data/cao-scratch/1700084b/f530_classify_before.py`):

```
progress_rows present? True  chooser rows: [0]
MAIN guard `not progress_rows and CHOOSER`: waiting signal emitted? False
F530 (unconditional):                       waiting signal emitted? True
CURRENT tree get_status_from_screen = TerminalStatus.WAITING_USER_ANSWER
```

The `not progress_rows` guard is the reproduced defect: with a spinner present
the waiting signal was never emitted, so the modal classified PROCESSING and the
matched rule was busy-vetoed.

## 2. The fix

**Layer 2 (primary — the proven fix), `providers/codex.py`:** emit the
`RESUME_CWD_CHOOSER_PATTERN` `waiting` signal UNCONDITIONALLY, even when a
progress spinner coexists. The chooser is a hard input-blocking modal; codex
cannot be "working" while blocked on it. The shared classification law resolves
`waiting` before `progress`/`completion`, so this WAITING signal wins, the frame
classifies `WAITING_USER_ANSWER`, and `_busy_veto` no longer vetoes the fire →
the responder sends Down+Enter (option 2, current dir). This directly unblocks
matching rules AND makes hot-reloaded rules fire on the next eval (the rule loop
in `on_screen` re-runs `_store.get_rules`, which reloads on mtime, every eval).

**Layer 1 (defense-in-depth), `services/auto_responder.py` + `providers/base.py`
+ `providers/codex.py`:** `dialog_region()` now optionally drops provider
footer/chrome rows (spinner, turn footer, composer prompt, status bar) BEFORE
slicing the `DIALOG_REGION_LINES` (20) tail, via a new
`BaseProvider.chrome_row_patterns()` hook (codex overrides it). This guarantees a
tall pane whose chrome would push the modal past the 20-row tail still matches.
NOTE: this is hardening, not the reproduced root cause — the chooser (9 rows) +
its chrome (~8 rows) fits inside 20, so chrome displacement was not what caused
occurrences #1–#15. Only whitelist RULE MATCHING uses the chrome-filtered tail;
the classifier, D6 history/banner, and unknown-dialog shape heuristic keep
reading the unfiltered bottom-anchored tail (F55 AST invariant preserved).

**Layer 3 (diagnosability), `services/auto_responder.py` +
`cli/commands/auto_answers.py`:** `Rule.reject_reason()` names WHY a rule does
not match (`disabled` / `question(regex)` / `question(contains)` /
`option[<opt>]`); the `no_rule_matched` decision-log entry now carries a
`_reject_summary` (per-rule failing field + first 80 chars of the matched
window); and a new `cao auto-answers test <provider> <pane.txt>` command replays
a captured pane against the rules and prints the region + per-rule verdicts with
NO side effects. This makes any future no-fire root-causeable from a saved pane
without a live supervisor.

## 3. Regression tests (mutation note per test)

`test/services/test_auto_responder.py` (F530 block) and
`test/cli/commands/test_auto_answers.py`:

- `test_f530_layer2_resume_chooser_classifies_waiting_despite_spinner` — PROVEN
  ROOT CAUSE guard. Mutation: restoring main's `not progress_rows and CHOOSER`
  guard re-suppresses the waiting signal under a spinner → the spinner-variant
  assert demotes to PROCESSING and the test fails.
- `test_f530_resume_chooser_under_chrome_fires_end_to_end` — end-to-end fire at
  the send-keys boundary. Mutation: reverting either layer regresses it
  (restoring the guard busy-vetoes → `sent == []`; removing the chrome filter
  displaces the chooser on a tall pane).
- `test_f530_layer1_chrome_rows_dropped_keep_chooser_in_tail` — Mutation:
  deleting the `_drop_chrome_rows` call (or returning `[]` from
  `chrome_row_patterns`) re-admits footer rows; inverting the F55 scrollback
  guard drops genuine output.
- `test_f530_layer3_reject_reason_names_failing_field` — Mutation: dropping the
  `disabled` short-circuit, swapping regex/contains branches, or skipping the
  options loop each flip a named reason.
- `test_f530_layer3_diagnose_rules_reports_region_and_verdicts` — Mutation:
  matching against the unfiltered region leaks chrome into `match_normalized`;
  dropping `reject_reason` fails the trust-dir verdict.

Each mutation note is inline in the test docstring.

## 4. Verification (targeted `-n0` only, per brief)

```
uv run pytest -n0 test/services/test_auto_responder.py \
  test/services/test_f55_auto_responder_hardening.py \
  test/cli/commands/test_auto_answers.py \
  test/providers/test_codex_dialog_screens.py
=> 110 passed
```

- `black --check` on the 4 fully-owned F530 files (auto_responder.py,
  auto_answers.py, and both test files): clean. `codex.py` carries pre-existing
  black debt (its `main` copy is not black-clean); the F530-touched region was
  hand-kept clean (removed one stray double blank line) — no unrelated
  reformats, per scope discipline.
- `mypy --strict` on the touched source files: the F530 additions introduce
  **zero new errors**. Every error on the current tree maps 1:1 to an identical
  pre-existing error in the `main` copy of the same file (verified by diffing
  mypy output against `git show main:` snapshots). The repo's `--strict`
  baseline for these files is already dirty and not gated here.

## Containment

Worktree-only (`.cao/worktrees/1700084b`). No `~/` or `~/.claude` writes; the
real `~/.aws/.../auto-answers/codex.yaml` was NOT read (out of containment) — the
root-cause proof uses the two rules transcribed in the test file. No laptop full
suite. Scratch under `/data/cao-scratch/1700084b/`.
