# F-seed-trust-subdir — auto_responder SEED_RULES mirror (fork lane cao/seed-trust-subdir @ 8be51f9f)

Scope: `src/cli_agent_orchestrator/services/auto_responder.py` SEED_RULES["codex.yaml"] only (+7/-1).
1. New rule `codex-trust-dir-subdir` (contains "subdirectory of a Git project. Trusting will apply to the repository root", options ["Press enter to continue"], answer ["Enter"]) — mirrors the live ~/.aws/cli-agent-orchestrator/auto-answers/codex.yaml rule (user 2026-08-29: auto-answer this variant).
2. `codex-usage-resets` seed answer `["Enter"]` -> `wait`. Standing rule: never auto-spend usage-limit resets; the live yaml already says `wait`, the seed did not (fresh installs would auto-spend).

ACs
- AC1 `yaml.safe_load(SEED_RULES["codex.yaml"])` -> 4 rules, names [codex-usage-resets, codex-trust-dir, codex-trust-dir-subdir, codex-resume-working-directory]; rule 0 `is_wait` True after loading through the fork's Rule parser (not raw yaml).
- AC2 `make test-quick` green (TCACHE_BIN=/home/chao/VScode_projects/cli-subagents/scripts/tcache) incl. test/auto_answers.
- AC3 mutation: flip the seed's subdir `question` to a string absent from the dialog -> a test in test/auto_answers (or a new targeted one) must fail; if none does, report the gap (do not edit).
- AC4 seeding path: with a temp HOME/empty rules dir, `_rules_path("codex")` seeds a file whose parsed rules include codex-trust-dir-subdir and usage-resets is_wait.
