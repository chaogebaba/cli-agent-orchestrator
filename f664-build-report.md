# F664 (#519) build report — register `omp` and `mcode` in `PROVIDER_CLASSES`

**Repo:** fork `cli-agent-orchestrator` (nested, own `.git`).
**Branch:** `f664-register-omp-mcode` (pushed to `origin`).
**Build HEAD:** `b9e3f33f5072c93bc607ae9c708ec8bbfe8c46fd`
**Base / fork main:** `5d39ec7fd67803b82f685e8a95dfb41130d34340`
**Worktree:** `/data/cao-scratch/f664-wt`
**Box:** `box@grok-box-002` (both runs; acquired via `scripts/box-run.sh`). No pytest,
mypy, or build ran on the laptop.

---

## Governing record and conflict check

There is **no F664 blueprint**. The frozen record that governs this issue is the
`## Decision Amendment — F662` section inside
`orchestrator/blueprints/f497-position-provider-decoupling.md` (amendment r4 FROZEN,
root commit `fcd18cfa`). Its D1 validation rule (2) names the oracle and, at
`f497-position-provider-decoupling.md:611-624`, records this exact divergence:

> **Oracle, named (r1/B2):** `providers/manager.py`'s `PROVIDER_CLASSES` (:33-46), NOT
> `models/provider.py`'s `ProviderType` (:4-21) — the two disagree (14 enum values vs
> 12 registry keys; `omp` and `mcode` are dispatchable at `manager.py:205,265` yet
> absent from the registry …). **Making them un-defaultable regresses nothing (r2/N5):
> they are already unspawnable.** The enum/registry divergence is a real fork defect,
> **to be filed as its own issue, NOT worked around here (r2/N4).**

That issue is #519. **The record does not pick a fix** — it deliberately defers the
choice. Issue #519 states the two options and requires a deliberate pick.

**No conflict between the brief and the record.** The brief says "register", which is
the issue's option (a); the record neither mandates nor forbids it. Two facts in the
frozen text point the same way and are the basis for the pick:

1. §F574 build step 5a (`:484`) enumerates the provider modules that must gain
   `SETTABLE_KNOBS` / `classify_spawn_failure` and **lists `omp` and `minimax` among
   them**. `SETTABLE_KNOBS` is reachable only through `get_provider_class` (`:621-623`),
   so the frozen plan already assumes those two resolve through the registry.
2. The r2/N5 note ("un-defaultable regresses nothing") is a *justification for not
   blocking F662 on this*, not a requirement that they stay unregistered. Registering
   them grows the oracle; it does not change which symbol is the oracle, so D1's
   `E-DEFAULT-UNKNOWN-CLI` contract is untouched. After this change `[defaults] omp = …`
   becomes legal — that is a widening of what F662 accepts, not a change to how it
   decides, and it is the direction the record's own reasoning points.

Option (b) — deleting the enum members and the `construct_provider` branches — was
rejected on evidence, not preference: both providers are complete implementations with
dedicated unit suites (`test/providers/test_omp_unit.py`,
`test/providers/test_native_status_shared.py:58,64`), live e2e coverage
(`test/e2e/test_supervisor_orchestration.py:885-924`, `test/e2e/test_skills.py:269`,
`test/e2e/test_handoff.py:429`), installer support
(`test/services/test_install_service.py:1320`), API surface
(`test/api/test_api_endpoints.py:139-194`), and entries in
`RUNTIME_SKILL_PROMPT_PROVIDERS` / `SOFT_ENFORCEMENT_PROVIDERS`. Deleting them would
delete working code.

---

## The change

```
 src/cli_agent_orchestrator/providers/manager.py       | +2
 src/cli_agent_orchestrator/providers/minimax_code.py  | +5
 test/providers/test_f664_registry_completeness.py     | +115 (new)
```

**1. `providers/manager.py:44,46`** — two registry rows, placed in enum order:

```python
    ProviderType.ANTIGRAVITY_CLI.value: AntigravityCliProvider,
    ProviderType.OMP.value: OmpProvider,
    ProviderType.CLINE_CLI.value: ClineCliProvider,
    ProviderType.MINIMAX_CODE.value: MiniMaxCodeProvider,
    ProviderType.MOCK_CLI.value: MockCliProvider,
```

Both classes were **already imported** at `manager.py:26,27` for `construct_provider`'s
branches — the registry was the only thing missing.

**2. `providers/minimax_code.py:318-323`** — `CAO_TERMINAL_TOKEN` forwarding in
`_write_plugin`, mirroring `omp.py:199-203`.

**This is not scope creep; it is what makes the registration valid.** `PROVIDER_CLASSES`
is not only a lookup table — `test/providers/test_terminal_token_forwarding.py` (F332
AC11) *iterates it* and asserts that every registered provider which injects
`CAO_TERMINAL_ID` also forwards `CAO_TERMINAL_TOKEN`. `minimax_code._write_plugin` set
`CAO_TERMINAL_ID` at `:318` and nothing else, so registering `mcode` without this line
turns that suite red — measured, see M3 below. The underlying defect is real beyond the
test: an `mcode` worker's MCP servers would have carried an unauthenticated callback
identity. `omp` already complied and needed no change.

**3. `test/providers/test_f664_registry_completeness.py`** — the registry ⊇ enum
invariant (parametrized over all 14 `ProviderType` values, so a future enum member that
skips the registry fails at once) plus one named arm per fatal call site.

---

## Call-site walk

The brief carried a **five-call-site** requirement. The real number depends on which
question is asked, and both answers are given here because only the pair is honest:

- **10** call sites of `get_provider_class` exist in `src/`.
- **5** of them are *fatal* — a `ValueError` from `get_provider_class` fails the
  operation. These are the five the brief means, and they are exactly the sites at which
  `omp`/`mcode` were unspawnable.

Fatality was determined by AST, not by eye: for each call, every enclosing `Try` whose
`body` contains the call was collected with its handler types, then each handler was read
to see whether it *recovers* or merely cleans up and re-raises. `terminal_service.py:1845`
(`except BaseException:` → release lock, `raise`) and `:2079` (`except Exception:` →
cleanup, `raise`) are syntactically wrapped but semantically unguarded, which is why
issue #519 calls them unguarded; `mcp_server/server.py:1470` catches but converts the
failure into `{"success": False, "message": "Interrupt failed: …"}`.

### The five fatal sites

| # | Site | What it reads off the class | Disposition after registration |
|---|------|------------------------------|-------------------------------|
| 1 | `services/session_service.py:355` | `supports_seed_resume_identity` | **Handled.** Neither provider overrides it → inherits `BaseProvider.supports_seed_resume_identity = False` (`base.py:139`) → `seed_mode` False → `start_session` reports `bootstrap.mode = "not_applicable"`. Correct: neither implements `seed_resume_identity`. Arm `test_site1_start_session_seed_probe_resolves`. |
| 2 | `services/terminal_service.py:546` | `supports_seed_resume_identity`, then `seed_resume_identity` | **Handled.** `seed_resume_bootstrap` returns `None` at the first check and never reaches `seed_resume_identity`. Arm `test_site2_seed_resume_bootstrap_returns_none` calls the real coroutine. |
| 3 | `services/terminal_service.py:1845` | `supports_seed_resume_identity` (the `seed_required` guard) | **Handled.** `False` → the `raise RuntimeError("seed_required")` branch is not taken and terminal creation proceeds. Arm `test_site3_create_terminal_seed_required_guard`. |
| 4 | `services/terminal_service.py:2079` | `preflight_launch` (F295 preflight, before any resource allocation) | **Handled.** Neither overrides the hook, so `BaseProvider.preflight_launch` (`base.py:145-148`, documented no-op returning `None`) is awaited. Arm `test_site4_f295_preflight_hook_awaits_clean` awaits it. |
| 5 | `mcp_server/server.py:1470` | `interrupt_keys` | **Handled.** Inherits `["C-c"]` (`base.py:155`); neither provider overrides. Before this change, interrupting an `omp`/`mcode` terminal returned `Interrupt failed: Unknown provider type: omp`. Arm `test_site5_interrupt_keys_resolve`. |

### The five recovering sites (walked, none needs a change)

| # | Site | Before | After | Verdict |
|---|------|--------|-------|---------|
| 6 | `services/epoch_recovery_service.py:135` | `except ValueError: supports = False` → `provider_lacks_fork_capability` | class resolves, `supports_fork_context` inherits `False` (`base.py:137`) → `provider_lacks_fork_capability` | **Provably unaffected** — identical outcome by both routes. |
| 7 | `services/fork_context_service.py:740` | `except ValueError:` → registration rejected `provider_unknown` | resolves, `supports_fork_context` False → rejected `fork_unsupported` | **Intentional refinement.** Both still reject; the error code becomes the accurate one. No test asserts the old code for these providers: `test/services/test_offline_base_registration.py:144` drives `provider_unknown` with the literal `"missing_provider"`, not `omp`/`mcode`. |
| 8 | `mcp_server/server.py:1857` | `except ValueError: supports_fork = False` → `raise ValueError("provider_lacks_fork_capability")` | resolves, `supports_fork_context` False → same raise | **Provably unaffected** — identical outcome. |
| 9 | `services/terminal_service.py:2091` | `except Exception: pass` → `_has_process_child` keeps its `True` default | resolves, `getattr(cls, "has_process_child", True)` → `True` (`base.py:150`) | **Provably unaffected** — identical value. Correct on the merits: both are real CLIs with a process child. |
| 10 | `cli/commands/auto_answers.py:40` | `except ValueError:` → "unknown provider … reporting without chrome filtering" | resolves; `diagnose_rules` calls `_chrome_patterns_for_class` (`auto_responder.py:328-337`), which is `getattr(cls, "_CHROME_ROW_PATTERNS", None)` | **Handled.** Neither provider defines `_CHROME_ROW_PATTERNS`, so patterns are `None` and the diagnostic falls back to the unfiltered region — the same region as before, now without the spurious "unknown provider" note. |

### Registry consumers other than `get_provider_class`

- `test/providers/test_terminal_token_forwarding.py:16-24` **iterates `PROVIDER_CLASSES`**
  and parametrizes over it: 12 params on main → 14 on this branch. This is the consumer
  that forced change 2 above.
- `test/providers/test_cline_cli_unit.py:680-683` asserts one specific key; unaffected.
- No `src/` code iterates the registry.

### Adjacent gates deliberately left alone

- `constants.PROVIDERS` / `install_service.py:378` derive from `ProviderType` and already
  admitted all 14 — the registry was the sole blocker, exactly as #519 says.
- `utils/provider_plane.admit_provider:387` rejects anything outside
  `{codex, claude_code, grok_cli}` in a sandbox. `omp`/`mcode` stay sandbox-inadmissible;
  registration does not and should not change that.
- `services/provider_rebind_service.py:22` imports `get_provider_class` and never calls
  it (pre-existing dead import). Not touched — out of scope for #519, noted for the
  tracker.

---

## Mutation ledger

Box `box@grok-box-002`. Targeted set for every row:
`test/providers/test_f664_registry_completeness.py`,
`test/providers/test_terminal_token_forwarding.py`,
`test/providers/test_provider_manager_unit.py` (**71 tests**). Driver:
`/data/cao-scratch/f664-ledger-remote.sh`; raw log `/data/cao-scratch/f664-ledger-out.txt`.
Each mutation was applied to the treatment tree, run, then reverted with
`git checkout -- src test`.

**Baseline probe on `origin/main` (`5d39ec7f`)** — the bug, measured:

```
registry keys: 12 [antigravity_cli, claude_code, cline_cli, codex, copilot_cli,
                   cursor_cli, grok_cli, hermes, kimi_cli, kiro_cli, mock_cli, opencode_cli]
enum values  : 14
omp   -> ValueError: Unknown provider type: omp
mcode -> ValueError: Unknown provider type: mcode
```

**Treatment probe (`b9e3f33f`)**:

```
registry keys: 14 [… mcode … omp …]
enum values  : 14
omp   -> OmpProvider
mcode -> MiniMaxCodeProvider
```

| Mutation | Named arms turned RED | Negative controls that stayed GREEN | Result |
|----------|----------------------|-------------------------------------|--------|
| **M1** — delete `ProviderType.OMP.value: OmpProvider,` from the registry | `test_registry_covers_every_provider_type[omp]`, `test_registry_class_matches_construct_provider_branch[omp]`, `test_site1_start_session_seed_probe_resolves[omp]`, `test_site2_seed_resume_bootstrap_returns_none[omp]`, `test_site3_create_terminal_seed_required_guard[omp]`, `test_site4_f295_preflight_hook_awaits_clean[omp]`, `test_site5_interrupt_keys_resolve[omp]` | all 7 `[mcode]` arms, both minimax token arms, all 12 other `test_registry_covers_every_provider_type` params, `test_provider_forwards_terminal_token…[mcode]`, 29 provider-manager tests | **7 failed, 63 passed** |
| **M2** — delete `ProviderType.MINIMAX_CODE.value: MiniMaxCodeProvider,` | the same 7 arms, `[mcode]` variants | all 7 `[omp]` arms, both minimax token arms, `test_provider_forwards_terminal_token…[omp]`, 29 provider-manager tests | **7 failed, 63 passed** |
| **M3** — delete the `CAO_TERMINAL_TOKEN` block from `minimax_code._write_plugin` | `test_minimax_plugin_forwards_terminal_token`, `test_provider_forwards_terminal_token_if_it_forwards_terminal_id[mcode]` | `test_minimax_plugin_omits_token_when_unset`, all 14 registry arms, all 10 site arms, `[omp]` token param | **2 failed, 69 passed** |
| **M4** — point the `omp` row at `MockCliProvider` (wrong class, still a `BaseProvider`) | `test_registry_class_matches_construct_provider_branch[omp]` **only** | `test_registry_covers_every_provider_type[omp]` — deliberately still GREEN, because `MockCliProvider` *is* a `BaseProvider` subclass | **1 failed, 70 passed** |
| **REVERT** — `git checkout -- src test`, no mutation | — | — | **71 passed** |

M4 exists to prove the arms are coupled to what the registry *maps to*, not merely to
key presence: the coarse invariant survives it and only the identity arm fires. No arm
in this ledger asserts the presence of text.

---

## Suite, types, format

Driver `/data/cao-scratch/f664-suite-remote.sh`; raw log
`/data/cao-scratch/f664-suite-out.txt`. Box `box@grok-box-002`.

| Run | Result |
|-----|--------|
| **Default suite, baseline `origin/main` `5d39ec7f`** | `18 failed, 15006 passed, 214 skipped, 17 xfailed in 514.11s` |
| **Default suite, treatment `b9e3f33f`** | `18 failed, 15036 passed, 214 skipped, 17 xfailed in 520.37s` |
| **Delta** | **+30 passed**, failure count unchanged at 18 |

The +30 is fully accounted for and was predicted before the run: 28 tests in the new
`test_f664_registry_completeness.py` (14 registry params + 2×5 site arms + 2 identity
arms + 2 minimax token arms) plus the 2 new `[omp]`/`[mcode]` params that
`test_terminal_token_forwarding.py` gains by iterating a 14-key registry instead of a
12-key one.

The failure *count* is identical but the failure *set* differs by one swap, reported as
measured rather than smoothed over:

- treatment gains `test/services/test_ready_deadline_edge_probe.py::test_ready_completion_at_deadline_has_one_lawful_owner@quarantine-serial`
- treatment loses `test/services/test_fx191_convergent_delivery.py::TestS2AC14MultiTickConvergence::test_safety_gate_obligations_escalate_within_bound[waiting_user_answer]`

Both are timing/concurrency arms (one runs in the `quarantine-serial` xdist group).
Neither file contains a single reference to `get_provider_class`, `PROVIDER_CLASSES`,
`omp`, or `mcode` — verified by grep, count 0 in both — so neither can be coupled to this
diff. The 17 failures common to both runs are pre-existing on `origin/main`.

`mypy --strict` on the two touched source files: **2 errors on baseline, the same 2 on
treatment** — `minimax_code.py` `_try_load_profile` is missing a return annotation and is
called from typed context (baseline `:398`/`:427`, treatment `:403`/`:432`; the +5 shift
is exactly my five inserted lines). Pre-existing, untouched, no new error.

`black --check --line-length 100` and `isort --check-only` on all three touched files:
clean, rc=0, "3 files would be left unchanged."

The baseline failures are pre-existing on `origin/main` and environmental (real-home PII
in fixtures, tmux-site sandbox probes, timing/simulation arms). They are reported as
measured, not adjudicated.

---

## What is not in this change

- **No `SETTABLE_KNOBS` / `classify_spawn_failure`** for `omp`/`mcode`. §F574 build step
  5a owns that migration for all 16 modules; this change only makes the class reachable,
  which is 5a's precondition.
- **No `providers.toml` / `routing.toml` edits.** F662's `[defaults]` table does not exist
  yet; registering the classes is what will let a future `[defaults] omp = …` validate.
- **No sandbox admission** for `omp`/`mcode` (see site walk, "adjacent gates").
- **No fix for the dead `get_provider_class` import** in `provider_rebind_service.py:22`.
- **No live spawn of `omp` or `mcode`.** Neither binary is on the box or the laptop; the
  e2e suites for both are gated on `_cli_available` (`test/e2e/conftest.py:223,283`) and
  are excluded from the default suite. What is proven here is that the five fatal
  resolution sites now resolve and behave correctly, not that a real `omp` terminal
  reaches ready.
