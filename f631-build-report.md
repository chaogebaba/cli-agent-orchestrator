# F631 slice 1 — build report (**r3**)

**Issue:** #486 (F631). **Authority:** `orchestrator/blueprints/f631-terminal-identity-registry.md`
(DRAFT r6), read in full before building. **Branch:** `f631-slice1-identity`.
**Base:** fork `main` @ `5d39ec7f`. **Tip:** this report is the last commit on the
branch, so the reviewed tip IS the merge ref's tip.

**Lineage.** r1 **GATE-NO**, 2 blockers (report
`/data/cao-scratch/f631-gate-report-r1.md`, sha
`a468495400f1a55bccd8c9085679005c0a77b8bf1b98bcd93a30f1cd6b35b767`, reviewed tip
`15b874f9`) — the two production catch-alls; fixed in §0.1. r2 **GATE-NO**, 2
blockers (report `/data/cao-scratch/f631-gate-report-r2.md`, sha
`679dc7e757d048b9bb98ca13328e2dc17ac8a4ff0d80142d5a6a105b696469f0`, reviewed tip
`a7a59352`) — the r2 arms for those fixes were **false semantic kills**; fixed in
§0.3. **r3 changes only the two arms. The production diff is byte-identical to the
one r2 confirmed correct.**

Confirmed by the gate and unchanged here: the production catch-all removal
(independently checked for a benign reachable exception — none found), the M10
atomicity arm, M1–M11 as 11/11 valid kills, the D1/D2/D10 schema, the slice
boundary, the §7 deferral audit, the migrator-swallow disclosure (now empirically
settled — see §0.4), and both team-lead rulings (`readopt_service.py:242` stays
out; the three unwritten columns are acceptable under D10).

Commits:

| sha | subject |
|---|---|
| `29400fb9` | F631 (#486) slice 1: terminal_identity row + create/reap lifecycle |
| `1237f258` | annotate identity helpers for mypy --strict |
| `bcb1c626` | cast the widened delete result at the bool seam (mypy delta 0) |
| `b89c5166` | widen the WPM4-a exact-dict delete assertion for resume_key |
| `3bc008fa` | **r2:** remove both catch-alls; arm the failed identity write |
| `f9202775` | **r2:** collapse the dedented query to black's line (fmt delta 0) |
| `5353523b` | **r3:** state-only blocker arms with pre-flush fault injection |
| `9534a028` | **r3:** hoist the identity-state tuple for black (fmt delta 0) |

---

## 0. The two GATE-NO rounds

### 0.1 Decision on the `except` clauses: **removed, not narrowed**

Both blockers are the same defect on the two lifecycle helpers, and both are real.
The reviewer's probes reproduce exactly:

```text
CREATE_TERMINAL_ROWS=1   CREATE_IDENTITY_ROWS=0
REAP_RESULT={'terminal_deleted': True, 'intent_deleted': False, 'resume_key': None}
REAP_TERMINAL_ROWS=0   REAP_IDENTITY_LIFECYCLE=live   REAP_IDENTITY_REAPED_AT=None
```

I wrote those handlers on the reasoning "identity registration must never be able to
fail a terminal create." That reasoning is wrong, and the record says why: §3's promise
is not call ordering, it is that **a laptop-created terminal is never unregistered**. A
swallowed write failure does not protect the create — it makes the one forbidden state
an intentional success path, and does it silently.

I considered narrowing rather than removing and rejected it, deliberately: **there is no
exception these bodies can raise that is benign.** Every one of them (`IntegrityError`,
`OperationalError`, a mapper failure, anything else) means the identity row was not
written, which is precisely the state that must abort the operation. Any class I chose
to tolerate would reinstate the blocker for that class. And the one case that genuinely
*must* be tolerated — a lane with no identity row, i.e. the pre-registry case D10
supports — was never an exception in the first place: it is control flow
(`one_or_none()` → early return on the reap side, the insert-if-absent early return on
the create side). Removing the handlers therefore preserves it untouched, which
`test_a_pre_registry_lane_is_control_flow_not_a_tolerated_exception` asserts as an
explicit negative control on the removal itself.

So both `try/except` blocks are gone. `create_terminal` now propagates a registration
failure and its transaction never commits; `delete_terminal_and_warm_intent` propagates
a retirement failure and `SessionLocal.begin()` rolls back the hard delete with it.

**One consequence I am naming rather than leaving for the gate to find:** the migrator
`_migrate_f631_terminal_identity()` swallows its own failure (matching every migrator in
`init_db`). If the migration ever failed, `create_terminal` would now fail loudly on
every call instead of running unregistered. That is the correct direction under §3 — an
unregistered fleet is the failure this slice exists to prevent — but it is a behaviour
change worth a reviewer's eye.

### 0.2 The blocker arms are siblings of M10, not replacements

M10 is kept **exactly as both gates confirmed it**. It forces `create_terminal` to
raise between the identity insert and `db.commit()` and asserts both rows are absent —
the *outer abort* path. A swallowed exception never reaches that path: the transaction
is never told anything went wrong and commits normally. M10 cannot see it by
construction, which is why the fix needed its own arms rather than a stronger M10.

### 0.3 r3 — why the r2 arms were hollow, and what changed

The r2 gate is right and the finding is a good one. My r2 injection was a mapper
`before_insert` / `before_update` listener. Those fire **during `Session.flush()`**,
which poisons the SQLAlchemy transaction — so with the catch-all restored the swallow
happened, but the outer `db.commit()` then failed **on its own**, the durable state
stayed correct, and the arm went red only because the propagating exception class
changed (`PendingRollbackError` / `InvalidRequestError` instead of my `RuntimeError`).
The reviewer proved it by broadening the matcher and leaving every state assertion
intact: `N1_STATE_ONLY_RC=0`, `N2_STATE_ONLY_RC=0`. The forbidden state was never
reachable under that injection, so no state assertion could have caught it. A red for a
reason connected to the change is still not a red for the reason claimed.

Two things changed in r3, and **only the arms changed**:

**(a) The injection is now PRE-FLUSH and pure Python**, so the caller's transaction
stays healthy and a swallowed failure really does commit the forbidden state:

| Side | r2 injection (poisons the transaction) | r3 injection (leaves it usable) |
|---|---|---|
| create | mapper `before_insert` listener raises in `flush()` | the model **constructor** raises while the row is being built — the r1 probe's own fault |
| reap | mapper `before_update` listener raises in `flush()` | an attribute **`set` event on `lifecycle`** raises during `row.lifecycle = "reaped"` |

**(b) Both arms are STATE-ONLY BY CONSTRUCTION.** Neither makes any assertion about
which exception propagates, or whether one propagates at all: the call is wrapped in a
bare `except Exception: pass` and the verdict is read entirely off the database
afterwards. **There is no exception matcher left to broaden** — the reviewer's
falsification is not merely survivable, it is inapplicable.

The propagation property is not lost; it is kept in a separate companion test that is
explicitly documented as **not** a blocker arm, because under the swallow nothing
propagates and it fails for a non-state reason. §5.0 shows both failing side by side
under the same mutation, which is the evidence that the two properties are cleanly
separated.

**One assertion-design note.** On the reap side the identity row reads `live` /
`reaped_at is None` under BOTH the clean and the swallowed path, so asserting only on
the identity row would prove nothing. The discriminator is the **terminals** row:
hard-deleted when the failure is swallowed, alive when it propagates. The arm asserts
that first.

### 0.4 The migrator swallow — answered, not just flagged

My r2 report flagged that `_migrate_f631_terminal_identity()` still swallows its own
failure, so a failed migration would now make `create_terminal` fail loudly instead of
running unregistered. The r2 gate settled it empirically rather than by argument: with
the migration forced to swallow a real SQLite connect failure and the table absent,
`create_terminal` raised `OperationalError` and the terminal count stayed 0. So the
removal converts a missing-table migration failure into fail-closed availability loss,
never into an unregistered terminal — the correct direction under §3. It remains a
worthwhile later arm, and the misleading "migration succeeded" startup log is an
availability/diagnostic follow-up, not a consistency regression.

Focused arms: **24 passed** (r1: 20; r2: 23; r3 adds the companion test).


---

## 1. The slice boundary I settled on

Slice 1 is **the identity row + the create/reap lifecycle**, reconstructed from the
record's D-rows and ACs as:

**In scope — built.**

| Record item | What landed |
|---|---|
| **D1** — a NEW `terminal_identity` table; `provider_sessions` left alone | `TerminalIdentityModel` (`clients/database.py`), the full §2 column set, PK `terminal_id`, indexed `provider_session_id`. `provider_sessions` and `register_provider_session` are untouched. |
| **D2** — `lifecycle` is a two-value column (`live` → `reaped`) | `CheckConstraint("lifecycle IN ('live','reaped')", name="ck_terminal_identity_lifecycle")` on the new table. |
| **D10** — additive migration, `CREATE TABLE`, no rebuild, no backfill | `_migrate_f631_terminal_identity()`, appended LAST in `init_db()`, following the `_migrate_f642_delivery_ledger()` precedent (raw `sqlite3`, `CREATE TABLE IF NOT EXISTS`, idempotent, failure logged at debug and never propagated). |
| **§3 bullet 1** — terminal create inserts the identity row **in the same transaction** as the terminals row | `_register_terminal_identity(db, …)` called from BOTH terminal-create writers (`create_terminal`, `create_terminal_with_warm_intent`) with the caller's open session. |
| **§3 bullet 4** — `delete_terminal` sets `lifecycle="reaped"`, stamps `reaped_at`, **returns the resume key**, and never deletes the identity row | `_mark_terminal_identity_reaped(db, …)` inside `delete_terminal_and_warm_intent`'s transaction, immediately before the unchanged hard delete of the terminals row; `resume_key` added to that result, propagated through `_delete_terminal_under_lease` onto each reaped entry of the cascade dict that §1 records as having "no resume key". |
| **§4 fleet projection** — a `reaped` identity row never appears as a lane | No code change is required (identity rows live in their own table, invisible to every terminals-keyed projection); asserted rather than assumed — AC14. |

ACs armed by this slice: **AC1, AC2, AC14, AC15**, plus D2's CHECK and D4's
reap-side return. Everything else in §6 belongs to a later slice.

**In scope, deliberately — the whole column set in one `CREATE TABLE`.** D10 promises
"a single `CREATE TABLE terminal_identity`", so splitting the columns across slices
would turn a later slice's work into an `ALTER`. `retained_persona_home` (D11),
`git_sha` and `dirty_hashes` (D4's staleness snapshot, back-filled by `mark_ready`)
therefore exist as columns but have **no writer in this slice** and stay NULL — a NULL
snapshot means "no snapshot taken", which is exactly what D4 specifies.

**Out of slice 1 — designed in the record, built later.**

- **D3** — provider-typed capture of `provider_session_id`, and the eligibility
  projection (AC6, AC19, and AC7's projection half). Create records what the caller
  already has and **fabricates nothing**; nothing captures later yet.
- **D4** — the two new `resolve_base` branches, `terminal_reaped_unregistered`, the
  by-uuid handle (AC3, AC4, AC5, AC17). Only D4's *reap-side return* is here.
- **D5 / D6 / D7 / D8 / D12** — the entire pin plane (AC8–AC13, AC20–AC26). Not one
  line of `authority_pin_service.py` is touched.
- **D9** — the box-hosted lane's laptop-resident row and the F634 D15 adoption
  handshake (AC16). The laptop create path is built; box adoption is not.
- **D11** — moving the retained-persona claim plane onto the identity row (AC18).
- **§3 bullet 3** — `mark_ready`'s back-fill of the snapshot columns.

## 2. Deferred BY NAME in the record — verified, and absent from my diff

I re-read §7 before building. The record defers these **by name**; none has a D-row,
an AC or a mutant anywhere in it, and none appears in this diff:

1. **#486 AC2 — the server-side low-memory watchdog.** Runtime work that consumes the
   row but does not shape it.
2. **#486 AC3 — `rss_mib` in `fleet`.** §7 assigns it to F634's D9 fleet columns,
   "which already assume `rss_mib` exists". (Separately established this session as
   F634's, not F631's — so out under either reading.)
3. **#486 AC4 — the doctrine `lanes.md` rewrite** (reap-at-callback, resume recipe,
   LANES RESUME column). A documentation fold that can only be written truthfully once
   the mechanism lands.
4. **F641 #496** — `register_frozen_pins`'s hard-coded `version=1`. §7 names it as
   pre-existing, NOT caused by F631, and explicitly out of scope.

§7's three open questions are left open rather than silently answered: no first-boot
**backfill** (a pre-registry terminal gets no identity row, which is the correct typed
answer per D10 — arm: `test_reap_of_a_pre_registry_lane_returns_none_and_still_deletes`),
no pin-chain **retention** rule, and **claude_code's unwired id** stays unwired.

**One adjacent seam I knowingly did NOT wire, flagged for the gate rather than
decided silently:** `services/readopt_service.py:242` inserts a `TerminalModel` row on
the orphan-readopt recovery path. It is not a terminal *create* — it re-adopts a pane
that already exists — and it has no D-row and no AC. A readopted lane therefore gets no
identity row and behaves exactly as D10 specifies a pre-registry lane behaves. If the
gate reads §3's "terminal create" as covering readopt, that is a one-call addition.

## 3. The diff

```
 src/cli_agent_orchestrator/clients/database.py             | 258 ++++++++++-
 src/cli_agent_orchestrator/services/terminal_service.py    |  14 +-
 test/clients/test_f631_terminal_identity.py                | 553 +++++++++++++++++
 test/services/test_f631_reap_resume_key.py                 | 205 +++++++++
 test/services/test_wpm4a_deferred_init_hardening.py        |   3 +
 5 files changed, 1026 insertions(+), 7 deletions(-)  (excl. this report)

r3 touches ONLY `test/clients/test_f631_terminal_identity.py`; the two production
files are byte-identical to the r2 tip the gate confirmed correct.
```

`clients/database.py`

- `TerminalIdentityModel` — the new table (D1/D2).
- `_migrate_f631_terminal_identity()` + its registration, appended LAST in `init_db()` (D10).
- `_register_terminal_identity(db, …)` — insert-if-absent, in the caller's transaction,
  **unguarded** (§0.1). Insert-if-absent rather than upsert because the PK already gives
  one-row-per-terminal and flipping an existing `reaped` row back to `live` would
  resurrect an identity the delete path deliberately retired.
- `_mark_terminal_identity_reaped(db, …)` — flip + stamp + return the key, **unguarded**
  (§0.1); the pre-registry no-row case stays as an `one_or_none()` early return.
- `get_terminal_identity(terminal_id)` — the read accessor the ACs and later slices need.
- Both create writers call the register helper; `delete_terminal_and_warm_intent` calls
  the reap helper and returns `resume_key`; its annotation widens `Dict[str, bool]` →
  `Dict[str, Any]`, with a `cast(bool, …)` at the one `-> bool` seam
  (`database.delete_terminal`) so the mypy delta stays 0.

`services/terminal_service.py`

- `_delete_terminal_under_lease` returns `deletion.get("resume_key")` — `.get`, not an
  index, because the F138 non-durable-force branch fabricates a result that never
  reached the DB writer.
- `_delete_terminal_inner` puts `resume_key` on each reaped cascade entry, and omits the
  key entirely when there is none rather than emitting a null that reads like a handle.

`test/services/test_wpm4a_deferred_init_hardening.py` — one pre-existing test asserted
**exact dict equality** on `delete_terminal_and_warm_intent`'s result and so pinned the
old two-key contract. D4 widens that contract on purpose; the assertion now includes
`"resume_key": None`. This is the only pre-existing test my diff touches.

## 4. Measured numbers (all on grok boxes; no laptop suite/mypy/build runs)

| Check | HEAD (`b89c5166`) | BASE (`5d39ec7f`) | Verdict |
|---|---|---|---|
| `pytest` new arms (`test_f631_terminal_identity.py` + `test_f631_reap_resume_key.py`) | **24 passed** in 2.86s (r1: 20, r2: 23) | n/a (files are new) | green |
| `mypy --strict` on the two touched source files | **148 errors** | **148 errors** | **delta 0** |
| `black --check` on the 2 new test files + `terminal_service.py` | clean | clean | green |
| `black --check src/…/database.py` | would-reformat | would-reformat | **pre-existing**; `black --diff` output is line-for-line identical HEAD vs BASE |
| `isort --check-only` on the 2 new test files | clean | — | green |
| `isort --check-only src/…/database.py` | ERROR | ERROR | **pre-existing** (identical both sides) |
| full `test/clients` + `test/services` | see §4.1 | see §4.1 | see §4.1 |

I did not reformat `database.py`: its `black`/`isort` complaints are present on `main`
and reformatting an 8k-line shared file would bury this slice's diff.

### 4.1 Full-suite A/B (`test/clients` + `test/services`)

Two clean runs per side on **grok-box-002**, each preceded by an explicit
`git fetch` + `git checkout` with the resolved sha echoed (see the correction below):

| Side | sha | run 1 | run 2 |
|---|---|---|---|
| HEAD **r3** | `9534a028` | **9 failed, 7771 passed**, 25 skipped, 3 xfailed | **9 failed, 7771 passed**, 25 skipped, 3 xfailed |
| BASE **r3** | `5d39ec7f` | **9 failed, 7747 passed**, 25 skipped, 3 xfailed | — |
| HEAD r2 | `f9202775` | 9 failed, 7770 passed | 9 failed, 7770 passed |
| HEAD r1 | `b89c5166` | 9 failed, 7767 passed | 9 failed, 7767 passed |

The r3 legs are the cleanest pair measured on this branch: both sides produced the
same nine names with no extra flake on either.

The **9-failure set is byte-identical on both sides** — all pre-existing on `main`:

```
test/clients/test_database.py::TestInboxOperations::test_message_status_storage_is_additive_unconstrained_text
test/services/test_f516_d2.py::test_d2_fast_path_waiting_fires_on_first_eval
test/services/test_f516_fixtures.py::test_chooser_fixtures_render_the_resume_cwd_dialog_in_region
test/services/test_session_brief_contract.py::test_absent_field_keeps_generated_settings_literal_bytes
test/services/test_stage0_flip_machinery.py::test_trace_manifest_is_byte_exact_and_has_36_hits
test/services/test_stage0b_receiver_evidence.py::test_d6_auto_responder_publishes_full_frame_then_reclassifies_region[False]
test/services/test_stage0b_receiver_evidence.py::test_d6_auto_responder_publishes_full_frame_then_reclassifies_region[True]
test/services/test_wp_watchdog_delegation.py::test_legacy_inbox_migration_and_null_park_warm_are_false
test/services/test_wpdt_delivery_truth.py::TestAC7DoctrineArmingStep::test_doctrine_arming_section_exists
```

**Failure delta: 0** — the nine-name set is byte-identical on both sides. **Passed
delta: +24**, exactly this slice's new arms. Removing both
catch-alls broke nothing elsewhere: no fixture in `test/clients` or `test/services`
creates a terminal against a database without the `terminal_identity` table. `mypy
--strict` is 148/148 at the r3 tip, unchanged — r3 touches no production line.

Earlier rounds' BASE legs each produced their own extra failure under a *different*
name (`test_fx191…`, `test_recovery_decision_intake…`, and the r2 gate's own BASE run
hit `test_f55…test_m4_effect_and_real_delete_terminal_are_serialized_by_delivery_lock`).
That is the flake population speaking, not this slice; the r3 pair happened to land
clean on both sides.

**BASE run 2's 10th failure** is `test_fx191_convergent_delivery.py::TestAC13TraceEmitCount::test_escalation_produces_trace`
— on the **base**, with none of my code present. That names the suite's pre-existing
flake population, and it matters because of what I chased first:

**Correction, recorded rather than buried.** Earlier full runs showed HEAD at *10*
failed against BASE's 9, with the extra failure being a *different* test each time
(`test_wpm4a_deferred_init_hardening.py::test_dispatcher_uses_slot_grant_not_delayed_validator_entry`,
then `test_fx191_convergent_delivery.py::TestS1Rung2FloorInvocation::test_rung2_send_keys_called_when_rung1_demotes`).
I did not report that as "flaky" on the strength of the varying name. I ran the wpm4a
suspect isolated (3× single test, 2× whole file) at `b89c5166` — green every time —
then re-ran the full A/B twice per side. HEAD came back 9/9 both runs and BASE produced
its own 10th on run 2. The suspicion is now discharged by measurement, not by assertion.

One earlier flake-characterization run was **invalid and is excluded**: it used
`git checkout` without a preceding `git fetch`, landed on a box that had never seen the
branch tip, and so silently measured an older sha (its `wpm4a` "1 failed" was the
un-widened assertion at `29400fb9`). Every run quoted above echoes its resolved sha.

**One genuine regression was found and fixed, not explained away.**
`test_wpm4a_deferred_init_hardening.py::test_atomic_delete_preserves_keep_bases_intent`
failed deterministically on HEAD (A/B-confirmed: 1 failed on HEAD, 75 passed on BASE) —
it asserts **exact dict equality** on `delete_terminal_and_warm_intent`'s result, which
D4 widens by design. Fixed in `b89c5166` by widening the assertion.

## 5. Mutation ledger

**r2 note.** Removing the two `try/except` wrappers dedented six of the eleven r1
anchors, so the r1 ledger no longer applied verbatim to the reviewed tip. Rather than
carry the r1 numbers forward, I re-anchored and **re-ran all eleven at `f9202775`** —
including M10, whose old anchor named the removed `except` and is now the equivalent
premature-`db.commit()` inside the unguarded helper. All eleven kill again, with the
same arms and controls. N1/N2 below are the two new blocker mutations.


Driver: apply an exact source edit → run the **named arm** (must go RED) → run the
**negative controls** (must stay GREEN) → `git checkout --` the file → re-run the named
arm (must be GREEN again). A mutation counts as KILLED only when all four hold. Every
arm below is a behavioural assertion against real DB rows or real return values — there
is no text-presence assertion anywhere in either test file.

| # | Mutation | Named arm | pre → mutated → controls → post-revert | Verdict |
|---|---|---|---|---|
| M1 | drop the create-time `_register_terminal_identity` call | `test_ac1_create_writes_a_live_identity_row` | 0 → 1 → 0 (3 passed) → 0 | **KILLED** |
| M2 | reap DELETES the identity row instead of retiring it | `test_ac2_identity_row_survives_the_reap` | 0 → 1 → 0 (2 passed) → 0 | **KILLED** |
| M3 | reap returns no resume key | `test_reap_returns_the_resume_key` | 0 → 1 → 0 (2 passed) → 0 | **KILLED** |
| M4 | reap never flips `lifecycle` to `reaped` | `test_ac2_identity_row_survives_the_reap` | 0 → 1 → 0 (3 passed) → 0 | **KILLED** |
| M5 | the migrator also `ALTER`s `provider_sessions` | `test_ac15_migration_creates_the_table_and_rebuilds_nothing` | 0 → 1 → 0 (2 passed) → 0 | **KILLED** |
| M6 | the lifecycle CHECK is dropped from the model | `test_d2_lifecycle_check_rejects_a_third_value` | 0 → 1 → 0 (2 passed) → 0 | **KILLED** |
| M7 | `base_name` becomes a synthetic name, not the terminal id | `test_ac1_identity_row_carries_what_the_resume_consumer_dereferences` | 0 → 1 → 0 (2 passed) → 0 | **KILLED** |
| M8 | the cascade entry drops the resume key | `test_cascade_reports_the_resume_key_on_the_reaped_entry` | 0 → 1 → 0 (2 passed) → 0 | **KILLED** |
| M9 | `_delete_terminal_under_lease` drops the DB writer's key | `test_under_lease_returns_the_db_resume_key` | 0 → 1 → 0 (2 passed) → 0 | **KILLED** |
| M10 | the identity row is committed OUTSIDE the terminals transaction | `test_ac1_identity_row_is_written_in_the_terminals_transaction` | 0 → 1 → 0 (2 passed) → 0 | **KILLED** |
| M11 | create fabricates a `provider_session_id` when the provider mints none | `test_nothing_fabricates_a_provider_session_id` | 0 → 1 → 0 (2 passed) → 0 | **KILLED** |

**11/11 killed at `f9202775`** (box run `f631-r2-mutants-full`; worktree clean after).

### 5.0 Blocker mutations — reintroducing each GATE-NO r1 defect

**These are the arms r2 got wrong, rebuilt.** The driver no longer accepts a return
code as a kill: for each mutation it also requires the mutated run to have failed on
the arm's **named state assertion**, identified by a marker string in the assertion
message. A red without the marker is reported REVIEW, not KILLED — the r2 gate's
falsification, mechanised.

| # | Mutation | Named arm | pre → mutated → controls → post-revert | State marker in the failure | Verdict |
|---|---|---|---|---|---|
| N1 | restore the create-side catch-all (r1 blocker 1) | `test_a_failed_identity_insert_aborts_the_whole_create` | 0 → 1 → 0 (4 passed) → 0 | **yes** | **KILLED** |
| N2 | restore the reap-side catch-all (r1 blocker 2) | `test_a_failed_identity_retirement_aborts_the_whole_reap` | 0 → 1 → 0 (4 passed) → 0 | **yes** | **KILLED** |

The actual failing assertions under mutation — the kill is a durable-state difference,
and N1's is the r1 probe's `1, 0` verbatim:

```text
N1  E  AssertionError: F631-N1-STATE: terminals,identities = (1, 0), want (0, 0)
N2  E  AssertionError: F631-N2-STATE: the terminals row was hard-deleted despite a failed retirement
```

**Self-falsification, run before reporting** (box run `f631-r3-regress2`). Under the
same restored create-side catch-all, the blocker arm and the companion propagation test
fail for demonstrably different reasons:

```text
[BLOCKER ARM] rc=1
    E  AssertionError: F631-N1-STATE: terminals,identities = (1, 0), want (0, 0)
    E  assert (1, 0) == (0, 0)
[COMPANION]   rc=1
    E  Failed: DID NOT RAISE <class 'Exception'>
```

That asymmetry is the point. The blocker arm dies on state; the companion dies on a
non-state property, which is exactly why it is documented as not being one of the two
arms. There is no matcher on the blocker arms to broaden — the r2 falsification cannot
be applied to them.

N1's control set still includes **M10's arm**
(`test_ac1_identity_row_is_written_in_the_terminals_transaction`) and it stays green.
In r2 I described that as "the ledger's own proof" of the swallowed-error diagnosis;
the r2 gate correctly rejected that reading, since N1 was not observing the property at
all. **Now it observes it**, so the control means what I claimed then: M10 stays green
on a state difference it structurally cannot see. N1's other controls: AC1's create
arm, N2's arm, and the pre-registry control. N2's controls: AC2's survival arm,
`test_reap_returns_the_resume_key`, N1's arm, and the pre-registry control.

**Ledger total: 13/13 killed** — 11 replayed valid at the r3 tip, plus these two, now
state-verified.

Negative controls per mutation (each stayed GREEN while the named arm was RED):

- **M1** → AC15's migration arm, AC1's `provider_sessions`-rejects-the-row arm, and the
  service-side `test_under_lease_returns_the_db_resume_key`. The create path can break
  without the migration or the reap path noticing — the controls prove the arms are
  independent, not one assertion under three names.
- **M2** → `test_ac1_create_writes_a_live_identity_row`, `test_reap_returns_the_resume_key`
  (the key is still returned even when the row is wrongly deleted — which is precisely
  why the survival arm has to be separate).
- **M3** → `test_ac2_identity_row_survives_the_reap`, `test_ac1_create_writes_a_live_identity_row`.
- **M4** → `test_ac1_create_writes_a_live_identity_row`, `test_reap_returns_the_resume_key`,
  AC15's migration arm.
- **M5** → AC1's create arm and AC2's survival arm (a rebuilt `provider_sessions` leaves
  the identity plane working, so only the migration arm can catch it).
- **M6** → `test_ac15_migrated_table_matches_the_model` (the migrator's own CHECK is
  untouched by a model-side mutation) and AC1's create arm.
- **M7** → AC1's create arm (a row is still written — only its `base_name` is wrong) and
  AC15's migration arm.
- **M8** → `test_under_lease_returns_the_db_resume_key` and
  `test_cascade_omits_the_key_for_a_lane_that_has_none`.
- **M9** → the DB-layer `test_reap_returns_the_resume_key` and the cascade arm — the two
  links either side of the mutated one.
- **M10** → AC1's create arm and AC2's survival arm (the row is still written; only its
  transaction boundary moved).
- **M11** → `test_ac1_identity_row_carries_what_the_resume_consumer_dereferences` (a lane
  that HAS a uuid still records the real one) and `test_reap_returns_the_resume_key`.

**One correction I owe the ledger.** M4's first run came back **REVIEW**, not KILLED:
the named arm went red as designed, but one of my declared *controls* —
`test_ac14_reaped_lane_is_invisible_to_live_terminal_projections` — went red too. That
was a mis-declared control, not a code defect: AC14's arm also asserts the reaped
lifecycle, so M4 legitimately kills **two** arms. I re-ran M4 with controls that are
genuinely independent of the reap lifecycle (recorded above), and separately confirmed
AC14 as a second killed arm rather than quietly dropping it:

```
[KILLED ] M4 reap never flips lifecycle
    arm=…::test_ac2_identity_row_survives_the_reap
    pre_rc=0 | mutated_rc=1 | controls_rc=0 (3 passed) | post_revert_rc=0
--- AC14 is a second killed arm, not a control ---
FAILED …::test_ac14_reaped_lane_is_invisible_to_live_terminal_projections
```

### 5.1 Arms that are NOT mutation-driven

Two arms carry the record's own reasoning rather than guarding a line of my code, and I
name them as such:

- `test_ac1_mutant_provider_sessions_rejects_the_identity_row` — AC1's mutant made
  executable. It asserts all three constraints that D1 says reject the row
  (`session_uuid` NOT NULL, the `status` CHECK, the `kind` CHECK) fire against a real
  `provider_sessions` insert. It guards the *decision*, not my diff.
- `test_ac15_migrated_table_matches_the_model` — a fresh install gets the table from
  `Base.metadata.create_all`, an existing DB from the migrator; if the two DDLs drift,
  half the fleet runs a different schema. This one DOES have a mutation (M6's control
  role exercises it).

## 6. Box-actions ledger

Everything ran through `scripts/box-run.sh` from the root repo. `grok-box-001` was never
touched (frozen; the wrapper refuses it anyway). No laptop pytest/mypy/black/build runs.

| Label | Box | What |
|---|---|---|
| `f631-s1-pytest` | grok-box-010 | first checkout + fmt gate (surfaced the pre-existing `database.py` black complaint) |
| `f631-s1-fmt` / `f631-s1-fmt2` | grok-box-010 / -002 | `black --diff` and HEAD-vs-BASE `black --check` — established the complaint is pre-existing |
| `f631-s1-pytest2` | grok-box-004 | black-diff A/B line-count identical + **20 passed** |
| `f631-s1-mutants` | grok-box-002 | mutations M1–M11 (10 killed, M4 REVIEW) |
| `f631-s1-m4` | grok-box-009 | M4 re-run with corrected controls + AC14 confirmation |
| `f631-s1-mypy` … `f631-s1-mypy5` | grok-box-009 / -002 | `mypy --strict` A/B; 152 vs 148 → fixed → **148 vs 148** |
| `f631-s1-isort` | grok-box-010 | isort A/B on `database.py` (pre-existing both sides) |
| `f631-s1-regress` / `-regress2` / `-regress3` | grok-box-010 | full `test/clients` + `test/services` A/B (found the wpm4a contract regression) |
| `f631-s1-flake` | (invalid — no fetch; excluded, see §4.1) | killed |
| `f631-s1-flake2` | grok-box-002 | r1 final 2×HEAD + 2×BASE full A/B with sha echo |
| `f631-r2-arms` | grok-box-004 | r2 focused arms (**23 passed**) + fmt gate |
| `f631-r2-mutants` | grok-box-004 | N1/N2 blocker mutations (2/2 killed) + black A/B |
| `f631-r2-mutants-full` | grok-box-010 | re-anchored M1–M11 re-run at the r2 tip (11/11) |
| `f631-r2-regress` | grok-box-010 | r2 full A/B ×2 HEAD + BASE, plus mypy A/B |
| `f631-r3-mutants` | grok-box-004 | r3 N1/N2 with state-marker verification (2/2) |
| `f631-r3-fmt` | grok-box-004 | black diff for the rewritten arms |
| `f631-r3-verify` | grok-box-004 | fmt + focused (**24**) + N1/N2 + M1–M11 replay (11/11) |
| `f631-r3-regress2` | grok-box-004 | self-falsification + full A/B ×2 HEAD + BASE + mypy A/B |
| `f631-s1-triage` / `f631-s1-triage2` | grok-box-010 | isolating the head-only failures |

- Raw ssh: none.
- Temp files on boxes: `/tmp/f631-*.py`, `/tmp/f631-*.txt` (the mutation driver was
  removed by its own run; the rest are transient `/tmp`).
- Box checkouts: every run ends with `git checkout -q <head sha>` and every mutation was
  reverted with `git checkout --` inside the driver.
- Environment mutations: only the per-worktree `.venv` from `uv sync --frozen`. No apt,
  no global installs, no lockfile change.
- Deviations from box-ops rules: none. `grok-box-3` (unpadded) and the padded hosts were
  all selected by `box-run.sh` from `scripts/boxes.tsv`; no hostname was hand-constructed.

## 7. What a reviewer should look at hardest

1. **The blocker arms' fault-injection points** (§0.3). The whole r2 round turned on
   *where* the fault is raised, not what it is: flush-time injection is unfalsifiable
   here, pre-flush injection is the only kind under which the forbidden state is
   reachable. If either injection is ever changed, the arms must be re-falsified — a
   green arm is no evidence that the state it names is still observable.
2. **The migrator still swallows its own failure** — now empirically settled as
   fail-closed (§0.4) rather than argued, but still the one place where a swallowed
   error decides behaviour, and still unarmed. A later slice should arm it.
3. **The `readopt_service` seam** (§2, last paragraph) — a judgement call I made
   explicitly rather than silently, and one the team lead has since ruled on.
4. **`create_terminal_with_warm_intent` writes `provider_session_id=None`** because that
   writer never receives one. If the warm-fork path can supply a uuid at create, D3's
   capture slice is where it belongs, not here.
5. **The widened `delete_terminal_and_warm_intent` contract.** One pre-existing test
   pinned the exact dict; I widened the assertion rather than the reverse. Any other
   caller doing exact-equality on that result would be a latent break — I found none.
