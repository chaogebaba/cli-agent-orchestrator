# Upstream-Merge Regression Fix Build Report — realserver workflow 404 + xdist_group marker

Branch: `cao/26643eb7` (based at `af15b36d`, worktree
`.cao/worktrees/26643eb7/cli-agent-orchestrator`)
Baseline for diff: `4e873cfd`

## Summary

Two independent defects on the upstream-merge branch, both fixed test-side, zero
production-source changes:

1. **realserver 404** — `test/api/test_workflow_lifecycle_realserver.py` submitted a
   script spec that the spawned real `cao-server` could not resolve, returning
   `404 {"detail":"unknown workflow 'rs_fast_...'"}` on submit. 4 tests failed
   (`composed_flow_over_real_http_script_tier`, `run_listing_over_real_http`,
   `cancel_over_real_http`, `detached_run_answerable_over_real_http`).
2. **xdist_group INTERNALERROR** — collection under `-p no:xdist` (xdist plugin
   disabled) raised `pytest.PytestUnknownMarkWarning: Unknown pytest.mark.xdist_group`,
   which `filterwarnings=error` + `--strict-markers` promoted to a collection
   INTERNALERROR (4 errors).

## Root Cause

### Defect 1 — NOT a workflow-wiring regression (brief theory corrected)

The brief's hypothesis was "workflow registration/route wiring lost in the merge —
patches in `api/main.py` and `session_service.py`." **This is incorrect.** The
`4e873cfd..af15b36d` diff touches **no** workflow source at all:

```
git diff --name-only 4e873cfd af15b36d -- \
  src/cli_agent_orchestrator/api/main.py \
  src/cli_agent_orchestrator/services/workflow_*.py \
  src/cli_agent_orchestrator/services/session_service.py \
  src/cli_agent_orchestrator/constants.py
# → (empty)
```

The only workflow-relevant files the merge changed are the **test harness**:
`test/api/test_workflow_lifecycle_realserver.py` (the `_spec_dir` helper) and
`test/conftest.py` (`_hermetic_cao_env`).

The merge rewrote `_spec_dir` to:

```python
if os.environ.get("CAO_HOME_DIR", "").strip():
    return WORKFLOW_SPEC_DIR
return server.home_dir / WORKFLOW_SPEC_DIR.relative_to(CAO_HOME_DIR.parent.parent)
```

`test/conftest.py:78` sets `os.environ["CAO_HOME_DIR"] = /tmp/cao-pytest-*` in the
**test process**, so `constants.WORKFLOW_SPEC_DIR` (import-time home-derived) resolves
under that pytest tmp dir. The test therefore wrote the script spec into the *test
process's* `CAO_HOME_DIR/workflows`.

But the `cao_server` fixture's `_subprocess_env` (`test/fixtures/cao_server.py`)
**deliberately strips** `CAO_HOME_DIR` and `CAO_HOME` from the child env and redirects
`HOME` to `server.home_dir`. The subprocess therefore resolves `WORKFLOW_SPEC_DIR`
under its *own* redirected `$HOME` = `server.home_dir/.aws/cli-agent-orchestrator/
workflows`. Spec written in one directory, subprocess reads another → the index
rebuild finds nothing → `KeyError` → `404 unknown workflow`.

The baseline `_spec_dir` was correct precisely because it always wrote to the
subprocess's redirected-`$HOME` location.

### Defect 2 — mark provided only by the plugin

`test/plugins/quarantine.py:107` calls `item.add_marker(pytest.mark.xdist_group(...))`.
`xdist_group` is registered by `pytest-xdist` **only when the plugin is loaded**. Under
`-p no:xdist` it is unknown; with `--strict-markers` + `filterwarnings=error` the
resulting `PytestUnknownMarkWarning` becomes a hard collection error.

## Changes

| File | Change |
|------|--------|
| `test/api/test_workflow_lifecycle_realserver.py` | Reverted `_spec_dir` to the baseline `server.home_dir / ".aws" / "cli-agent-orchestrator" / "workflows"`; removed now-unused imports (`os`, `CAO_HOME_DIR`, `WORKFLOW_SPEC_DIR`); documented why the redirected-`$HOME` path is the only correct target. |
| `pyproject.toml` | Registered `xdist_group(name)` in `[tool.pytest.ini_options].markers` so collection is clean whether or not the xdist plugin is loaded. |

Net: 2 files, +13 / −11.

## Design Decisions

1. **Fix the harness, not the source.** The empirical diff proves the workflow
   service/route wiring is untouched by the merge. Adding source "wiring" would have
   been a spurious change chasing a wrong theory; the correct fix is to make the test
   write where the subprocess actually reads.
2. **Revert to baseline `_spec_dir` rather than teach it about the stripped env.** The
   fixture's documented contract is that the subprocess always uses the redirected
   `$HOME` (it strips `CAO_HOME_DIR`/`CAO_HOME`). The baseline expression encodes that
   contract directly and is the minimal, intention-revealing fix.
3. **Register `xdist_group` in `markers`, not via a conftest shim.** This is the
   upstream-documented convention for a mark that must be known independent of plugin
   load order. It coexists harmlessly with the plugin's own registration (the plugin's
   definition wins when loaded).
4. **Did NOT touch the load-bearing `addopts` contract** (`-n 2 --dist loadgroup
   --strict-markers`, F254 D20–D22).

## Known / Disclosed

- The **literal** command `uv run pytest -p no:xdist --collect-only -q` still exits 4
  with a pytest **usage** error (`unrecognized arguments: -n --dist`), because default
  `addopts` hardcodes `-n 2 --dist loadgroup` and those options vanish with the plugin.
  This is **pre-existing** (identical at baseline `4e873cfd`) and is a *usage* error,
  **not** an INTERNALERROR. The INTERNALERROR the brief targets reproduces when the
  xdist-only addopts are neutralized (the realistic way to disable xdist), and that is
  now fully fixed. Making the literal invocation succeed would require gating the
  xdist-only addopts — a change to the F254 addopts contract that affects every test
  run and is out of scope here.

## Test Results (local, worktree; `uv run pytest`)

```
# Acceptance #1 — 4 realserver tests, -n0 --dist=no
uv run pytest -n0 --dist=no \
  test/api/test_workflow_lifecycle_realserver.py::test_composed_flow_over_real_http_script_tier \
  test/api/test_workflow_lifecycle_realserver.py::test_run_listing_over_real_http \
  test/api/test_workflow_lifecycle_realserver.py::test_cancel_over_real_http \
  test/api/test_workflow_lifecycle_realserver.py::test_detached_run_answerable_over_real_http
→ 4 passed in 9.52s

# Full realserver file (default -n 2 --dist loadgroup)
uv run pytest test/api/test_workflow_lifecycle_realserver.py
→ 6 passed, 1 skipped in 14.68s   (skip = disclosed YAML/agent-tier deferral)

# Acceptance #2 — collection with xdist DISABLED (addopts neutralized), no INTERNALERROR
uv run pytest -p no:xdist -o addopts="--strict-markers -p no:randomly" --collect-only -q
→ 14248 tests collected in 11.58s   (BEFORE fix: 4 errors / INTERNALERROR)

# Collection with xdist ENABLED (default) — sanity
uv run pytest --collect-only -q
→ 14248 tests collected

# Acceptance #3 — targeted regression for touched files
uv run pytest test/plugins/    # quarantine/serial_only machinery (pyproject markers)
→ 148 passed, 1 xfailed in 39.67s

# Lint
uv run black --check --line-length 100 test/api/test_workflow_lifecycle_realserver.py  → unchanged
uv run isort --check-only --profile black --line-length 100 test/api/test_workflow_lifecycle_realserver.py  → OK
```

Full-suite verification deferred to a box per the brief (not run locally). No merge
performed (F244).
