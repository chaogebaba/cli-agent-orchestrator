#!/usr/bin/env make -f
# CLI Agent Orchestrator — maintenance targets.
#
# Offline vendoring of the upstream MCP Apps builder skills
# (modelcontextprotocol/ext-apps). See skills/vendor/ext-apps/README.md.

.PHONY: refresh-ext-apps-skills check-ext-apps-skills

# Re-vendor the ext-apps builder skills from the pinned tag and rewrite NOTICE.
# To move to a newer upstream release, bump PINNED_REF/PINNED_SHA in
# scripts/vendor_ext_apps_skills.py first, then run this target.
refresh-ext-apps-skills:
	uv run python scripts/vendor_ext_apps_skills.py

# Verify the on-disk vendored copy still matches the pin (CI / pre-commit).
# Exit 0 = in sync, 1 = drift, 2 = network-gated (could not verify).
check-ext-apps-skills:
	uv run python scripts/vendor_ext_apps_skills.py --check


.PHONY: test-smoke test-full test-quick

# Lockfile for serializing full-suite runs across lanes (F169).
SUITE_LOCK := /tmp/cao-suite.lock

# Internal recipe: acquire exclusive flock, then run pytest under resource fence.
# $(1) = extra pytest args appended after the default addopts.
define _fenced_pytest
	@( \
	  echo "acquiring suite lock ($(SUITE_LOCK))..."; \
	  exec 9>"$(SUITE_LOCK)"; \
	  if ! flock -n 9 2>/dev/null; then \
	    echo "waiting for suite lock (another suite is running)..."; \
	    flock 9; \
	  fi; \
	  echo "lock acquired — running pytest"; \
	  if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope true >/dev/null 2>&1; then \
	    echo "[fence] systemd-run --user --scope -p CPUWeight=30 -p MemoryHigh=70% nice -n 10"; \
	    systemd-run --user --scope -p CPUWeight=30 -p MemoryHigh=70% nice -n 10 \
	      uv run pytest $(1); \
	  else \
	    echo "[fence] WARNING: systemd-run unavailable — falling back to nice -n 10 (no memory cap)"; \
	    nice -n 10 uv run pytest $(1); \
	  fi \
	)
endef

test-smoke:
	uv run pytest -m smoke

# Full suite (all markers, overrides addopts -m filter). Fenced + locked.
# Extra args: make test-full ARGS="-m 'not e2e'"
ARGS ?=
test-full:
	$(call _fenced_pytest,-m "" $(ARGS))

# Quick suite — default addopts unchanged (same as bare `uv run pytest`). Fenced + locked.
# Exists so briefs have one vocabulary for the default-filtered run.
test-quick:
	$(call _fenced_pytest,$(ARGS))
