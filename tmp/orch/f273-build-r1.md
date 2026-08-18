# F273 Build Report — tmpfs eviction r1

**Artifact-Path:** /data/cao-scratch/f273-worktree/tmp/orch/f273-build-r1.md
**Artifact-SHA256:** 0b0b5ab39128956c2adc83d13d14784b46cf8ba95bd16247023c07a65e54476c
**Artifact-Repo-Path:** tmp/orch/f273-build-r1.md
**Git-SHA:** 032a73c1d6a65fe11284f9caafe741eb8addf206

---

## Summary

All project scratch routed to `/data/cao-scratch/tmp`. Fenced make targets
preflight with `findmnt /data` and exit 2 when absent. Central `scratch_dir()`
helper in Python for any runtime code that needs scratch. No /tmp fallback ever.

## Envelope — changed files (fork)

| File | Change |
|------|--------|
| `Makefile` | F273_PREFLIGHT define + `export TMPDIR` + preflight in 7 targets |
| `scripts/run-pytest.sh` | SUITE_LOCK → `/data/cao-scratch/tmp/cao-suite.lock` |
| `src/cli_agent_orchestrator/utils/scratch.py` | NEW — central scratch_dir() resolver |
| `test/utils/test_scratch.py` | NEW — 9 unit tests |

## Envelope — changed files (root repo, UNCOMMITTED)

| File | Change |
|------|--------|
| `scripts/test-gated-merge.sh` | F273 preflight + `/tmp/frozen` → `/data/cao-scratch/tmp/frozen`, all fixture paths |
| `scripts/test-tcache.sh` | F273 preflight + MARKER_DIR default + suite.lock → /data |

## Per-site /tmp adjudication table

| File:Line | Reference | Decision | Why |
|-----------|-----------|----------|-----|
| `providers/copilot_cli.py:50` | Comment: "e.g. /tmp/..." | **LEFT** | Regex pattern documentation, describes external path format |
| `providers/cursor_cli.py:407` | Comment: "/tmp/cursor-agent-logs/" | **LEFT** | External Cursor software log path, not our scratch |
| `providers/cursor_cli.py:542-544` | Docstring: "/tmp/cao_test" | **LEFT** | Documents test env-var override example, not operational |
| `clients/tmux.py:282` | Docstring: "/tmp" in blocked dirs list | **LEFT** | Documents the BLOCKED_DIRECTORIES policy |
| `services/workflow_spec_service.py:102` | Docstring: "cwd/tmp fixture" | **LEFT** | Describes test fixture pattern, not a path literal |
| `services/script_runner.py:933` | Comment: "../../../tmp/evil" | **LEFT** | Security documentation showing traversal attack vector |
| `services/wiki_healer.py:228` | Comment: "/tmp/repo/src/gone.py" | **LEFT** | Example path in regex documentation |
| `constants.py:349` | Comment: "never /tmp or cwd" | **LEFT** | Policy documentation |
| `utils/path_validation.py:28` | `"/tmp"` in BLOCKED_DIRECTORIES | **LEFT** | Load-bearing deny list — must block /tmp as working dir |
| `utils/path_validation.py:38` | `"/private/tmp"` in BLOCKED_DIRECTORIES | **LEFT** | macOS equivalent deny-list entry |
| `sandbox_bootstrap.py:507` | `TMUX_TMPDIR or "/tmp"` socket path | **LEFT** | tmux socket convention — tmux itself resolves sockets here |
| `skills/cao-worker-protocols/SKILL.md:109` | "/tmp" in bwrap context | **LEFT** | Security documentation about sandbox permissions |
| `scripts/tcache:266` (root) | `/tmp/pytest-` and `/tmp/xdist-` | **LEFT** | F237 D16 literal-/tmp predicate for strace read-set filtering |
| `scripts/test-orchestrator-md-a1.sh:171` (root) | TMUX_SOCK="/tmp/fx173-test-sock-$$" | **LEFT** | tmux socket convention (where tmux actually places sockets) |
| `scripts/test-self-redeploy.sh` (root) | Multiple `${PHOENIX_STUB_LOG:-/tmp}` | **LEFT** | Fallback defaults in heredoc stubs; harness sets env, never reached |

## Test evidence

### (i) make test-quick with /data present — TMPDIR exported

```
$ make -n test-quick TCACHE_BIN=.../scripts/tcache
if ! findmnt -rno TARGET /data >/dev/null 2>&1; then echo "F273: ..." >&2; exit 2; fi
mkdir -p /data/cao-scratch/tmp
".../scripts/tcache" run ".../scripts/run-pytest.sh"

$ make test-quick TCACHE_BIN=.../scripts/tcache ARGS="test/utils/test_scratch.py -n 0"
9 passed in 0.53s
```

### (ii) Simulated absence — unit test mocks _is_data_mounted=False

```
$ uv run pytest test/utils/test_scratch.py::TestScratchDir::test_raises_when_unmounted -v -n 0
PASSED — ScratchUnavailableError raised with correct message
```

### (iii) Disk-floor-adjacent tests still pass

```
$ uv run pytest test/services/test_f119_disk_space_guard.py -v -n 0
4 passed in 0.25s

$ uv run pytest test/utils/test_path_validation.py -v -n 0
55 passed in 0.76s
```

## Slot-gated full suite

**NOT YET RUN** — requires exclusive suite slot.

REQUEST SUITE SLOT F273 — waiting for supervisor GO before running:
```
TCACHE_BIN=/home/chao/VScode_projects/cli-subagents/scripts/tcache make test-quick
```
