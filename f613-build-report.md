# F613 #469 — fork hotfix build report

## Authority and result

- Issue: F613 #469 (fork hotfix). Fork base: `b2814464` (main tip at task start).
- Worktree: `.cao/worktrees/f613`; branch `cao/f613-position-alias`.
- Fix commit: **`b1a366279dac8ee247d4ff45ff4b58583435b068`**.
- Result: both oracle-pinned bugs fixed, tested (11 new + 2 updated), 2 mutants
  killed, black/isort L100 clean, mypy --strict parity delta 0, box-verified on
  grok-box-004. Pushed.

## Bugs fixed

### Bug 1 — general fallback used the raw provider id, not the alias stub stem

`utils/routing.py` `resolve_routing_binding`, the non-gate general-substitution
branch (was line 391) returned:

```python
fallback = f"{provider}_{GENERAL_POSITION}"   # e.g. "cline_cli_general"
```

But the INSTALLED general alias stubs are named `<short>_general`:
`cline_general.md` (provider: cline_cli), `kiro_general.md` (kiro_cli),
`grok_general.md` (grok_cli), `codex_general.md` (codex), `claude_general.md`
(claude_code). So for `cline_cli` / `kiro_cli` / `grok_cli` the f-string
(`cline_cli_general`) is NOT an installed profile — the server fails to load it
and silently re-derives to `claude_code`. It only "worked" for `codex` /
`claude_code` because their raw id happens to equal the stub short form
(`codex_general`, and claude's stem coincided in the codex-only D9 test).

**Fix:** resolve the installed alias stub via
`agent_profiles._find_alias_for_cell(GENERAL_POSITION, provider)` (scans the flat
agent store for a composition stub whose `extends`/`position == general` AND
`provider == provider`, returns its file stem). Bind that stem as the fallback.
When NO stub exists, raise `RoutingError(E-ALIAS-MISSING)` rather than hand an
unresolved name to the server. New stable code `E_ALIAS_MISSING = "E-ALIAS-MISSING"`
added to `routing.py`. The lazy in-function import mirrors the existing
routing→agent_profiles lazy import at routing.py:238 (no import cycle).

### Bug 2 — `_assign_impl` didn't thread the resolved provider to `_create_terminal`

`mcp_server/server.py` `_assign_impl` computes `_resolved_provider` (from
`resolve_assignment_target` / the D9 routing binding) but called `_create_terminal`
WITHOUT it. `_create_terminal` then re-derived the provider via
`resolve_provider(agent_profile, fallback_provider=<supervisor provider>)`, which
falls back to the supervisor's provider (claude_code) whenever the position/alias
`agent_profile` fails to load — the same class of silent claude_code fallback.

**Fix:** add a `provider: Optional[str] = None` parameter to `_create_terminal`.
When supplied (non-empty) it WINS over the per-branch `resolve_provider(...)`
re-derivation in BOTH create branches (existing-session and new-session); when
absent the behaviour is byte-identical to before. Thread
`provider=_resolved_provider` through from the `_assign_impl` `_create_terminal`
call.

- `_resolved_provider` is `resolve_assignment_target(agent_profile, provider)`'s
  second element: for ENGAGED position mode it is the real provider (e.g.
  `cline_cli`); for a NOT-ENGAGED legacy concrete-name assign with no explicit
  `provider=`, it is `None` → `provided_provider is None` → re-derive
  (byte-identical, no regression for legacy names).
- **Fork-row branch (server.py:1786):** its local `provider =
  resolve_provider(agent_profile, fallback_provider=row["provider"])` is used
  ONLY for fork-base compatibility validation (`validate_base_source`,
  `supports_fork_context`, the D10 mismatch degrade) and is NEVER fed to
  `_create_terminal`. No change needed there; the assign spawn provider is
  governed by the threaded `_resolved_provider`.

## Files touched

Production:
- `src/cli_agent_orchestrator/utils/routing.py` — Bug 1 + `E_ALIAS_MISSING`.
- `src/cli_agent_orchestrator/mcp_server/server.py` — Bug 2 (`_create_terminal`
  `provider=` param + both branches honor it; `_assign_impl` threads it).

Tests:
- `test/mcp_server/test_f613_position_alias.py` (NEW, 11 tests).
- `test/mcp_server/test_f497_routing_d9.py` — 2 existing D9 fallback tests updated
  to seed the general alias stub in the flat store (they had relied on the
  `codex` raw==stem coincidence; the fix now requires a real stub).

## Test evidence (box grok-box-004, SHA b1a36627)

Scope run: `test/mcp_server/test_f613_position_alias.py`,
`test/mcp_server/test_f497_routing_d9.py`, `test/utils/test_f497_resolver.py`,
`test/utils/test_f497_composition.py`.
- **F613 (11) + D9 routing (12): PASS.** resolver/composition breadth also run.
- black `--check -l 100`: pass (4 files unchanged). isort: pass.
- **mypy --strict parity** on the two touched source files (stash-free via
  `git show <base>:<file>`): `uv run mypy --strict
  src/cli_agent_orchestrator/utils/routing.py
  src/cli_agent_orchestrator/mcp_server/server.py` →
  HEAD **35** == BASE (`b2814464`) **35**, **delta 0** (routing.py contributes 0;
  all 35 pre-exist in server.py). No new type errors.

### F613 test arms
- Per-provider alias resolution: `cline_cli→cline_general`, `kiro_cli→kiro_general`,
  `grok_cli→grok_general`, `codex→codex_general`, `claude_code→claude_general`
  (parametrized, via `_find_alias_for_cell` with a `CAO_HOME_DIR` temp store +
  seeded stubs); unknown provider → `None`.
- `resolve_routing_binding` non-gate fallback binds `cline_general` (NOT
  `cline_cli_general`); missing stub → `RoutingError` with `.code == E-ALIAS-MISSING`.
- `_assign_impl("secretary", ...)` (position bound to cline_cli) threads
  `provider=cline_cli` into `_create_terminal` (mock captures it).
- `_create_terminal(provider="cline_cli")` with supervisor metadata provider =
  claude_code → HTTP terminal-create params carry `provider=cline_cli`, and
  `resolve_provider` is NOT called (supplied provider wins).
- `_create_terminal(...)` without `provider=` → `resolve_provider` called once,
  byte-identical.

## Mutation ledger (1 per fix, box grok-box-004)

| Mutant | Applied edit | Named selector | Kill |
|---|---|---|---|
| F613-bug1-raw-fstring | general fallback reverts to `f"{provider}_{GENERAL_POSITION}"` | `test_routing_binding_fallback_binds_alias_stub_for_cline` | test_rc=1, pre=0, post_revert=0 |
| F613-bug2-drop-passthrough | `provided_provider = None` (ignore supplied provider) | `test_create_terminal_supplied_provider_wins_and_reaches_http` | test_rc=1, pre=0, post_revert=0 |

## Pre-existing, out-of-scope failure (flagged, not fixed)

`test/utils/test_f497_composition.py::test_ac1_narrowed_extracted_profile_matches_golden_except_body[codex_empirical_reviewer]`
fails with `assert not {'contextPolicy'}`. It compares the LIVE installed
`codex_empirical_reviewer.md` stub (which now carries a `contextPolicy` field)
against a committed golden that predates that field — pure installed-store /
environment drift. `git diff --name-only b2814464 HEAD` shows my change touches
ONLY `routing.py`, `server.py`, and the two test files — nothing in composition,
`agent_profiles`, or the goldens. It fails identically regardless of code SHA and
is unrelated to F613; it is not in the authoritative F613 scope (it was extra
breadth I ran).

## Box-actions ledger

All via `CAO_BOXES=box@grok-box-004 bash scripts/box-run.sh f613-verify -- '<cmd>'`
from `/home/chao/VScode_projects/cli-subagents`; the verify script was delivered
base64-inline and decoded read-only on the box. No raw state-changing ssh. Never
grok-box-1.

| Label | Action | Result |
|---|---|---|
| `f613-verify` | fetch origin cao/f613-position-alias; `git checkout -f b1a36627`; uv sync --frozen; pytest scope; 2 mutants (sed→test→restore→clean); black/isort; stash-free mypy head+base parity | F613+D9 pass; 2/2 mutants killed; fmt clean; mypy 35==35 delta 0; final box status clean |

Box left clean: checkout at `b1a36627`, only the disposable per-worktree `.venv`
from `uv sync --frozen`; mypy base temp dir under `~/box-scratch` removed; no
apt/pip/global installs.


---

## r2 fold — golden drift + rebase onto moved fork main

Supervisor follow-up: fold the previously-flagged composition-golden drift into
this lane and rebase onto the moved fork main before gating.

### Golden drift fix (test_f497_composition[codex_empirical_reviewer])

- **Correction to the r1 note:** the failing test does NOT read the installed
  ``~/.aws`` store. ``test_ac1_narrowed_extracted_profile_matches_golden_except_body``
  compares a COMMITTED golden fixture (``test/utils/f497_golden/<name>.md``)
  against a profile composed from the ROOT repo's ``profiles/`` corpus (copied
  into a tmp agent-store by ``_install_ephemeral_stores``; ``_PROFILES`` is the
  ROOT ``profiles/`` dir, auto-discovered as an ancestor). So there is **no
  live-store-read design smell** — the harness is correctly fixture-vs-source.
- **The drift was legitimate and one field only:** the committed golden's
  ``contextPolicy.extraLeaves`` still carried the stale ``"gpt-unrestricted.md"``
  leaf, which the ROOT ``profiles/`` source of truth has since dropped
  (``profiles/positions/empirical_reviewer.md`` certification evidence records
  "extraLeaves gpt-unrestricted all fixed"). The current ROOT composition yields
  ``contextPolicy = {scope: persona, memoryTypes: [project], memoryNames: [],
  globalClaudeMd: false, extraLeaves: []}``.
- **Fix:** regenerated the golden's ``contextPolicy`` from the CURRENT ROOT
  ``profiles/`` composition — ``extraLeaves: ["gpt-unrestricted.md"]`` →
  ``extraLeaves: []`` in ``test/utils/f497_golden/codex_empirical_reviewer.md``.
  The ``kiro_reviewer`` golden had NO drift and is unchanged. The test's
  comparison logic is untouched. (ROOT ``profiles/`` was read-only; not modified.)

### Rebase onto fork main (slice A + B merged)

- Fork main moved to ``e64684f901edc7a3b2b98982e976544c41918798`` ("Merge
  'cao/f582-sliceb' into main"), which descends from this lane's original base
  ``b2814464``.
- ``git rebase e64684f9`` replayed the three F613 commits with **no conflicts**
  (the F613 diff touches ``routing.py`` / ``server.py`` / F497 tests + one golden;
  the F582 merges touched status_monitor / inbox / providers — disjoint).
- Post-rebase the F613 fixes are intact (``_find_alias_for_cell`` /
  ``E_ALIAS_MISSING`` in routing.py; ``provided_provider`` /
  ``provider=_resolved_provider`` in server.py; golden ``extraLeaves: []``); the
  diff vs ``e64684f9`` is exactly the six F613 files.
- New branch HEAD: **``bc18bf541715b19896028b8ccd9fd64ce04d8118``** (force-pushed
  with ``--force-with-lease``).

### Fresh box evidence (grok-box-005, SHA bc18bf54, base e64684f9)

- ``pytest`` scope ``test/mcp_server/test_f613_position_alias.py
  test/mcp_server/test_f497_routing_d9.py test/utils/test_f497_composition.py
  test/utils/test_f497_resolver.py`` → **67 passed, 15 skipped**. The
  previously-drifting selector
  ``test_ac1_narrowed_extracted_profile_matches_golden_except_body`` → **2 passed**.
- Both F613 mutants **re-confirmed KILLED** post-rebase (F613-bug1-raw-fstring,
  F613-bug2-drop-passthrough).
- black / isort ``-l 100``: clean.
- ``mypy --strict`` parity vs ``e64684f9`` on the two touched source files:
  HEAD **35** == BASE **35**, **delta 0**.
- Box repo left clean at ``bc18bf54``; mypy base temp dir removed; no installs.
  (Box selected in the grok-box-2..8 range; never grok-box-1.)
