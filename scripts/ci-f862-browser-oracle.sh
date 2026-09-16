#!/usr/bin/env bash
# F862 Amendment D — the CI arm for the origin-receipt oracle (B3).
#
# The oracle is the load-bearing evidence for AC-25/AC-35: it is the only thing
# that can tell an origin receipt from a local fulfil, and D11's build stop is
# defined in its terms. It is e2e+slow, so the merge selection deselects it, and
# it skips when Chromium is absent. Together that meant a green merge gate could
# contain zero executions of the oracle.
#
# This script is the arm that closes that: it installs the browser, selects the
# oracle explicitly, and sets CAO_F862_REQUIRE_BROWSER so a run that STILL cannot
# reach a browser FAILS instead of exiting 0 with every arm skipped.
#
# Run it from the fork root, on a box (it compiles nothing but does download a
# Chromium build and run a real browser).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== installing the pinned Playwright Chromium =="
uv run playwright install chromium

echo "== running the origin-receipt oracle as a VERDICT =="
CAO_F862_REQUIRE_BROWSER=1 \
CAO_F862_EVIDENCE="${CAO_F862_EVIDENCE:-}" \
  uv run pytest -p no:randomly -n 0 -q --tb=short -m browser_oracle "$@"
