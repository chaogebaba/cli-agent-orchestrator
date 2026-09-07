# F786 #643 stage 1 (fork) — mutant ledger

NOTE: the lane spec named `/data/claude-scratch/worker-scratch/f786-fork/` for
this ledger, but the F452 worktree-containment PreToolUse hook (fx121) refuses
any write outside my worktree, so the ledger lives here in the worktree instead.

Each mutant is a single-line change to production code that makes the named AC
test fail (kill). >=8 required (blueprint §4). Verification method: git-clean
tree, apply one mutation, run the killing test (expect FAIL), revert (expect PASS).

| # | Mutant (production edit) | File:site | Killed by (test nodeid) |
|---|--------------------------|-----------|-------------------------|
| 1 | synthesis reverts to `f"{provider}_{position}"` | agent_profiles.`_synthesise_position_profile_name` | test_f786_position_only_roles.py::TestAC1aEffectiveName::test_synthesis_never_reverts_to_provider_underscore_position |
| 2 | D3 refusal disabled (`return None`) | routing_guard.`retired_profile_refusal` | test_f786_position_only_roles.py::TestAC2RetiredProfiles::test_every_retired_name_is_refused_naming_position |
| 3 | refusal fires for a flat non-table name (drop membership guard) | routing_guard.`retired_profile_refusal` | test_f786_position_only_roles.py::TestAC2RetiredProfiles::test_negative_arm_non_retired_names_pass_through |
| 4 | legacy providers.toml key silently ignored (no-op the checker) | settings_service.`_reject_legacy_profile_keys` | test_f786_position_only_roles.py::TestAC5ProvidersTomlPositionKeyed::test_legacy_key_raises_at_load |
| 5 | position field carries the whole effective name (return agent_profile when col set) | fleet_service.`_fleet_position` | test_f786_position_only_roles.py::TestAC6FleetPosition::test_column_wins_even_when_name_tampered |
| 6 | resume guesses a lane instead of row provider + RETIRED_PROFILES (drop unmapped→refuse) | agent_profiles.`resolve_resume_effective_name` | test_f786_position_only_roles.py::TestAC9ResumeResolution::test_unmapped_retired_name_refuses |
| 7 | fallback scans the flat store instead of composing general-<provider> | routing.`resolve_routing_binding` (step 3 fallback) | test_f613_position_alias.py::test_general_fallback_derives_general_hyphen_provider |
| 8 | missing `[required]` row treated as empty (`.get(position, [])`) | routing.`resolve_routing_binding` (step 2) | test_f497_routing_d9.py::test_d12b_missing_required_row_fails_closed |
| 9 | profile-less fallback restored at terminal_service (`if agent_profile:` → `if False:`, `profile=None` instead of raise) | terminal_service.create_terminal (load block, :2093) | test_f786_composed_and_routing.py::TestAC8ComposedStore::test_missing_composed_name_fails_closed (r2: rewritten to DRIVE create_terminal via the test seam and assert ProfileMissingError/E-PROFILE-MISSING with no window/DB row — the r1 version only asserted load_agent_profile raised FileNotFoundError and never entered create_terminal, so #9 survived, EMPIRICAL gate r1 B3) |
| 10 | composed write skipped at the server seam (remove writer call) | server.py `_assign_impl` D8 seam | test_f786_composed_and_routing.py::TestAC8ComposedStore (fail-closed loader → E-PROFILE-MISSING) |
| 11 | D2c reverts to bare position (`spawn_profile=position`) | routing.`resolve_routing_binding` (step 3 certified) | test_f613_position_alias.py::test_assign_impl_threads_resolved_provider_to_create_terminal |
