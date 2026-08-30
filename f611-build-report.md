# F611 (#467) — provider condition detection: build report (r3)

- **Branch@sha:** `cao/b16d0d8c` (code tip `0c78ed06619f4f40f3ddfc06e2f4fb5ff312d195`; this report is the tip's final report-only commit)
- **Base:** `62e767f99b909b538f93c10d258e211ab1588d1e` (fork main)
- **Blueprint:** `orchestrator/blueprints/f611-condition-detection.md` r5, sha256 `5f23f318b0dca7a3e135061177df527bd0af95ac12c535a9ef03ad491cdc36dc`
- **Gate history:** DESIGN-GATE-YES r5 (B=0 S=0 N=2); EMPIRICAL-GATE-NO r1 (B=1 S=1, library-only) → folded; EMPIRICAL-GATE-NO r2 (B=2 S=0: non-load-bearing wiring mutant + unwired fleet surface) → folded here.
- **Verification host:** grok-box-002 / grok-box-004 (box-run.sh slot-locked; auto-suspend failover exercised)

## r2 → r3 fold

- **B1 (accepted):** every r2 wiring test invoked `_classify_and_deliver_condition` directly; none went through `_apply_detection`, so the literal "replace the `status_monitor.py:1276` call with `pass`" mutant survived the whole wiring file. And the r2 ledger's "W.m1 drop the poll-site call" actually mutated an early-return *inside the helper*, not the call site — the label was inaccurate. **r3 adds `test_apply_detection_transition_drives_condition_fanout`**, which drives a real IDLE→COMPLETED transition through `_apply_detection` itself (the production `publish_external` seam) and asserts the fan-out fired. The literal poll-site-call mutant now goes **RED**. The ledger below describes every mutant as what it actually edits.
- **B2 (accepted):** the r2 fleet surface was unwired — `fleet_service.build_fleet` had no `condition` key and nothing subscribed to `terminal.{id}.condition`. **r3 wires the fleet RESPONSE**: `build_fleet` projects a `condition` key on every terminal row via `status_monitor.get_condition`, so `/sessions/{name}/fleet` carries it (blueprint §3 surface 1). A test asserts the payload carries the condition after a transition; the projection-drop mutant goes RED. **The Rust fleet-TUI cell rendering is scoped out** — see "Surface delivery status" below.

## What was built, per D-row

Detection-first (§0): a read-time projection BESIDE `get_status`, connected to production and surfaced on the fleet response. `StatusMonitor.fuse_status` and the 6-value `TerminalStatus` are **byte-untouched** (proven by AC2 calling fuse_status, and by the mypy A/B diff).

| D-row | Contract | Implementation | Arm |
|---|---|---|---|
| **D1** | condition is a SEPARATE typed field, not a `TerminalStatus` member | `Condition`/`ConditionKind`; `Terminal.condition`; live `_last_condition`+`get_condition`; `get_terminal` + `build_fleet` read it; fusion unchanged | AC2 (calls fuse_status) |
| **D2** | banner-only scan; user-prefix regions suppressed | `banner_rows()` reuses codex `USER_PREFIX_PATTERN` | AC5 |
| **D3** | only high/medium deliver; low logs | `Confidence`; `should_deliver`; delivery gate | AC7 |
| **D4** | ONE event, fanned out; never three producers | `ConditionDelivery` performs fan-out via sinks; de-dup `(tid,kind,subtype,epoch)`; wired at the transition seam | AC8 + wiring arms |
| **D5** | BUSY precedence 7; PROC_EXITED first | `PRECEDENCE`; min-rank winner | AC3 (cap-outranks-busy) |
| **D6** | box-plane cap `scope=credential_plane`, advisory only | scope stamp; `policy_for_condition` → ADVISORY_ONLY | AC9 |
| **D7** | CAPPED is runtime, NOT a routing refusal | policy consumes event; no `E-PROVIDER-CAPPED` | AC10 |
| **D8** | auth/dialog STOP and ask; never rebind | `policy_for_condition` → STOP_AND_ASK | AC11 |

### Runtime wiring (one event per transition → three surfaces, §3/D4)

1. **Detection at the production poll site** — `services/status_monitor.py` `_apply_detection`: inside the `publish_external` block (the published status-transition seam), after the `terminal.{id}.status` publish, the code reads the live pane buffer + provider and calls `_classify_and_deliver_condition(...)`. Off the monitor lock; failures swallowed. **This exact call is now guarded by `test_apply_detection_transition_drives_condition_fanout`** (the literal mutant replacing it with `pass` goes RED).
2. **`ConditionDelivery` performs the three-surface fan-out** via injected sinks:
   - **Fleet field (§3 surface 1):** `_condition_fleet_sink` sets/clears `_last_condition`; `get_condition()` reads it. TWO consumers now read it: `terminal_service.get_terminal` → `Terminal.condition` (terminal-detail egress), and **`fleet_service.build_fleet` → the `condition` key on each `/sessions/{name}/fleet` terminal row** (the fleet RESPONSE, r3 B2).
   - **Supervisor inbox (§3 surface 2):** `_condition_inbox_sink` enqueues exactly ONE `clients.database.create_inbox_message(...)` (`Condition.render_event`) from the affected terminal to its recorded `caller_id`, then `request_delivery`. De-duped per epoch.
   - **CLI/bus projection (§3 surface 3):** `_condition_cli_sink` publishes a distinct `terminal.{id}.condition` bus frame (never the `status` frame).

### Surface delivery status (honest §3 mapping)

- **§3 surface 1 (fleet field): DELIVERED — both halves of the response.** `Terminal.condition` on terminal-detail egress AND the `condition` key on the `/sessions/{name}/fleet` response row.
- **§3 surface 2 (supervisor inbox): DELIVERED.** One `create_inbox_message` per transition to the recorded caller.
- **§3 surface 3 (CLI/bus): partially delivered.** The `terminal.{id}.condition` bus frame is published, and `cao fleet` (which calls `build_fleet`) now carries the condition in its JSON. **The Rust fleet-TUI *cell rendering* (drawing the CAPPED/BLOCKED/AUTH label in the grid) is SCOPED OUT of this WP** — it is a `tui/` (Rust/ratatui) change requiring a cargo build. The TUI `Terminal` struct deserializes with `#[serde(default)]` and no `deny_unknown_fields` (verified `tui/src/types.rs:146-162`), so the new `condition` key rides the wire without breaking the current TUI; a follow-up adds the cell. The data surface the TUI consumes is delivered; only the visual cell is deferred.

## Test list + counts

**49 passed** (grok-box, at 0c78ed06).

`test_f611_condition_detection.py` (38): AC1 taxonomy (17 parametrized) + status_truth + reset-hint; AC2 ★ (calls fuse_status before/after); AC3 busy guard + cap-outranks-busy; AC4 precedence; AC5 ★ + raw-tail witness; AC6 reset≠cap; AC7 gate; AC8 ★; AC9 ★ + no-scope witness; AC10; AC11 ★; AC12; AC13; AC14; AC15; threshold; event-render.

`test_f611_condition_wiring.py` (11): delivery performs three surfaces; dedup suppresses extra inbox+CLI; low-confidence no-op; None clears fleet; helper sets all three surfaces; fleet field read back by `get_condition`; no-provider safe no-op; **`_apply_detection` transition drives the fan-out end-to-end (B1)**; no-transition does not fire; **fleet response carries the condition after a transition (B2)**; fleet condition None when absent.

## Mutation ledger — 30/30 killed, 0 survived (grok-box-002)

Each row describes the mutant as WHAT IT ACTUALLY EDITS (r2's W.m1 label was corrected).

| RED node → arm | mutants (all KILLED) |
|---|---|
| D1 → AC2 | add `CAPPED` enum member; alter `IDLE` enum value; add `BLOCKED` enum member |
| D2 → AC5 | `banner_rows` returns raw rows; disable `USER_PREFIX` suppression; keep suppressed continuation rows |
| D3 → AC7 | `should_deliver` always True; inferred proc-exit HIGH not LOW; deliver ignores confidence gate |
| D4 → AC8 | same-epoch dedup disabled; dedup key omits epoch; suppressed repeat still delivers |
| D5 → AC3 | BUSY rank 0.5; BUSY rank 3.9; CAPPED rank 9.0 |
| D6 → AC9 | scope forced `provider`; policy ignores scope; drop `credential_plane` on Condition |
| D7 → AC10 | add `E-PROVIDER-CAPPED` (×2 placements); refusal value leaks into `ADVISORY_ONLY` |
| D8 → AC11 | auth/dialog rebinds to kiro; auth/dialog returns NONE; AUTH drops out of stop set |
| **WIRING** | **W1: the LITERAL poll-site call `_classify_and_deliver_condition(...)` at the `_apply_detection` transition seam → `pass`** (the r2 survivor, now RED via the new arm); W2: `_classify_and_deliver_condition` early-return no-op after the provider guard; W3: `ConditionDelivery` fleet-sink body dropped; W4: `ConditionDelivery.deliver` skips the whole fan-out |
| **B2 fleet response** | build_fleet condition projection replaced with `None`; the `"condition"` row key removed |

Harness: `/data/cao-scratch/b16d0d8c/mutate_f611_r3.sh`. Independently, `run_f611_r3.sh` re-verifies the two B1 mutants (literal poll-site drop + helper early-return) and the two B2 mutants each go RED after the full 49-pass suite. Summary: `MUTANTS_KILLED=30 MUTANTS_SURVIVED=0`.

## mypy --strict parity (base vs head, touched src)

Line-number-normalized A/B over the 10 pre-existing touched files (adds `services/fleet_service.py` vs r2):

- **base = 202 errors, head = 202 errors → NET NEW 0** (normalized-signature diff = 0). fleet_service.py carries 1 pre-existing strict error present in BOTH revisions (hence 202 vs r2's 201); F611 added none.
- `providers/condition.py` (net-new file): **0 errors.**

## Format

`black --check -l100` RC=0, `isort --check-only --profile black -l100` RC=0 (net-new + 10 touched src + 2 test files).

## Diff scope (`git diff --name-status 62e767f9..<code tip>`)

```
M	src/cli_agent_orchestrator/models/terminal.py
M	src/cli_agent_orchestrator/providers/base.py
M	src/cli_agent_orchestrator/providers/claude_code.py
M	src/cli_agent_orchestrator/providers/cline_cli.py
M	src/cli_agent_orchestrator/providers/codex.py
A	src/cli_agent_orchestrator/providers/condition.py
M	src/cli_agent_orchestrator/providers/grok_cli.py
M	src/cli_agent_orchestrator/providers/kiro_cli.py
M	src/cli_agent_orchestrator/services/fleet_service.py       (B2: fleet response condition projection)
M	src/cli_agent_orchestrator/services/status_monitor.py      (runtime wiring: detection at transition, delivery sinks, get_condition)
M	src/cli_agent_orchestrator/services/terminal_service.py    (get_terminal reads condition; Terminal(condition=None) at create)
A	test/providers/fixtures/conditions/INDEX.md
A	test/providers/fixtures/conditions/{17 fixtures × (.txt + .json)}
A	test/providers/test_f611_condition_detection.py
A	test/providers/test_f611_condition_wiring.py
```
48 code/fixture paths total. The 17 condition fixtures are byte-exact from `/data/cao-scratch/9064394e/fixtures/conditions/` (each `.txt` sha256 verified against its `.json` sidecar).

## Deferred rows / scope

Deferred taxonomy cells (blueprint §7 capture-on-encounter; `classify_condition` returns `None` until a byte-exact screen lands): NET on codex/grok/cline/claude (kiro CLOSED); PROC_EXITED text-anchor on codex/kiro/grok/claude (process-state path only); CAPPED claude; AUTH kiro/grok/cline; CONTEXT grok/cline; DIALOG cline/kiro; TRANSIENT grok/cline/claude.

Scoped out (documented above): the Rust fleet-TUI **cell rendering** for the condition label — the fleet RESPONSE data is delivered; only the visual grid cell is a follow-up. Also out of scope (§7): the F574 #431 chain-walk; the ROUTING.md→routing.toml generator; any change to `fuse_status`/`TerminalStatus`; auto-answering auth/login/usage-reset dialogs (D8 human gate).

## box-actions ledger

All pytest/mypy/mutation compute on offload boxes via `scripts/box-run.sh`; NO local pytest/mypy (local black/isort only). Payloads shipped as script files.

| # | box | label | action | result |
|---|---|---|---|---|
| 1 | grok-box-002 | f611-r3 | `pytest` detection+wiring @ 3b9742b5 + B1-literal/B2 mutant checks | 49 passed; all 4 mutants RED |
| 2 | grok-box-002 | f611-r3-mutate | 30-mutant ledger (D1-D8 + 4 wiring + 2 B2) @ 3b9742b5 | 30/30 killed, 0 survived |
| 3 | grok-box-002 | f611-r3-mypy | full A/B mypy + black/isort @ 3b9742b5 | base=202 head=202 net-new 0; condition.py 0; black/isort RC0 |
| 4 | grok-box-002 | f611-r3-final | `pytest` + B1/B2 mutant re-check @ 0c78ed06 (squashed) | 49 passed; 4/4 mutants RED |

No box work touched the production server (:9889) or its DB/config; no `cao sandbox` used. Boxes auto-suspend (002/004/005/006 flapped mid-session); box-run failed over per exit-76 semantics.

## Containment

Edited only inside the worktree `.cao/worktrees/b16d0d8c`; scratch under `/data/cao-scratch/b16d0d8c/`; no `/tmp` on the laptop; no recursive grep of `~/` or `~/.claude`; did not touch `orchestrator/ROUTING.md`, GOLDEN-TIPS, HANDOFF, or MISTAKES.
