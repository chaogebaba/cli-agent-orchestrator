# F634 (#489) wave-1 slice 1 — build report

**Authority**: `orchestrator/blueprints/f634-box-hosted-workers.md`, FROZEN r14, DESIGN GATE-YES at r13,
sha256 `9a4a3a3b355772acb81c14a5b9a335411f1f1461be48f9ac093264c9350e2820` (re-verified at build start;
matches the brief).
**Slice**: D15 create routes + D16 host-aware laptop shim.
**Repo**: fork only (`cli-agent-orchestrator`). **This slice changed no file in the ROOT repo and committed
nothing there.** Note for the lead: at the end of this build `git -C <root> status --short` was NOT empty —
it showed `.claude/hooks/test_supervisor_inbox_drain.sh` and `scripts/ci-regression-pack.sh` modified,
mtimes 02:07/02:08, i.e. written by a CONCURRENT lane in this session (the root repo was clean when this
lane started). They are not mine, I left them alone, and nothing in this slice's ledger or suites depends
on them.
**Branch**: `f634-slice1-d15-d16` (fork). **Code HEAD**: `e750dbfa6bb20c7cf67119176c86168fe399a1d3` — the
single commit carrying every source and test change. This report is the commit ON TOP of it (gates.md
clause 7: the artifact is in-tree so the reviewed tip IS the merge ref's tip); the report commit touches
**no code**, and the mutation ledger below was re-run on the report tip as well — identical, 8/8 mutants
RED on their named arm and GREEN on revert, tree clean, baseline 36 passed.
**Base**: `5d39ec7f` (fork main at build start; the record's pin `d3375619` is an ancestor of it, and r14
records that the delta touches only `teammate_push_service.py` plus a test, so no F634 cite moves).
**Boxes used**: `box@grok-box-002` and `box@grok-box-004` (load-balanced). `grok-box-001` never touched.
No pytest / mypy / black / isort / uv execution on the laptop.

---

## 1. What was built

### D15 — `terminal_id` becomes a public create-route field

`terminal_id` was already accepted by the DB layer (`clients/database.py` `create_terminal`, id first) and
by the SERVICE layer (`terminal_service.create_terminal(terminal_id: Optional[str] = None)`, with
`epoch_recovery_service.py` passing it in production), but no HTTP route exposed it. All three create
routes now do:

| route | file | note |
|---|---|---|
| `POST /sessions` | `api/main.py` `create_session` | first terminal of a session |
| `POST /sessions/start` | `api/main.py` `start_session_endpoint` | surface uniformity only (D10: supervisor seat is laptop-only) |
| `POST /sessions/{session_name}/terminals` | `api/main.py` `create_terminal_in_session` | **the route `assign` actually posts to**, and the only one that sets `caller_id` |

Both new fields are **query params**, matching the in-tree convention that routing flags stay in the query
string while only message content moves to the JSON body (`CreateTerminalBody`'s own docstring).

Semantics, enforced by one shared boundary helper `_admit_supplied_terminal_id` (`api/main.py`):

* **absent** → `terminal_id=None` reaches the service, i.e. today's server-side allocation, byte-identical
  to now, and the conflict lookup is never even called;
* **present and unknown** → adopted verbatim;
* **present and already known on this server** → refused **409** `{"code": "terminal_id_conflict", ...}`,
  no create runs, so a retried create is idempotent rather than id-stealing;
* **present and off-shape** → refused **400** before anything else (see §4, declared addition).

Threaded through `session_service.create_session` (for `/sessions` and `/sessions/start`) and straight into
`terminal_service.create_terminal` (for the terminals route).

### D15 third clause — recover refused on a box-plane server

New module `services/box_plane.py`: `CAO_SERVER_PLANE`/`is_box_plane()`/`BoxPlaneRecoveryRefused` +
`refuse_recovery_on_box_plane(reason)`. Called at the **entry** of both `recover_epoch`
(`epoch_recovery_service.py`) and `recover_provider_reauth` (`provider_rebind_service.py`) — at the service,
not the route, so the refusal holds for any future caller and fires before either service touches a
terminal. `POST /sessions/{name}/recover` maps it to **409** with the structured code; the arm precedes the
existing `ValueError` → 400 arm deliberately (this is not a bad request, it is a capability the serving
plane does not offer). Absent the env key the plane is `laptop` and nothing changes.

`refuse_recovery_on_box_plane` compares the **stripped** env value against the literal `box`; any other
value (including absent) is `laptop`, so a typo can never silently disarm a laptop server's recovery.

### D16 — the F620 shim becomes host-aware

`should_inject_shim(..., is_box_hosted: bool = False)` returns False for a box lane, before every other
clause; `maybe_shim_env(..., is_box_hosted=False)` forwards it; `terminal_service.create_terminal` gains
`is_box_hosted: bool = False` and passes it at the wiring call. Defaults reproduce today's behaviour
exactly for every existing caller.

---

## 2. Scope decisions, declared

1. **DECLARED DEVIATION — a NARROWING of D15's literal sentence. The D13 sender token is NOT in this
   slice.** Stated here so the reviewer can check the reasoning rather than discover the gap in the diff.
   The four citations that make the narrowing safe on the record's own facts: **AC20** arms the id;
   **AC21** arms `is_box_hosted`; **AC17** arms the sender token at the INBOX check (`api/main.py:7990`)
   and nowhere at create; **D13** states the process-wide `CAO_TERMINAL_TOKEN` is already the laptop's and
   needs "zero header plumbing". An unarmed third field on a credential path is worse surface than an
   absent one. What D15's prose says, verbatim: *"F634 adds an OPTIONAL `terminal_id` (and the D13 sender
   token, and D16's `is_box_hosted`) to ALL THREE routes"* — and D15 then gives explicit semantics for
   `terminal_id` ONLY. The deviation was raised with the team lead BEFORE building and the narrowing was
   confirmed; it is a narrowing, never a widening — the routes accept less than the sentence allows, and
   the field can be added later without changing anything shipped here.
2. **The recover clause IS in this slice.** It is D15's own wave-1 disposition and AC20's third clause, and
   it adds no field. Shipping D15's create half while leaving the recovery door open would leave the
   mutant AC20 names live.
3. **§7 deferrals honoured.** §7's two open uncertainties (catalog persistence, per-terminal backend) are
   NOT built here — nothing in this diff persists `host`/`is_box_hosted` on the terminal record or touches
   the backend singleton. AC7/AC7b/AC8/AC12 (F631-gated) are untouched.

---

## 3. Evidence

### 3.1 Suites (all on offload boxes)

Every run went through the slot lock — `bash scripts/box-run.sh <label> -- '<cmd>'`, never a raw ssh
suite, never a pgrep/sleep poll. Exact commands and exit codes:

```
# branch, full suite  -> box-run wrapper EXIT=0, payload RC=1 (pre-existing reds, §3.1 below)
CAO_BOXES="box@grok-box-002" scripts/box-run.sh f634-full-final -- \
  'cd /workspace/cao/home/f634-slice1 && echo HEAD=$(git rev-parse HEAD) > /workspace/cao/home/f634-full-final.txt \
   && uv run pytest -q -p no:randomly >> /workspace/cao/home/f634-full-final.txt 2>&1; echo "RC=$?" >> …'
   -> acquired box@grok-box-002; HEAD=e750dbfa…; RC=1

# base, full suite     -> box-run wrapper EXIT=0
CAO_BOXES="box@grok-box-004" scripts/box-run.sh f634-full-base -- \
  'cd /workspace/cao/home/f634-base && … uv run pytest -q -p no:randomly …'
   -> acquired box@grok-box-004; HEAD=5d39ec7f

# mutation ledger + lint/mypy -> box-run wrapper EXIT=0
CAO_BOXES="box@grok-box-004" scripts/box-run.sh f634-final2 -- \
  'cd /workspace/cao/home/f634-slice1 && … bash /workspace/cao/home/f634-mutants.sh'
```

`RC=1` on the branch full suite is pytest's nonzero for the pre-existing red set, not a wrapper failure;
the base run is nonzero for the same reason with one MORE failure. The counts below are from the run logs,
not re-derived.


| run | box | HEAD | result |
|---|---|---|---|
| new F634 arms only | grok-box-002 | `db3a8ce9` | **36 passed in 3.76s** |
| full fork suite, **BRANCH** | grok-box-002 | `e750dbfa` | **17 failed, 15003 passed, 229 skipped, 17 xfailed** (503.82s) |
| full fork suite, **BASE** | grok-box-004 | `5d39ec7f` | **18 failed, 14965 passed, 229 skipped, 17 xfailed** (544.39s) |
| targeted A/B of every failing node id | grok-box-002 | base vs branch, back to back | base 14 failed / 64 passed; branch 15 failed / 63 passed |
| flake A/B, 3 disputed arms, 3 reps each | grok-box-004 | base vs branch | **84 passed** on every one of the six runs |

**Branch failures ⊆ base failures, plus one load-sensitive arm.** 16 of the branch's 17 failures appear
verbatim in the BASE run — they are pre-existing and untouched by this slice. The one that does not
(`test/services/test_ready_deadline_edge_probe.py::test_ready_completion_at_deadline_has_one_lawful_owner
@quarantine-serial`) was chased down rather than waved away: run alone on the branch it passes **5/5**, and
run in its 3-file group it passes **3/3 on the branch and 3/3 on base**. It failed only while a second full
suite was running concurrently on the same box. Nothing in this diff touches ready deadlines or lawful
ownership. Symmetrically, BASE failed two arms the branch passed
(`test_fx191_convergent_delivery.py::…[not_idle]`, `test_suite_slot.py::…test_sample_ledger_monotonic_growth`)
— both in that same 3-file group that now passes 3/3 on both trees. The class is timing/subprocess
sensitivity under box load, in both directions.

**One real regression was found and fixed**: `test/api/test_api_endpoints.py::TestCreateSession::
test_create_session_success` pins `session_service.create_session`'s full kwarg set with
`assert_called_once_with`, so the two new kwargs broke it. The expectation now carries
`terminal_id=None, is_box_hosted=False` — the amendment's own defaults. It failed ONLY on the branch in the
targeted A/B, which is how it was caught, and it is absent from the final branch run.

### 3.2 Lint / types

* **black**: my 7 touched `.py` files are clean. Four files still report "would reformat"
  (`utils/terminal.py`, `services/epoch_recovery_service.py`, `services/session_service.py`,
  `services/provider_rebind_service.py`) — **measured pre-existing**: the identical four report the same at
  base `5d39ec7f` in a worktree with no F634 change at all, and the diff hunks are on lines this slice does
  not touch (`value[last_dash + 1:]`, a `grok_cli` path expression, two `logger.warning` wraps). Left alone
  deliberately: reformatting untouched lines is churn and upstream merge noise.
* **isort**: clean on every touched file.
* **mypy** (`--config-file mypy.ini`, strict): base `5d39ec7f` = **472 errors in 44 files (321 files
  checked)**; branch = **472 errors in 44 files (322 files checked)**. **Strict-mypy delta 0**; the extra
  checked file is the new `box_plane.py`.

### 3.3 Mutation ledger

Driver: `/data/cao-scratch/f634-mutants.sh`, run on grok-box-004 at the final HEAD `e750dbfa`. Each mutant
is applied by exact single-occurrence string replacement (the driver aborts if the anchor is not unique),
both F634 arms files are run, then the tree is reverted with `git checkout` and the named arm re-run. Final
tree: **0 modified files**; baseline re-run **36 passed**.

Per the record's own ledger direction (§5, r12/N3), the mapping is **mutation → AC**, not AC → mutation.

| # | mutation | named arm | mutated | reverted |
|---|---|---|---|---|
| M1 | `should_inject_shim` ignores `is_box_hosted` (predicate) | `TestPredicateIsHostAware::test_box_hosted_lane_is_never_shimmed` (AC21) | **RED** 1 failed | GREEN 1 passed |
| M2 | `create_terminal` stops threading `is_box_hosted` into `maybe_shim_env` (wiring) | `TestWiringThroughTheRealSpawnPath::test_box_hosted_worker_spawn_is_not_shimmed` (AC21) | **RED** 1 failed | GREEN 1 passed |
| M3 | amend only `POST /sessions` — terminals route drops `terminal_id` (the record's own AC20 mutant) | `TestSuppliedTerminalId::test_unknown_terminal_id_is_adopted[…/terminals]` (AC20) | **RED** 1 failed, 1 passed (the `/sessions` param stays green — exactly the silent fallback the mutant describes) | GREEN 2 passed |
| M4 | drop the conflict refusal | `TestSuppliedTerminalId::test_known_terminal_id_is_refused_as_a_conflict` (AC20) | **RED** 2 failed | GREEN 2 passed |
| M5 | drop the id-shape check | `TestSuppliedTerminalId::test_off_shape_terminal_id_is_refused_before_any_create` | **RED** 12 failed | GREEN 12 passed |
| M6a | drop the box-plane refusal from the epoch recover service | `TestBoxPlaneRecoveryRefusal::test_recover_is_refused_typed_and_creates_nothing[epoch]` (AC20 third clause) | **RED** 1 failed | GREEN 1 passed |
| M6b | drop it from the provider-rebind recover service | `…test_recover_is_refused_typed_and_creates_nothing[provider-reauth]` (AC20 third clause) | **RED** 1 failed | GREEN 1 passed |
| M7 | terminals route stops carrying `is_box_hosted` to the service (transport) | `TestIsBoxHostedFieldReachesTheService::test_is_box_hosted_is_threaded_verbatim` (AC21 transport) | **RED** 3 failed, 3 passed (only the terminals params) | GREEN 6 passed |

M1 and M2 are deliberately separate links: M1 (predicate) also reddens the layer-2 arm, but M2 (wiring)
reddens **only** the layer-2 arm and leaves every predicate arm green — which is the r6/N1 point that the
flag is one more parameter or the predicate never sees it. M6a additionally reddens
`test_laptop_plane_is_not_refused[trailing-space-value]`, correctly: that case asserts the stripped `box `
value IS refused.

**No arm in this ledger is a text-presence assertion.** Every one observes behaviour: the composed `PATH`
handed to `create_window` through the REAL `create_terminal` worker branch (layer 2 reuses the F636 spawn
harness with a real nested root+fork git layout on disk), the kwargs a route hands its service, an HTTP
status plus structured code, or a service's first observable step never running.

---

## 4. Additions beyond the record's literal text, declared

**The 8-hex shape check on an adopted `terminal_id` (400).** The record states three semantics and is
silent on shape. Making a server-private parameter public exposes an assumption the whole tree already
makes: `utils/terminal.resolve_terminal_id` fast-paths on `[a-f0-9]{8}` and extracts that same shape as the
trailing segment of the `<profile>-<id>` display form, and `generate_window_name` composes the tmux window
name from it (already `validate_tmux_name`-checked, so an off-shape id would surface as a deep 500 rather
than a boundary refusal). An adopted off-shape id would be accepted and become unresolvable later. This
addition only **narrows** what the route accepts — it adds no capability — and it is billed with its own
mutant (M5) and 12 parametrized cases. New public helper: `utils/terminal.is_raw_terminal_id`.

---

## 5. Files

Fork, branch `f634-slice1-d15-d16` @ `e750dbfa`:

* `src/cli_agent_orchestrator/api/main.py` — `_admit_supplied_terminal_id`, three routes, `/recover` arm
* `src/cli_agent_orchestrator/services/box_plane.py` — NEW, the server-plane key + typed refusal
* `src/cli_agent_orchestrator/services/laptop_shim.py` — `is_box_hosted` on predicate + compose helper
* `src/cli_agent_orchestrator/services/terminal_service.py` — `is_box_hosted` param + wiring call
* `src/cli_agent_orchestrator/services/session_service.py` — threads `terminal_id`/`is_box_hosted`
* `src/cli_agent_orchestrator/services/epoch_recovery_service.py` — box-plane refusal at entry
* `src/cli_agent_orchestrator/services/provider_rebind_service.py` — box-plane refusal at entry
* `src/cli_agent_orchestrator/utils/terminal.py` — `is_raw_terminal_id`
* `test/api/test_f634_create_route_amendment.py` — NEW, AC20 + the transport half of AC21
* `test/services/test_f634_shim_host_awareness.py` — NEW, AC21 (predicate + real spawn path)
* `test/api/test_api_endpoints.py` — the exact-kwargs expectation, updated with the amendment's defaults

---

## 6. Not done / for the next slice

* D13 sender token on the create routes (deferred by the lead's ruling — §2.1).
* Nothing persists `host`/`is_box_hosted` on the terminal record; the record names that as the follow-on
  when per-lane recovery returns, along with threading `terminal_id`/`caller_id`/`is_box_hosted` through
  both recover services and the workflow-runner creates (`flow_service.py`, `agent_step.py`).
* `scripts/box-cao-up.sh` (D2) does not exist yet, so nothing sets `CAO_SERVER_PLANE=box` in production
  today — the refusal is dormant until that slice lands, which is the correct sequencing (this slice builds
  the server-side half the launcher will arm).
* AC21's end-to-end form (a real box dev lane running the fork suite in its own box worktree) needs the
  D1/D2/D3 slices; what is proven here is the decision and its full transport, at the seam the record names.

---

## 7. Reproducing this evidence

The two boxes hold ready-made worktrees (deliberately left in place so the EMPIRICAL gate can re-run the
ledger without a rebuild; remove with `git worktree remove` when done):

* `box@grok-box-002:/workspace/cao/home/f634-slice1` — branch `f634-slice1-d15-d16` @ `e750dbfa`
* `box@grok-box-004:/workspace/cao/home/f634-slice1` — same
* `box@grok-box-004:/workspace/cao/home/f634-base` and `…002:/workspace/cao/home/f634-base` — detached `5d39ec7f`

Both box repos carry the branch ref (pushed over ssh directly to
`/workspace/cao/home/cli-subagents/cli-agent-orchestrator`; nothing was pushed to any GitHub remote and
nothing was merged). The mutation driver is at `/workspace/cao/home/f634-mutants.sh` on both boxes and at
`/data/cao-scratch/f634-mutants.sh` locally.

```
CAO_BOXES="box@grok-box-004" scripts/box-run.sh f634-ledger -- 'bash /workspace/cao/home/f634-mutants.sh'
```

Logs: `/data/cao-scratch/f634-{final2,full-final,full-base,ab,flake-ab,flake-rep,flake-rep2,mypy-delta}.log`.

---

## 8. Incidental finding (not a code change)

While probing box 002 I ran `ssh <host> 'pgrep -c -f "uv run pytest"'` and it reported a live process when
there was none: the ssh remote-command **argv carries the pattern**, so `pgrep -f` matched its own remote
shell. That is exactly the D5(2) self-match trap the record documents, observed live on the fleet, and it
confirms the record's rule empirically — re-running the same probe fed over ssh **stdin** (`ssh <host>
bash -s`) returned `pytest_procs=0` correctly. No F634 code depends on this; recording it because D5(2)'s
delivery rule is a wave-1 contract for `box-cao-up.sh` and this is a free confirmation of it.
