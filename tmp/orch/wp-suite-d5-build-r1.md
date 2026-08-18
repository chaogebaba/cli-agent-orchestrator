# WP-SUITE D5 — Build Report r1

Wall: **D5 — quarantine ledger and burn-down**
Branch: `cao/566bb65a`
Base: `ce48138b` (D1/D2/D4 mainline)

---

## AC5.1 — F262 ancestry check (precondition)

```
$ git merge-base --is-ancestor 6448778e HEAD && echo "F262 is ancestor"
F262 is ancestor
```

Registry at entry: 7 `serial_only` (6 from F262 + 1 re-quarantined by D2) + 2 `known_red` = 9 total.
Post-burn-down state confirmed: `serial_only` class present in `_VALID_CLASSES`, entries have verdicts.

**Status: PASS** — precondition satisfied, build proceeds.

---

## AC5.2 — serial_only registered as pytest marker

Added to `pyproject.toml` markers list:
```
"serial_only: quarantined test that must run serial (xdist_group quarantine-serial)",
```

Already present in `test/plugins/quarantine.py:42`:
```python
_VALID_CLASSES = frozenset({"xdist_flaky", "worker_crash", "known_red", "serial_only"})
```

Note: `-m serial_only` selects 0 items by design — the quarantine plugin applies
`pytest.mark.xdist_group("quarantine-serial")`, never `pytest.mark.serial_only`.
The marker registration satisfies `--strict-markers` (so the class name can appear
in test code or CLI without a collection error), but selection is via the quarantine
plugin's `modifyitems` hook, not marker filtering.

Verification (marker registered, collection does not error):
```
$ uv run pytest --collect-only -q -n 0 -m serial_only 2>&1 | tail -3
no tests collected (11711 deselected) in 10.26s
```

**Status: PASS** — marker registered, --strict-markers passes, 0 collected is correct.

---

## AC5.3 — burn-down criteria + expiry enforcement

### Format changes to quarantine.toml

All 9 entries now carry required fields:
- `class` — one of {serial_only, known_red}
- `reason` — root-cause description
- `filed` — 2026-08-18 (UTC date entry added)
- `review_by` — 2026-09-18 (filed + 30 days)

Header documents the mechanical criteria.

### Expiry enforcement: `test/plugins/quarantine_expiry.py`

New plugin registered in `test/conftest.py`, activates only under
`CAO_TEST_TIER_BUDGET=enforce` (the `make test-hygiene` env). At `pytest_configure`,
parses `test/quarantine.toml` and calls `pytest.exit(msg, returncode=1)` if any
entry's `review_by` is in the past or any entry lacks `filed`/`review_by`.

Positive test (current dates, not expired):
```
$ CAO_TEST_TIER_BUDGET=enforce uv run pytest --collect-only -q -n 0 -m smoke
16/11711 tests collected (11695 deselected) in 24.68s
```

Negative test (manually set review_by=2025-01-01):
```
$ CAO_TEST_TIER_BUDGET=enforce uv run pytest --collect-only -q -n 0 -m smoke
Exit:

QUARANTINE EXPIRY VIOLATION (D5 AC5.3)
The following quarantine entries have passed their review_by date.
Each must be resolved: fix the test (remove entry), re-file with a
new review_by (document progress in reason), or escalate.

  test/telemetry/test_spans.py::...test_emits_invoke_agent_with_required_attributes: review_by=2025-01-01 (expired 594d ago)

EXIT_CODE=1
```

**Status: PASS** — enforcement fires on expired entries, silent on non-expired.

---

## AC5.4 — root causes documented

6 original `serial_only` entries (from F262 burn-down):
- 4 × `test/telemetry/test_spans.py` — process-global OTel TracerProvider un-isolatable
  - Burn-down: session-scoped TracerProvider reset fixture
- 2 × `test/security/test_auth.py` — cross-worker monkeypatch of module-level auth constants
  - Burn-down: per-worker auth-constant isolation fixture

Both root causes and burn-down paths are documented in toml section comments.

**Status: PASS** — root causes named, burn-down criterion is "fix the fixture".

---

## AC5.5 — gw crashes NOT quarantined

```
$ grep -c "worker_crash" test/quarantine.toml
0
```

The 3 unexplained xdist gw crashes from F262's tier run have no node ids and are
correctly excluded. They are recorded as a D6 investigation item (D6a trace
retention is the instrument).

**Status: PASS** — no spurious quarantine entries.

---

## Targeted test results

```
$ uv run pytest test/telemetry/test_spans.py test/security/test_auth.py -n 2 -q --timeout 60
............................................  [100%]
44 passed in 1.99s

$ uv run pytest test/plugins/test_registry.py -x -q -n 0
.........  [100%]
9 passed in 0.16s
```

---

## Diff stat

```
 pyproject.toml                      |   1 +
 test/conftest.py                    |   1 +
 test/plugins/quarantine_expiry.py   |  84 +++ (new file)
 test/quarantine.toml                | 143 ++++++++++++++++++++++------
 4 files changed, ~175 insertions(+), 56 deletions(-)
```

---

## Summary

All D5 ACs delivered:
- AC5.1: F262 ancestry confirmed
- AC5.2: `serial_only` marker registered in pyproject.toml
- AC5.3: Ledger format enforced (filed/review_by mandatory), expiry plugin fails `test-hygiene`
- AC5.4: Root causes documented with burn-down criteria
- AC5.5: gw crashes correctly excluded
