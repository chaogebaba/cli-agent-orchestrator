# WP-SUITE D3 Build Report (r1, amended)

Builder: kiro_dev (terminal 84ef6fdb, isolated worktree)
Base: `ce48138b` (f254-phase3-enforcement, includes D1/D2/D4)
Branch: `cao/84ef6fdb`
Date: 2026-08-19

**Amendments (gate fix round, 0B/1S/2N):**
- S1: removed `2>/dev/null` on vocab-hash `cat` — missing file now fails step loudly.
- N1: `wall_time_seconds` parsed from junit XML `time=` attribute (real suite wall time).
- N2: derived-attestation lookup branch commented as forward-looking dead code.

---

## Per-AC Evidence

### AC3.1 — F281 workflow ancestry (LANDED, verify only)

```
$ git merge-base --is-ancestor 184778a9 ce48138b
(exit 0 — YES)
```

`184778a9` (F281 CI merge) is an ancestor of the current base. Workflow present,
SHA-pinned actions intact. No build step needed.

### AC3.2 — Tree hash computation

Added as first post-checkout step in `.github/workflows/test-ci.yml`:
```yaml
- name: Compute tree hash
  id: tree
  run: |
    TREE=$(git rev-parse HEAD^{tree})
    echo "hash=$TREE" >> "$GITHUB_OUTPUT"
```
Every subsequent step keys on `${{ steps.tree.outputs.hash }}`, never `github.sha`.

### AC3.3 — Lookup mechanism: `gh attestation verify`

Single command fuses lookup + verification (no "found but unverified" state):
```yaml
gh attestation verify suite-identity.json \
  --repo "${{ github.repository }}" \
  --signer-workflow "${{ github.repository }}/.github/workflows/test-ci.yml" \
  --format json
```
Exit 0 = hit; non-zero = miss. Uses default `github.token` (read-only, no PAT).

### AC3.4 — Fail toward running

The lookup step runs under `set +e`. Any error — network, API, malformed
attestation, `gh` missing — is logged and treated as miss. Step exits 0
regardless, with `hit=false` in outputs. A missed hit costs 5 min; a wrong hit
costs correctness.

### AC3.5 — Deterministic suite-identity.json

Built from tree-only data:
```json
{"deselect_count":0,"marker_expr":"not live and not e2e","tree":"<TREE>","vocabulary_version":"<VOCAB>","workers":4}
```
Keys sorted, one trailing newline, `printf`-constructed. Any later run at the
same tree produces byte-identical output, making `gh attestation verify`'s
digest comparison work.

### AC3.5b — vocabulary_version

`sha256(cat pyproject.toml test/plugins/env_capabilities.py test/plugins/quarantine.py test/quarantine.toml)`.
All four files are in-tree, so any edit moves BOTH the tree hash (cache miss)
AND the vocabulary_version (legibility for humans reading the attestation).

### AC3.6 — evidence_kind: direct | derived

- Direct runs (suite executed) mint `"evidence_kind": "direct"`.
- The lookup step checks `evidence_kind` in matched attestations. If `derived`,
  it requires `derived_from` to be present (pointer to the direct root).
- Derived-from-derived is rejected: if a matched attestation is derived but has
  no `derived_from`, it's a miss.
- **Note:** The current workflow always mints `direct`. Derived attestations
  would only exist if a future short-circuit run chose to mint one (a run that
  hits the cache does NOT mint — it just skips). This is the correct minimal
  design: only a run that observes the suite first-hand signs for it.

### AC3.7 — No OIDC on fork PRs

Both the lookup and minting steps carry:
```yaml
if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
```
Fork PRs execute the suite normally but publish nothing. No fallback signing.

### AC3.8 — 30-day age bound

Extracted from attestation predicate's `timestamp` field. Computed as:
```bash
AGE_DAYS=$(( (NOW_EPOCH - ATTEST_EPOCH) / 86400 ))
if [ "$AGE_DAYS" -gt 30 ]; then miss; fi
```
Covers runner-image drift (the one thing the tree hash cannot see).

### AC3.9 — Least-privilege permissions

Job-level (not workflow-level):
```yaml
permissions:
  contents: read
  id-token: write
  attestations: write
```
Minting is the LAST step so nothing else runs while `id-token: write` is hot.
The lookup uses `github.token` (contents: read, always available).

### AC3.10 — No cross-reads invariant

**FORK side (AC3.11):** Mechanically enforced by `test/test_ci_isolation.py` —
parses the workflow and fails if it references `tcache`, `TCACHE`,
`/data/cao-scratch`, or `scripts/run-pytest.sh`.

**ROOT side (AC3.12): DEFERRED** — requires adding a case to
`scripts/test-tcache.sh` in the ROOT repo (`/home/chao/VScode_projects/cli-subagents/`).
This FORK worktree cannot write there. The case would assert `scripts/tcache`
contains no reference to `gh`, `attestation`, or `sigstore`, and that a
`tcache run` under a stubbed `gh` completes normally. Blocked on ROOT access;
no design ambiguity.

### AC3.11 — CI-side no-cross-reads test

`test/test_ci_isolation.py` — 3 test cases:
1. `test_workflow_exists` — sanity
2. `test_no_tcache_references` — asserts zero hits for forbidden tokens
3. `test_no_deselect_lines` — AC2.3b mechanical enforcement

```
$ uv run pytest test/test_ci_isolation.py -v
3 passed in 1.70s
```

### AC3.12 — ROOT-side no-cross-reads test: DEFERRED

See AC3.10 above. Lives in ROOT `scripts/test-tcache.sh`. Cannot be built from
this FORK worktree. Missing: ROOT write access.

### AC3.13 — Short-circuit produces no junit

The short-circuit path (`steps.cache_check.outputs.hit == 'true'`) skips:
- `Install uv` / `Set up Python` / `Install dependencies`
- `Run test-ci suite`
- `Write step summary` (the full version)
- `Upload test artifacts`
- `Extract suite results` / `Mint suite attestation`

Only `Short-circuit summary` runs (writes a cache-hit notice). No
`junit-results.xml` is produced. D6b's ledger tolerates the gap by design.

### AC3.14/AC3.15/AC3.16 — Doctrine rule: DEFERRED

These land in ROOT `doctrine/` (empirical-gate section). Cannot be written from
this FORK worktree. Missing: ROOT write access. The rule text is fully specified
in the blueprint (6 conditions a-f); there is no design ambiguity to resolve.

---

## Diff Stat

```
 .github/workflows/test-ci.yml | 175 insertions(+), 2 deletions(-)
 test/test_ci_isolation.py     | 54 insertions(+) (new file)
```

## Test Results

### Targeted (AC3.11 assertions)
```
$ uv run pytest test/test_ci_isolation.py -v -n 2
3 passed in 1.70s
```

### Smoke tier
```
$ uv run pytest -m smoke -n 2 -q
16 passed in 36.94s
```

### YAML validation
```
$ python -c "import yaml; yaml.safe_load(open('.github/workflows/test-ci.yml'))"
# exits 0, no parse errors
```

---

## Deferrals (honest, with exact missing state)

| AC | Repo | What's missing | Why blocked |
|---|---|---|---|
| AC3.12 | ROOT | Case in `scripts/test-tcache.sh` asserting tcache has no gh/attestation/sigstore refs | This worktree is FORK-only; ROOT (`/home/chao/VScode_projects/cli-subagents/`) is outside scope |
| AC3.14 | ROOT | Doctrine rule in `doctrine/` empirical-gate section | Same — ROOT write access |
| AC3.15 | ROOT | Doctrine exclusion list (live/e2e, ROOT-touching diffs, /data) | Same |
| AC3.16 | ROOT | Worker-count divergence note (-n 4 vs -n 2) | Same |

All four are text/test additions to the ROOT repo with zero design ambiguity.
They can be built by any lane with ROOT access in a single commit.

---

## What cannot be verified locally

- **Attestation minting** requires a GitHub Actions OIDC token — cannot be
  triggered from a local machine. First real test is on push to the fork.
- **Short-circuit hit** requires at least one prior attestation to exist — cannot
  be demonstrated until after the first green push with attestation minting.
- **`gh attestation verify` success** requires the fork's attestation store to
  have entries — empty at time of build.

These are not design gaps; they are the inherent chicken-and-egg of a first
deployment. The workflow is structured to fail-open on all of them (AC3.4),
meaning the first push simply runs the full suite and mints the first attestation.
