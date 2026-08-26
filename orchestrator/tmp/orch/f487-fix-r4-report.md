# F487/F475 Fix — Round 4 Build Report

**Code-SHA:** `7787ed344e92df412c515ee9d2a7ae9874da5341`  
**Base-SHA:** `8c994302c540f8ddc03534e5edcf534dfe076ac1`  
**Report-commit:** `(this commit — see git log)`  
**Prior-round authority:** `a30e36cabae5039231c1d4939231312a441bd25d`  
**Date:** 2026-08-26

---

## S1 — Verdict diagnosis alignment (FIXED)

**Problem:** `test/quarantine-verdicts.md:182-188` still recorded the repudiated
"(c) global state corruption" diagnosis for `test_stale_entry_not_killed`,
claiming concurrent xdist workers share Python globals. Workers are separate
processes; globals cannot be shared.

**Fix:** Updated verdict to `(b) xdist resource contention` with corrected root
cause: "Process-timing sensitive: sentinel subprocess + /proc starttime read
races under xdist load." — aligned with `quarantine.toml:143-150`.

**File:** `test/quarantine-verdicts.md`

---

## N1 — Report SHA hygiene (FIXED)

**Problem:** r3 report truncated Code-SHA to `7787ed3428` (incorrect — real
prefix is `7787ed344e92`), and left the report-commit as a placeholder.

**Fix:** This report uses the full 40-char Code-SHA above and identifies the
prior-round authority commit explicitly. The report-commit is this commit itself
(deterministic once committed).

---

## No code changes — no suite re-run required

This round is metadata-only (verdict text + report). The production code and
test logic are byte-identical to r3 Code-SHA `7787ed344e92`. The r3 clean-worktree
suite result remains authoritative:

```
2 failed, 13640 passed, 214 skipped, 15 xfailed in 333.01s
```

(2 failures: pre-existing xdist timing flakes, pass serial on base.)
