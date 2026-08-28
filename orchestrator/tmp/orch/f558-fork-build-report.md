# F558 #413 (part 2) — fork build report

Branch: `cao/5d42be80`  ·  tip: `10b69b84372a71cf6f0e5e106a0b29ea0125c647`

## Root cause (3 lines)
1. `_scan_directory` (utils/agent_profiles.py) recorded **every** agent-store subdirectory as a profile — even one without `agent.md`, as a listable-but-unloadable entry.
2. A fresh `./install.sh` puts the F497 composition-input dirs `positions/` and `overlays/` as siblings **inside** the agent store (constants.py: `POSITIONS_STORE_DIR`/`OVERLAYS_STORE_DIR = LOCAL_AGENT_STORE_DIR / …`), so `list_agent_profiles()` returned bogus `positions`/`overlays` profiles.
3. The session brief's `profiles` section (`session_manifest_service.build_session_manifest` → `_charter_projection(name)` → `read_agent_profile_source`) then raised `FileNotFoundError` for those names → section marked `error` → `terminal_service.py:2616` raised `ValueError: required session brief core section failed: profiles` → every `claude_code` launch died on a fresh install. (The laptop was masked because its store predates F497.)

## Fix (contract item 1)
`_scan_directory`: a subdirectory **without** `agent.md` is not a profile and is skipped entirely (`continue`). This is a **structural** rule ("only a top-level `<name>.md` regular file, or a subdir containing `<name>/agent.md`, is a profile"), **not a name blacklist** — any stray directory (not just `positions`/`overlays`) is excluded by the same rule. Top-level `.md` files and directory-style (`<name>/agent.md`) profiles are unchanged; the local-store `dir_profiles_loadable=False` path is preserved.

## Bug-family sweep (contract item 3)
Grepped every `iterdir`/`listdir`/`glob`/`rglob` over the agent-store dir in `src/`:

- **`utils/agent_profiles.py:158` `_scan_directory` `directory.iterdir()`** — THE bug site. Fixed.
- **`utils/agent_profiles.py:252` built-in store scan** (`agent_store.iterdir()`) — already filters `if name.endswith(".md")`; a bare subdir can never surface. Structurally correct, no change.
- **`cli/commands/config_reconcile.py:101` `_built_in_profile_names`** (`store.iterdir()`) — already `if item.name.endswith(".md")`. No change.
- **`cli/commands/config_reconcile.py` `_profile_locations` / `_profile_directories`** — resolve by name (`directory / f"{name}.md"`, `directory / name / "agent.md"`); no directory iteration, cannot invent a profile. No change.
- **`services/profile_store.py`** — name→path resolution only (`_PROFILE_NAME_RE`), no iteration. No change.
- All other `iterdir`/`glob` hits (`fork_context_service`, `memory_reconciliation`, `persona_context`, `providers/codex`, `memory_service`) iterate codex/memory/proc dirs, **not** the agent store. Out of family.

Conclusion: the only site that could surface a bare directory as a profile was `_scan_directory`; every other agent-store enumeration already applies the equivalent `.md` filter.

## Tests (contract item 2)
New class `TestF497CompositionStoresNotProfiles` in `test/utils/test_agent_profiles_coverage.py`:
- `test_positions_overlays_stray_excluded_only_real_profiles` — store with `positions/`, `overlays/`, a stray non-`.md` file, and 2 real profiles → `list_agent_profiles()` returns **exactly** `["developer","reviewer"]`; `positions`/`overlays` absent. (mutation note inline: revert the `continue` guard → names gain positions/overlays → fails.)
- `test_brief_profiles_section_builds_over_fresh_install_store` — **brief regression**: drives the exact `profiles()` loop (`_charter_projection(name)` for each `list_agent_profiles()` name) over the fresh-install store; no `FileNotFoundError`, yields the 2 real rows. (mutation note inline: revert guard → `_charter_projection("positions")` raises FileNotFoundError → fails, exactly as the fresh-install launch failed.)

Updated three tests that encoded the OLD listed-but-unloadable-bare-dir behavior (that behavior WAS the bug):
- `test_agent_profiles_coverage.py::test_scan_subdirectory_without_agent_md` → now asserts the bare dir is excluded (`"bare-agent" not in profiles`; `profiles == {}`). Mutation note inline.
- `test_profile_search.py::test_directory_without_agent_md_marked_unloadable` → renamed `…_excluded`; asserts not listed at all.
- `test_profile_search.py::test_broken_yaml_on_disk_listed_but_not_searchable` → dropped `empty-monitor` (bare dir) from the expected set; the two real unloadable `.md` files stay listed.

## Gates (contract item 4)
- Targeted tests: `CI=1 uv run pytest -p no:suite_slot` over `test_agent_profiles.py`, `test_agent_profiles_coverage.py`, `test_profile_search.py`, `test_disabled_agent_dirs.py` → **123 passed**. (No laptop suite run; CI=1 + `-p no:suite_slot` scoped to touched-area files, as sanctioned.)
- `black` + `isort`: applied, clean.
- `mypy` (repo `mypy.ini` config), touched source file: my changed region (`_scan_directory`) introduces **0** new errors. 2 pre-existing errors remain at `agent_profiles.py:747-748` (`Name "Optional" is not defined`) in the F497 `resolve_assignment_target`/`_position_exists` code — verified pre-existing (base reports the same at line 732; `black` re-wrapped one pre-existing over-long line so the 2nd `Optional` usage moved to its own line, making mypy count 2 vs 1). **Out of scope** (unrelated latent bug in F497 code); flagged here, not fixed, per scope discipline.
- `mypy --strict` bare (no config) reports additional generic-`dict`/`Dict` `type-arg` findings — all pre-existing, module-wide, none in my region; the repo gates via `mypy.ini`, not bare `--strict`.
- Test-file mypy findings are all pre-existing `import-untyped` (no `py.typed` marker) noise.

## Diff --stat
```
 src/cli_agent_orchestrator/utils/agent_profiles.py |  36 +++++--
 test/services/test_profile_search.py               |  24 +++--
 test/utils/test_agent_profiles_coverage.py         | 117 ++++++++++++++++++++-
 3 files changed, 155 insertions(+), 22 deletions(-)
```

Counts: 3 files changed, +155 / −22. Target-area tests: 123 passed (2 new + 3 updated).
