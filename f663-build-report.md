# F663 (#518) build report — ROUND 2 (gate r1 GATE-NO repairs)

**Repo:** fork `cli-agent-orchestrator` (nested, own `.git`).
**Branch:** `cao/c97e89aa` (pushed to `origin`).
**HEAD:** `c7e50eb91df13bf2f5cfd91124fead300ce7d23f`
**Parent (r1 HEAD, the reviewed commit):** `1fc0df3510241a13e892b231004b9e8fe9345fa4`
**Merge-base / fork main:** `5d39ec7fd67803b82f685e8a95dfb41130d34340`
**Prior artifacts:** build r1 `/data/cao-scratch/f663-build-report.md`
(sha256 `a1eadaf830191499e5eeff8be892ad7f6ec8c719b9f176b7c6b8b7eef0520210`);
gate r1 `/data/cao-scratch/f663-gate-report-r1.md`.

Both gate findings are **accepted as correct** — neither was a misread, and
neither repair widens the frozen r1 contract (see "Conflict check" below).
Both are fixed in one commit on top of the reviewed HEAD.

---

## Housekeeping: the r1 worktree had been reaped

`/data/cao-scratch/worktrees/cli-agent-orchestrator/c97e89aa` was empty at the
start of this round (directory present, worktree gone). Nothing was lost: the
branch was intact on `origin`. Recreated with

```
git -C .../cli-agent-orchestrator worktree add -B cao/c97e89aa \
    /data/cao-scratch/worktrees/cli-agent-orchestrator/c97e89aa origin/cao/c97e89aa
# HEAD is now at 1fc0df35  (== the reviewed commit, verified)
```

so this round builds on exactly the bytes the gate reviewed.

---

## B1 — resolver is not fail-safe for malformed metadata (FIXED)

### The finding is correct, and its severity claim is correct

Verified by reading, not by re-running the gate's probe:
`_recovered_caller_id(row)` is called at `epoch_recovery_service.py:247`, inside
the **outer** `try:` (line 243) whose only handler is a `finally:` (line 326)
that releases the three leases and re-raises. The `except Exception` that turns
a creation failure into a `resume_failed` result belongs to the **inner** `try:`
(line 255), which the raise never reaches. So an `AttributeError` from corrupt
metadata propagates out of `_recover_row` and aborts the row entirely — the base
is not recovered at all — instead of degrading to `None -> non-worker -> no
shim`. Exactly as the gate stated.

The second half of the finding is equally correct: `metadata.get("caller_id")`
was returned unvalidated, so `{"caller_id": 123}` reached `create_terminal` as an
`int` despite the helper's declared `str | None`.

### The repair

`src/cli_agent_orchestrator/services/epoch_recovery_service.py`

```diff
-def _recovered_caller_id(row) -> str | None:
+def _recovered_caller_id(row: Mapping[str, Any]) -> str | None:
     ...
+    Fail-safe by construction: recovery must never be aborted by corrupt
+    metadata, and ``create_terminal`` must never be handed a non-string
+    ``caller_id``. Anything that is not a mapping carrying a non-empty string
+    ``caller_id`` degrades to ``None`` (non-worker, no shim) instead of raising
+    or leaking a wrong-typed value.
     """
     source_terminal_id = row.get("source_terminal_id")
     if not source_terminal_id:
         return None
     metadata = get_terminal_metadata(source_terminal_id)
-    if not metadata:
-        return None
-    return metadata.get("caller_id")
+    if not isinstance(metadata, Mapping):
+        return None
+    caller_id = metadata.get("caller_id")
+    if isinstance(caller_id, str) and caller_id:
+        return caller_id
+    return None
```

plus the two stdlib imports (`collections.abc.Mapping`, `typing.Any`).

`isinstance(metadata, Mapping)` subsumes the old `if not metadata` (an empty
dict is a Mapping but yields no `caller_id`, `None` is not a Mapping), and
rejects the `str`/`list`/`int` shapes that raised. The `str` check is
deliberately `isinstance(...) and caller_id` so the empty string — falsy, and
therefore silently "no shim" under the F620 `if caller_id:` guard anyway —
resolves to a literal `None` rather than a `""` that misrepresents the contract
to any future caller.

**Scope note — what was deliberately NOT added:** no blanket
`try/except Exception` around `get_terminal_metadata`. The gate's required
repair was "accept only mapping-shaped metadata and only a non-empty string
`caller_id`", which the type checks satisfy exactly. A bare `except` would also
swallow database faults, which are a pre-existing whole-service concern and not
this defect. Flagging that as a judgement call, not hiding it.

### Behavioural coverage added (the gate's list, all five cases)

New `test_arm3_malformed_source_metadata_degrades_to_no_shim`, parametrized over
nine metadata shapes, drives the **real** `_recover_row` and asserts it
completes (`status == "resumed"`), hands the seam `caller_id is None`, and adds
no `PATH` key:

| id | metadata returned | gate's required case |
|----|-------------------|----------------------|
| `non_mapping_string` | `"src-term"` | non-mapping metadata (raised in r1) |
| `non_mapping_list` | `["caller_id"]` | non-mapping metadata (raised in r1) |
| `non_mapping_int` | `7` | non-mapping metadata |
| `empty_mapping` | `{}` | empty metadata |
| `caller_id_int` | `{"caller_id": 123}` | wrong-typed caller ID |
| `caller_id_list` | `{"caller_id": []}` | wrong-typed caller ID |
| `caller_id_empty_string` | `{"caller_id": ""}` | wrong-typed caller ID |
| `caller_id_absent` | `{"wrong_key": "x"}` | missing caller ID |
| `caller_id_none` | `{"caller_id": None}` | valid non-worker source |

Plus resolver units over the same nine shapes
(`test_recovered_caller_id_returns_none_for_malformed_metadata`), the
deleted-source-row case (already in `test_recovered_caller_id_resolution`:
`get_terminal_metadata` → `None`), and
`test_recovered_caller_id_returns_the_string_for_a_valid_worker` asserting the
happy path still returns a `str`. The pre-existing arms 1 / 2 / 2b are
unchanged.

---

## B2 — strict-mypy baseline (FIXED; delta is 0, measured)

The gate was right and the r1 report was wrong: `row` was unannotated, so
`_recovered_caller_id` was itself a `no-untyped-def` — base 24, fork 25.
Annotating `row: Mapping[str, Any]` removes that one instance. The file's six
other untyped siblings are untouched (out of scope, and touching them would
change the base comparison).

Measured on **box@grok-box-004**, same environment, same command shape,
`--no-incremental` with `.mypy_cache` removed before each run, base obtained by
`git checkout 5d39ec7f -- <the one file>` so only the file content varies:

```text
=== HEAD c7e50eb91df13bf2f5cfd91124fead300ce7d23f ===
Found 24 errors in 1 file (checked 1 source file)
HEAD_ERRORS=24
      1 [no-any-return]
     18 [no-untyped-call]
      4 [no-untyped-def]
      1 [type-arg]
=== BASE 5d39ec7f (file only) ===
Found 24 errors in 1 file (checked 1 source file)
BASE_ERRORS=24
      1 [no-any-return]
     18 [no-untyped-call]
      4 [no-untyped-def]
      1 [type-arg]
```

Normalized diff (error text with `path:line:` stripped, sorted) — run as a
second pass on the same box because the first pass's regex assumed a column
field mypy does not emit by default:

```text
=== normalized diff (line numbers stripped) ===
IDENTICAL — mypy delta 0
```

So: **base 24, fork 24, byte-identical error sets.** The `no-untyped-def` count
is 4 on both sides (the gate measured 4 base / 5 fork).

---

## Suite

**box@grok-box-002**, BOX_HEAD `c7e50eb91df13bf2f5cfd91124fead300ce7d23f`:

```text
uv run black --check --line-length 100 <service> <test>
All done! ✨ 🍰 ✨
2 files would be left unchanged.

uv run isort --check-only <service> <test>
(no output — clean)

uv run pytest -q test/services/test_f663_epoch_recovery_shim.py \
                test/services/test_epoch_recovery_service.py \
                test/services/test_laptop_shim.py \
                test/services/test_f636_shim_spawn_path.py \
                test/services/test_fx179_epoch_timestamps.py
145 passed in 4.77s
```

r1 ran three files (94 passed); this round adds `test_f636_shim_spawn_path.py`
and `test_fx179_epoch_timestamps.py` — the two other suites that touch the shim
and the epoch service — hence 145. Nothing is skipped or xfailed into green.

`test_d10_non_goals_are_absent_from_epoch_service` (the
`text.count("create_terminal(") == 1` scope guard in
`test_epoch_recovery_service.py`) is inside that 145 and passes: the repair adds
no `create_terminal` call site.

---

## Mutation transcript — both findings, RED → revert → GREEN

**box@grok-box-002**, BOX_HEAD `c7e50eb91d…`. Source under test forced ahead of
the box's editable install via `PYTHONPATH` pointing at a `mktemp -d` copy of
`src/` (the gate's documented requirement); the copy is removed by an EXIT trap
and the persistent box checkout was never mutated (`git status --short` at the
end: empty).

```text
===== CLEAN (mutated-copy path, unmutated) =====
23 passed in 2.16s

===== MUTATION M1: caller_id kwarg REMOVED from create_terminal =====
E       AssertionError: assert None == 'supervisor-9064394e'
test/services/test_f663_epoch_recovery_shim.py:167: AssertionError
FAILED ...::test_arm1_recovered_worker_is_shimmed_pytest_denied
1 failed, 22 passed in 2.13s
----- M1 REVERTED -----
23 passed in 2.28s

===== MUTATION M2: resolver reverted to r1 (unvalidated metadata.get) =====
        metadata = get_terminal_metadata(source_terminal_id)
        if not metadata:
            return None
>       return metadata.get("caller_id")
               ^^^^^^^^^^^^
E       AttributeError: 'str' object has no attribute 'get'
FAILED ...::test_arm3_malformed_source_metadata_degrades_to_no_shim[caller_id_empty_string]
FAILED ...::test_arm3_malformed_source_metadata_degrades_to_no_shim[caller_id_int]
FAILED ...::test_arm3_malformed_source_metadata_degrades_to_no_shim[caller_id_list]
FAILED ...::test_arm3_malformed_source_metadata_degrades_to_no_shim[non_mapping_list]
FAILED ...::test_arm3_malformed_source_metadata_degrades_to_no_shim[non_mapping_int]
FAILED ...::test_recovered_caller_id_returns_none_for_malformed_metadata[caller_id_int]
FAILED ...::test_arm3_malformed_source_metadata_degrades_to_no_shim[non_mapping_string]
FAILED ...::test_recovered_caller_id_returns_none_for_malformed_metadata[caller_id_list]
FAILED ...::test_recovered_caller_id_returns_none_for_malformed_metadata[caller_id_empty_string]
FAILED ...::test_recovered_caller_id_returns_none_for_malformed_metadata[non_mapping_int]
FAILED ...::test_recovered_caller_id_returns_none_for_malformed_metadata[non_mapping_list]
FAILED ...::test_recovered_caller_id_returns_none_for_malformed_metadata[non_mapping_string]
12 failed, 11 passed in 2.18s
----- M2 REVERTED -----
23 passed in 2.45s
git status of persistent checkout:
(empty)
```

**M1** is r1's mutation, re-run unchanged: deleting `caller_id=recovered_caller_id`
still turns arm 1 RED, so the thread-through is still the load-bearing change.

**M2** reverts *only* the new validation (restores r1's
`if not metadata: return None; return metadata.get("caller_id")`) and kills 12
tests. The traceback in the transcript **is** the gate's B1 defect reproduced
against the new coverage: the `AttributeError` fires at the resolver, inside
`_recover_row`, aborting the recovery.

Three shapes stay GREEN under M2 — `empty_mapping`, `caller_id_absent`,
`caller_id_none` — correctly: r1 already handled those, so they are regression
guards, not discriminators. `caller_id_empty_string` fails under M2 because r1
returns `""` rather than `None`.

---

## Conflict check against the frozen record

Neither repair widens the r1 contract, so there was nothing to escalate:

- The r1 helper's own docstring already declared the intended posture —
  "Source terminal gone / no source terminal -> None -> non-worker default, no
  shim where none belongs" — and the declared return type was already
  `str | None`. B1 makes the code honour the contract r1 froze; it does not
  change it.
- B2 is the explicitly deferred call from r1 ("Flagging for the supervisor's
  call if a stricter stance is wanted"). The stricter stance is now taken, on
  the one new function only.
- The diff still touches exactly two paths, `create_terminal(` still occurs
  exactly once, `terminal_service.py` is still untouched, and F634's
  `CAO_SERVER_PLANE` recovery refusal is still absent from both base and HEAD.

## Not fixed here (unchanged from r1, and the gate agreed)

`api/main.py:3736` / `:3861` — the `memory_manager` sidecar spawns in an
existing session with no `caller_id`, so the F620 guard is deterministically
bypassed. The gate called this a "real adjacent containment gap, file
separately" and explicitly said it "should not be repaired in F663". Still
report-only; it wants its own issue.

## Box / workspace ledger

| run | box | result |
|-----|-----|--------|
| `f663-r2-suite` (black + isort + 5 test files) | `box@grok-box-002` | 145 passed |
| `f663-r2-mypy` (strict A/B, `--no-incremental`) | `box@grok-box-004` | 24 / 24 |
| `f663-r2-mypydiff` (normalized diff) | `box@grok-box-004` | identical |
| `f663-r2-mutation` (M1 + M2) | `box@grok-box-002` | RED → GREEN both |

- All execution went through `scripts/box-run.sh`. **`grok-box-001` was never
  contacted.** `grok-box-003` is not enrolled in `scripts/boxes.tsv` (box-run
  warned and skipped it).
- **Zero pytest / mypy / uv execution on the laptop.** Laptop work was reading,
  editing, `git`, and writing this report.
- Both boxes are left on branch `cao/c97e89aa` at `c7e50eb91d…` with a clean
  working tree. Box `/tmp` artifacts: `/tmp/f663-mypy-head.txt`,
  `/tmp/f663-mypy-base.txt` (transient); the mutation copy under
  `/tmp/f663-mut.*` was removed by its EXIT trap.
- Environment mutations on boxes: none beyond `uv`'s normal `.venv` resolution.
- Not merged. Branch pushed to `origin/cao/c97e89aa`.

## Local artifacts

- Report: `/data/cao-scratch/f663-build-report-r2.md`
- Worktree: `/data/cao-scratch/worktrees/cli-agent-orchestrator/c97e89aa`
- Raw run logs: `/data/cao-scratch/f663-r2-suite.txt`,
  `/data/cao-scratch/f663-r2-mypy.txt`,
  `/data/cao-scratch/f663-r2-mypydiff.txt`,
  `/data/cao-scratch/f663-r2-mutation.txt`
