# F642 deferred follow-up leg (#498) — build report

**Parent SHA (A/B base):** b48e7b92ae77a5087fa4b055c016f27225b18d0a (fork main; branch `cao/f868fe8d`)
**Base subject:** `Merge 'cao/50b71a7b' into main (F244 gated)` — includes F642 core (`b48e7b92`) and F643b (`e3eaa1a4`) in history.
**Tip SHA:** _(this commit — filled in the callback)_
**Scope:** the F642 §7 deferred follow-up leg — the cross-repo coordination the blueprint named as remaining after F642 core: (1) the fork-side `cao messages list --claim <carrier>` CLI flag; (2) the paired ROOT-repo hook edit that uses it.

> Report format: written in the accepted F643/F643b structure. If the gate brief names a different canonical format, I will reformat — content maps 1:1.

---

## 0. What F642 core already shipped (verified in the synced tree at b48e7b92)

- `delivery_ledger`, `delivery_emission`, `condition_ledger` tables + `_migrate_f642_delivery_ledger()` (`clients/database.py`).
- `Carrier` enum (`clients/delivery_ledger.py`): `native | doorbell | hook | replay`.
- The server-side claim primitive **`hook_claim_ids(db, candidate_ids)`** (`clients/database.py`): given the ids the drain hook would print, returns ONLY the ids the hook won (no prior emission by a non-hook carrier), inserting a `hook` emission per winner in the same call. Its storage-layer behaviour is already covered by `test/clients/test_f642_delivery_ledger.py::test_ac18_*`.

The blueprint (§7) states the remainder explicitly: *"the flag is a fork-side CLI addition and the hook is a ROOT-repo file, so the two land together."* That is this build.

## 1. Fork change — the `--claim` flag wired end to end

Three touched files (`cao/f868fe8d`):

- **`services/mailbox_service.py`** — `list_messages(...)` gains `claim: str | None = None`. After the page is built, when `claim` is set it calls `hook_claim_ids(db, candidate_ids=[listed ids])`, commits, and **filters `items` to only the ids this carrier won** (§7/D3: "returns only the ids it won"). Opt-in only: without `claim` the read performs NO mutation (GET semantics preserved). Only `hook` is accepted — the read-as-claim path the spine defines; a non-hook claim raises `MailboxDomainError("unsupported_claim_carrier")` (other carriers claim on their own emit path, not through this read).
- **`api/main.py`** — `GET /messages` gains a `claim` query param, passed through to `list_messages`. Because `--claim` MUTATES (inserts an emission), the endpoint requires **write/admin scope** when `claim` is set (a read-scoped token may list but not claim → 403 `claim_requires_write`).
- **`cli/commands/messages.py`** — `cao messages list` gains `--claim` (`click.Choice(["hook"])`), forwarded as the `claim` request param; omitted entirely when not passed.

## 2. Root-repo change — the hook uses the flag (branch `quirks-merge-train`)

`.claude/hooks/supervisor-inbox-drain.sh` — its pending-list fetch changes by exactly one flag:

```
- PENDING_JSON=$(cao messages list --to me --status pending 2>/dev/null) || exit 0
+ PENDING_JSON=$(cao messages list --to me --status pending --claim hook 2>/dev/null) || exit 0
```
(plus a comment block explaining the read-as-claim.) The hook now prints nothing for an id another carrier already carried (AC18 / #488's first complaint) while still surfacing and acking the ids it owns. `bash -n` clean.

**NOTE — root-repo commit is BLOCKED (flagged to supervisor):** the file edit is applied and syntax-valid on `quirks-merge-train`, but the `fx121` worktree-containment hook refuses any git add/commit whose target or cwd is the root repo (`git add: target outside worktree`), including `git -C`. I did not bypass it. The commit awaits either a supervisor-run commit or an authorized path.

## 3. Tests (fork) — the deferred-leg layer above `hook_claim_ids`

New `test/services/test_f642_list_claim_flag.py` (8 tests):

Service integration (`list_messages(..., claim="hook")`):
- `test_list_claim_hook_filters_natively_claimed_id` — AC18 at the service layer: an id already claimed natively is omitted; an unclaimed id is won and returned.
- `test_list_claim_hook_wins_and_persists_claim` — the won id returns once; a second `--claim hook` read returns nothing (claim is durable, not a re-print).
- `test_list_without_claim_is_pure_read` — no `--claim` → listing unchanged AND zero hook emission rows created (GET semantics).
- `test_list_claim_unsupported_carrier_rejected` — a non-hook claim raises `MailboxDomainError`.
- `test_list_claim_mutant_no_filter_reprints_claimed` — **MUTANT**: make the service NOT filter to won ids → the natively-claimed id reappears in the hook listing, reproducing #488's duplicate print. Proves the filter is load-bearing.

CLI flag (`cao messages list --claim`):
- `test_cli_list_claim_forwards_param` — `--claim hook` forwards `claim=hook` to `GET /messages`.
- `test_cli_list_claim_rejects_unknown_carrier` — an unknown value is a click usage error before any request.
- `test_cli_list_without_claim_omits_param` — no `--claim` → no `claim` key in params (opt-in).

## 4. Verification (uv run pytest / mypy, in-worktree)

| Check | Result |
|---|---|
| `test_f642_list_claim_flag.py` (new) | **8 passed** |
| `test/cli/commands/test_messages.py` + `test/clients/test_f642_delivery_ledger.py` | passed (129 passed alongside neighbors) |
| `bash -n supervisor-inbox-drain.sh` | OK |
| mypy --strict on touched files | see below |

**mypy --strict (touched files):** the acceptance is "clean on touched files." The three touched files (`mailbox_service.py`, `api/main.py`, `cli/commands/messages.py`) carry PRE-EXISTING `--strict` errors in code I did not touch (generic `Dict`/`list` params, SQLAlchemy `Column[str]` assignment, an un-annotated click group). I proved my change introduces **ZERO new errors**: stashing my edits and re-running `mypy --strict` yields the SAME error set with line numbers shifted only by my added lines (baseline 10 in mailbox+messages, 133 in main.py; identical after my change modulo line shift). My added lines (the `claim` param, the claim block, the scope guard, the `--claim` option) produce no mypy error. Fixing the pre-existing debt is out of scope and not attempted.

## 5. Not touched (per instruction)
- `delivery_ledger`/`delivery_emission`/`condition_ledger` **schema** — unchanged (I consume `hook_claim_ids`, add no column, alter no table).
- `providers/condition.py` — untouched.

## Files changed
Fork (`cao/f868fe8d`):
- `src/cli_agent_orchestrator/services/mailbox_service.py` (+~24)
- `src/cli_agent_orchestrator/api/main.py` (+~14)
- `src/cli_agent_orchestrator/cli/commands/messages.py` (+~16)
- `test/services/test_f642_list_claim_flag.py` (new, 8 tests)
- `f642-followup-build-report.md` (this file)

Root repo (`quirks-merge-train`, edit applied, commit blocked):
- `.claude/hooks/supervisor-inbox-drain.sh` (+6/−1)
