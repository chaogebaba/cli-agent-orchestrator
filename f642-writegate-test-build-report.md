# F642 follow-up — write-gate test coverage (gate SHOULD) — build report

**Parent SHA (A/B base):** e04b50e5ed17e0177863dc5b9a6306e6c1746450 (fork main; branch `cao/f868fe8d`; F642 follow-up `1445e387` and root hook `091482fa` already merged/live)
**Tip SHA:** _(this commit — filled in the callback)_
**Tier:** targeted evidence (small, test-only, non-core diff — no box A/B).

> Format: accepted F643/F643b/F642 structure; reformat on request.

## 0. The gap the gate flagged

The empirical gate returned GATE-YES with one SHOULD: the `403 claim_requires_write` write-gate added in `api/main.py` (F642 follow-up `1445e387`) had **zero test coverage** — a reviewer disabled it in prod and the whole suite stayed green. `--claim` mutates (it inserts a `delivery_emission` claim), so the `GET /messages` endpoint requires write/admin scope when `claim` is set; without coverage that guard could silently rot.

## 1. Change — test only, no source diff

`git diff --stat` against e04b50e5 shows **no source change**: the write-gate already ships in prod; this build only binds it with tests. New file:

`test/api/test_f642_claim_write_gate.py` (5 tests), calling `list_messages_endpoint` directly with an explicit `_scopes` list and the service (`mailbox_service.list_messages`) stubbed — so each assertion is precisely "does the scope gate admit/deny", not DB behaviour:

- `test_read_scope_with_claim_is_403_and_does_not_claim` — READ scope + `claim=hook` → `HTTPException(403, code='claim_requires_write')`, and the service is **never called** (no mutation leaks past the gate).
- `test_write_scope_with_claim_passes_gate_and_claims` — WRITE scope + `claim` → gate admits; endpoint calls `list_messages(claim='hook')`.
- `test_admin_scope_with_claim_passes_gate` — ADMIN likewise admits.
- `test_read_scope_without_claim_is_unaffected` — a plain read (no `claim`) under READ scope is untouched by the gate and passes no `claim` kwarg.
- `test_mutant_gate_removed_lets_read_scope_claim` — asserts the LIVE gate blocks a READ+claim (the service is never reached), the inverse of the reviewer's disabled-gate build.

## 2. Mutant evidence (the reviewer's exact prod-disable)

Disabling the gate in `api/main.py` (`if False and SCOPE_WRITE not in _scopes …`) — the same shape the reviewer used — flips **two** tests to failure:
```
FAILED test_read_scope_with_claim_is_403_and_does_not_claim   (DID NOT RAISE HTTPException)
FAILED test_mutant_gate_removed_lets_read_scope_claim
3 passed, 2 failed
```
So a regression that drops the write-gate is now caught. Gate restored → all green.

## 3. Verification (uv run pytest, in-worktree)

| Suite | Result |
|---|---|
| `test_f642_claim_write_gate.py` (new) | **5 passed** |
| + `test_f642_list_claim_flag.py` + `test_messages.py` + `test_f642_delivery_ledger.py` | **47 passed** total |
| Mutant (gate disabled) | 2 tests fail → restored → green |
| Source diff | none (test-only) |

## Files changed
- `test/api/test_f642_claim_write_gate.py` (new, 5 tests)
- `f642-writegate-test-build-report.md` (this file)
