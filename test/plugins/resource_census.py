"""Per-test resource census plugin (F259).

Records per-test wall (phase-split), CPU (self + children), RSS delta,
I/O syscalls, file-descriptor delta, and subprocess spawns.  Produces a
ranked JSON artifact and a terminal summary section.

Activation: ``CAO_TEST_CENSUS=1`` env var **or** ``--resource-report=<path>``.
When off, nothing is registered — zero hooks, zero cost (D1, P-BUDGETMODE).

The census has **no verdict** — it never fails a test (D14).

Registered via the ``pytest_plugins`` tuple in ``test/conftest.py``.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LINUX = sys.platform == "linux"
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

_DEFAULT_OUTPUT_PATH = "tmp/orch/resource-census.json"
_DEFAULT_TOP_N = 15

# D11: slow-tier candidate thresholds (overridable via env).
_CANDIDATE_THRESHOLDS: dict[str, float] = {
    "wall_call": float(os.environ.get("CAO_TEST_CENSUS_CANDIDATE_WALL_CALL", "0.5")),
    "wall_setup": float(os.environ.get("CAO_TEST_CENSUS_CANDIDATE_WALL_SETUP", "0.5")),
    "rss_delta_kb": float(
        os.environ.get("CAO_TEST_CENSUS_CANDIDATE_RSS_KB", "51200")
    ),
}


# ---------------------------------------------------------------------------
# /proc helpers (D5, D7)
# ---------------------------------------------------------------------------


def _read_rss_kb() -> int | None:
    """Read RSS from /proc/self/statm (field 2 × page size → kB). D5."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * _PAGE_SIZE // 1024
    except (OSError, ValueError, IndexError):
        return None


def _read_proc_io() -> dict[str, int] | None:
    """Parse /proc/self/io → dict of rchar/wchar/syscr/syscw. D7."""
    try:
        result: dict[str, int] = {}
        with open("/proc/self/io") as f:
            for line in f:
                key, _, val = line.partition(":")
                key = key.strip()
                if key in ("rchar", "wchar", "syscr", "syscw"):
                    result[key] = int(val.strip())
        return result if result else None
    except (OSError, ValueError):
        return None


def _read_fd_count() -> int | None:
    """Count open file descriptors via /proc/self/fd. D7."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Subprocess counter (D6)
# ---------------------------------------------------------------------------

_original_popen_init = subprocess.Popen.__init__
_spawn_counter: int = 0


def _counting_popen_init(self: Any, *args: Any, **kwargs: Any) -> None:
    global _spawn_counter
    _spawn_counter += 1
    _original_popen_init(self, *args, **kwargs)


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class ResourceCensusPlugin:
    """Collects per-test resource metrics and produces the census artifact."""

    def __init__(self, output_path: str, top_n: int):
        self._output_path = output_path
        self._top_n = top_n
        self._is_worker = False
        self._worker_id: str | None = None
        self._worker_count: int | None = None

        # Per-test pre-values (reset each test)
        self._pre_rusage_self: Any = None
        self._pre_rusage_children: Any = None
        self._pre_rss_kb: int | None = None
        self._pre_io: dict[str, int] | None = None
        self._pre_fd: int | None = None
        self._pre_spawn_count: int = 0

        # Accumulated records: nodeid → metrics dict
        self._records: dict[str, dict[str, Any]] = {}
        # Phase durations from logreport: nodeid → {setup, call, teardown}
        self._wall: dict[str, dict[str, float]] = {}

    # --- Lifecycle ---

    def pytest_configure(self, config: pytest.Config) -> None:
        """Detect worker vs controller role (D8)."""
        self._is_worker = hasattr(config, "workerinput")
        if self._is_worker:
            self._worker_id = os.environ.get("PYTEST_XDIST_WORKER")
            self._worker_count = None
            count_str = os.environ.get("PYTEST_XDIST_WORKER_COUNT")
            if count_str:
                self._worker_count = int(count_str)
        else:
            # Controller or serial
            np = getattr(getattr(config, "option", None), "numprocesses", None)
            self._worker_count = np if np and np > 0 else None

    # --- Per-test hooks ---

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        """Record pre-test resource snapshot."""
        global _spawn_counter
        self._pre_rusage_self = resource.getrusage(resource.RUSAGE_SELF)
        self._pre_rusage_children = resource.getrusage(resource.RUSAGE_CHILDREN)
        self._pre_rss_kb = _read_rss_kb() if _LINUX else None
        self._pre_io = _read_proc_io() if _LINUX else None
        self._pre_fd = _read_fd_count() if _LINUX else None
        self._pre_spawn_count = _spawn_counter

    def pytest_runtest_teardown(self, item: pytest.Item) -> None:
        """Compute resource deltas and store the record."""
        global _spawn_counter

        # CPU (D3, D6)
        post_self = resource.getrusage(resource.RUSAGE_SELF)
        post_children = resource.getrusage(resource.RUSAGE_CHILDREN)

        cpu: dict[str, float | None] = {"user": None, "system": None, "children": None}
        if self._pre_rusage_self is not None:
            cpu["user"] = post_self.ru_utime - self._pre_rusage_self.ru_utime
            cpu["system"] = post_self.ru_stime - self._pre_rusage_self.ru_stime
        if self._pre_rusage_children is not None:
            children_u = post_children.ru_utime - self._pre_rusage_children.ru_utime
            children_s = post_children.ru_stime - self._pre_rusage_children.ru_stime
            cpu["children"] = children_u + children_s

        # RSS (D5)
        rss_delta_kb: int | None = None
        if _LINUX:
            post_rss = _read_rss_kb()
            if post_rss is not None and self._pre_rss_kb is not None:
                rss_delta_kb = post_rss - self._pre_rss_kb

        # I/O (D7)
        io_delta: dict[str, int | None] | None = None
        if _LINUX:
            post_io = _read_proc_io()
            if post_io is not None and self._pre_io is not None:
                io_delta = {
                    k: post_io.get(k, 0) - self._pre_io.get(k, 0)
                    for k in ("rchar", "wchar", "syscr", "syscw")
                }

        # FD (D7)
        fd_delta: int | None = None
        if _LINUX:
            post_fd = _read_fd_count()
            if post_fd is not None and self._pre_fd is not None:
                fd_delta = post_fd - self._pre_fd

        # Spawns (D6)
        spawns = _spawn_counter - self._pre_spawn_count

        # Tier from tier_marks
        tier = self._get_tier(item)

        record: dict[str, Any] = {
            "cpu": cpu,
            "rss_delta_kb": rss_delta_kb,
            "io": io_delta,
            "fd_delta": fd_delta,
            "spawns": spawns,
            "tier": tier,
            "worker": self._worker_id,
        }

        self._records[item.nodeid] = record

    # --- Phase wall from logreport (D4) ---

    def pytest_runtest_logreport(self, report: Any) -> None:
        """Accumulate per-phase wall durations from pytest's own reports."""
        nodeid = report.nodeid
        if nodeid not in self._wall:
            self._wall[nodeid] = {}
        self._wall[nodeid][report.when] = report.duration
        # Record outcome from the call phase
        if report.when == "call":
            if nodeid in self._records:
                self._records[nodeid]["outcome"] = report.outcome
            # Also store in wall dict for xdist merge (records may arrive later)
            self._wall[nodeid]["_outcome"] = report.outcome

    # --- xdist: worker → controller transport (D8) ---

    def pytest_sessionfinish(self, session: Any) -> None:
        """On workers, stash records into workeroutput for transport."""
        if self._is_worker:
            # Merge wall into records before transport
            self._merge_wall_into_records()
            session.config.workeroutput["cao_resource_census"] = self._records  # type: ignore[attr-defined]

    def pytest_testnodedown(self, node: Any, error: Any) -> None:
        """On controller, merge worker records."""
        worker_data = node.workeroutput.get("cao_resource_census")
        if worker_data:
            self._records.update(worker_data)

    # --- Terminal summary (D10) and artifact write ---

    def pytest_terminal_summary(
        self, terminalreporter: Any, exitstatus: int, config: pytest.Config
    ) -> None:
        """Print top-N per axis and write the JSON artifact."""
        # In serial or controller mode, merge wall here
        if not self._is_worker:
            self._merge_wall_into_records()

        if not self._records:
            return

        # Build sorted test list (D8 rule 1: sorted by nodeid)
        tests = []
        for nodeid in sorted(self._records.keys()):
            rec = self._records[nodeid]
            wall = self._wall.get(nodeid, {})
            # Outcome: prefer record, fall back to wall dict (xdist timing)
            outcome = rec.get("outcome") or wall.get("_outcome", "unknown")
            entry = {
                "nodeid": nodeid,
                "wall": {
                    "setup": wall.get("setup", 0.0),
                    "call": wall.get("call", 0.0),
                    "teardown": wall.get("teardown", 0.0),
                },
                "cpu": rec.get("cpu", {}),
                "rss_delta_kb": rec.get("rss_delta_kb"),
                "io": rec.get("io"),
                "fd_delta": rec.get("fd_delta"),
                "spawns": rec.get("spawns", 0),
                "tier": rec.get("tier", "unit"),
                "outcome": outcome,
                "worker": rec.get("worker"),
            }
            tests.append(entry)

        # D11: slow-tier candidates
        candidates = self._find_candidates(tests)

        # Build the artifact
        artifact = self._build_artifact(tests, candidates, config)

        # Write JSON (D9: one write at session end, AC12)
        out_path = Path(self._output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n"
        )

        # Terminal tail (D10)
        if self._top_n > 0:
            self._print_terminal_summary(terminalreporter, tests, candidates)

    # --- Helpers ---

    def _merge_wall_into_records(self) -> None:
        """Merge logreport wall data into records (serial/controller path)."""
        for nodeid, phases in self._wall.items():
            if nodeid not in self._records:
                # Tests with no resource record (e.g. skipped before setup)
                self._records[nodeid] = {
                    "cpu": {"user": None, "system": None, "children": None},
                    "rss_delta_kb": None,
                    "io": None,
                    "fd_delta": None,
                    "spawns": 0,
                    "tier": "unit",
                    "worker": self._worker_id,
                    "outcome": "skipped",
                }

    @staticmethod
    def _get_tier(item: pytest.Item) -> str:
        """Read the tier mark assigned by tier_marks.py."""
        for mark in item.iter_markers():
            if mark.name in (
                "unit", "contract", "integration", "sim",
                "slow", "live", "e2e", "pty",
            ):
                return mark.name
        return "unit"

    def _find_candidates(self, tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """D11: find unit-tier tests that trip candidate thresholds."""
        candidates = []
        for t in tests:
            if t["tier"] != "unit":
                continue
            trips = []
            wall = t.get("wall", {})
            if wall.get("call", 0) >= _CANDIDATE_THRESHOLDS["wall_call"]:
                trips.append("wall.call")
            if wall.get("setup", 0) >= _CANDIDATE_THRESHOLDS["wall_setup"]:
                trips.append("wall.setup")
            spawns = t.get("spawns", 0)
            cpu_children = (t.get("cpu") or {}).get("children", 0) or 0
            if spawns > 0 or cpu_children > 0:
                trips.append("spawns")
            rss = t.get("rss_delta_kb")
            if rss is not None and rss >= _CANDIDATE_THRESHOLDS["rss_delta_kb"]:
                trips.append("rss_delta_kb")
            if trips:
                candidates.append({"nodeid": t["nodeid"], "trips": trips})
        return candidates

    def _build_artifact(
        self,
        tests: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        config: pytest.Config,
    ) -> dict[str, Any]:
        """Build the full census JSON artifact."""
        run_url = os.environ.get("GITHUB_SERVER_URL", "")
        if run_url:
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            run_id = os.environ.get("GITHUB_RUN_ID", "")
            run_url = f"{run_url}/{repo}/actions/runs/{run_id}" if repo and run_id else ""

        artifact: dict[str, Any] = {
            "_notes": {
                "children_cpu": (
                    "RUSAGE_CHILDREN accumulates only on reap; a long-lived "
                    "session-fixture server books its CPU against whichever test "
                    "runs at reap time. Cross-reference with 'spawns' for attribution."
                ),
                "io": (
                    "rchar/wchar count bytes through read/write syscalls; "
                    "read_bytes/write_bytes (block device) are zero on tmpfs."
                ),
            },
            "header": {
                "worker_count": self._worker_count,
                "runner_image": os.environ.get("RUNNER_OS", "local"),
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
                "run_url": run_url or None,
                "test_count": len(tests),
            },
            "slow_tier_candidates": candidates,
            "tests": tests,
        }
        return artifact

    def _print_terminal_summary(
        self,
        tw: Any,
        tests: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> None:
        """D10: Print top-N per axis."""
        n = self._top_n

        # Wall (total = setup + call + teardown)
        by_wall = sorted(
            tests,
            key=lambda t: sum(t.get("wall", {}).values()),
            reverse=True,
        )[:n]
        tw.write_sep("=", "resource census: slowest wall (top {})".format(n))
        for t in by_wall:
            w = t["wall"]
            total = w["setup"] + w["call"] + w["teardown"]
            tw.write_line(
                f"  {total:7.2f} s  [setup {w['setup']:.2f} / call {w['call']:.2f} "
                f"/ teardown {w['teardown']:.2f}]  {t['tier']:<10}  {t['nodeid']}"
            )

        # CPU (user + system)
        by_cpu = sorted(
            tests,
            key=lambda t: (t.get("cpu", {}).get("user") or 0)
            + (t.get("cpu", {}).get("system") or 0),
            reverse=True,
        )[:n]
        tw.write_sep("=", "resource census: highest CPU (top {})".format(n))
        for t in by_cpu:
            c = t.get("cpu", {})
            total_cpu = (c.get("user") or 0) + (c.get("system") or 0)
            tw.write_line(
                f"  {total_cpu:7.4f} s  (u={c.get('user', 0):.4f} s={c.get('system', 0):.4f})  "
                f"{t['tier']:<10}  {t['nodeid']}"
            )

        # RSS delta
        by_rss = sorted(
            tests,
            key=lambda t: t.get("rss_delta_kb") or 0,
            reverse=True,
        )[:n]
        tw.write_sep("=", "resource census: highest RSS delta (top {})".format(n))
        for t in by_rss:
            rss = t.get("rss_delta_kb")
            rss_str = f"{rss:7d} kB" if rss is not None else "    N/A"
            tw.write_line(f"  {rss_str}  {t['tier']:<10}  {t['nodeid']}")

        # I/O syscalls (syscr + syscw)
        by_io = sorted(
            tests,
            key=lambda t: (
                ((t.get("io") or {}).get("syscr") or 0)
                + ((t.get("io") or {}).get("syscw") or 0)
            ),
            reverse=True,
        )[:n]
        tw.write_sep("=", "resource census: most I/O syscalls (top {})".format(n))
        for t in by_io:
            io = t.get("io") or {}
            total_io = (io.get("syscr") or 0) + (io.get("syscw") or 0)
            tw.write_line(
                f"  {total_io:7d}  (r={io.get('syscr', 0)} w={io.get('syscw', 0)})  "
                f"{t['tier']:<10}  {t['nodeid']}"
            )

        # Spawns
        by_spawns = sorted(
            tests,
            key=lambda t: t.get("spawns", 0),
            reverse=True,
        )[:n]
        tw.write_sep("=", "resource census: most subprocess spawns (top {})".format(n))
        for t in by_spawns:
            tw.write_line(
                f"  {t.get('spawns', 0):7d}  {t['tier']:<10}  {t['nodeid']}"
            )

        # D11 candidates
        if candidates:
            tw.write_sep(
                "=", "resource census: slow-tier candidates ({})".format(len(candidates))
            )
            for c in candidates:
                tw.write_line(f"  {c['nodeid']}  trips: {', '.join(c['trips'])}")


# ---------------------------------------------------------------------------
# pytest hooks (module-level — always loaded, but gating is in pytest_configure)
# ---------------------------------------------------------------------------


def pytest_addoption(parser: Any) -> None:
    """Register --resource-report CLI option."""
    parser.addoption(
        "--resource-report",
        default=None,
        metavar="PATH",
        help="Write resource census JSON to PATH (arms the census plugin).",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the plugin only when armed (D1, P-BUDGETMODE)."""
    armed = os.environ.get("CAO_TEST_CENSUS") or config.getoption(
        "--resource-report", None
    )
    if not armed:
        return

    # Determine output path
    output_path = config.getoption("--resource-report", None) or _DEFAULT_OUTPUT_PATH

    # Top-N from env
    top_n_str = os.environ.get("CAO_TEST_CENSUS_TOP", str(_DEFAULT_TOP_N))
    try:
        top_n = int(top_n_str)
    except ValueError:
        top_n = _DEFAULT_TOP_N

    plugin = ResourceCensusPlugin(output_path=output_path, top_n=top_n)
    config.pluginmanager.register(plugin, "resource_census")

    # D6: install subprocess.Popen counter patch
    subprocess.Popen.__init__ = _counting_popen_init  # type: ignore[assignment]
