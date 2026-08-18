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


.PHONY: test-smoke test-full test-quick test-ci test-live test-hygiene test-census test-tiers

# --- F237: default-cached fork pytest via tcache ---
# Resolve tcache from the fork's git common dir (F237 D4).
# Layout coupling: fork is one level inside root repo. Override with env for
# non-standard layouts. Existence guard below fires at parse time.
TCACHE_BIN ?= $(shell git rev-parse --git-common-dir)/../../scripts/tcache
$(if $(wildcard $(TCACHE_BIN)),,$(error F237: tcache not found at $(TCACHE_BIN). Set TCACHE_BIN env to override.))

PYTEST_WRAPPER := $(CURDIR)/scripts/run-pytest.sh

# F237 D3: content-hash interpreter verification for cross-worktree HIT
export TCACHE_INTERP_CHECK := content

ARGS ?=

# F254 D29: smoke routed through the fence (flock) like its siblings.
# Not cached (a 5-second suite gains nothing from a content-addressed cache).
test-smoke:
	"$(PYTEST_WRAPPER)" -m smoke $(ARGS)

# Full suite (all markers, overrides addopts -m filter). Cached by default (F237 D6).
# Opt-out: make test-full TCACHE=off
test-full:
	"$(TCACHE_BIN)" run "$(PYTEST_WRAPPER)" -m "" $(ARGS)

# Quick suite — default addopts unchanged. Cached by default (F237 D6).
# Opt-out: make test-quick TCACHE=off
# F279: belt-and-braces live exclusion (live tests are env-gated individually,
# but -m "not live" gives CI parity with test-ci).
test-quick:
	"$(TCACHE_BIN)" run "$(PYTEST_WRAPPER)" -m "not live" $(ARGS)

# F254 D29/D30: CI target — identical to test-full marker expression.
# CI passes extra flags (coverage, ignores) via ARGS=.
test-ci:
	"$(TCACHE_BIN)" run "$(PYTEST_WRAPPER)" -m "not live and not e2e" $(ARGS)

# F254 D30: opt-in live/e2e tier (never in a gate).
test-live:
	"$(PYTEST_WRAPPER)" -m "live or e2e" --run-live $(ARGS)

# F254 D30: hygiene run — serial, budgets enforced.
test-hygiene:
	CAO_TEST_TIER_BUDGET=enforce "$(PYTEST_WRAPPER)" -n 0 -m "" $(ARGS)

# F259: per-test resource census. Never cached (D12 forces a MISS); routed
# through the fence so profiling numbers are taken under the same CPUWeight /
# MemoryHigh / nice envelope as every other suite run.
test-census:
	CAO_TEST_CENSUS=1 "$(PYTEST_WRAPPER)" -m "" $(ARGS)

# F254 D17: tier census — collect-only, writes test/tier-census.json.
test-tiers:
	uv run pytest --collect-only -q -n 0 --tier-report=test/tier-census.json $(ARGS)
