"""Focused tests for the resource_census plugin (F259).

Runs with -p no:cacheprovider in its own tmpdir — no interference with
the real suite. Tests both serial (-n 0) and parallel (-n 2) per AC1/AC4.

Marked e2e — excluded from the parity suite (-m "not live and not e2e")
because each test spawns subprocess pytest instances.
Run focused: pytest test/plugins/test_resource_census.py -p no:suite_slot --override-ini="addopts="
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e  # subprocess-heavy; excluded from parity (-m "not live and not e2e")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLUGIN_PATH = str(Path(__file__).resolve().parent / "resource_census.py")
_TIER_MARKS_PATH = str(Path(__file__).resolve().parent / "tier_marks.py")


def _run_pytest(
    tmp_path: Path,
    test_file: str,
    *,
    n_workers: int = 0,
    census_env: bool = True,
    extra_args: list[str] | None = None,
) -> tuple[int, str, Path]:
    """Run pytest in a subprocess, return (exit_code, stdout, census_json_path)."""
    census_path = tmp_path / "census.json"
    conftest = tmp_path / "conftest.py"
    # Minimal conftest that registers tier_marks so tier derivation works
    conftest.write_text(
        textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent.parent)!r})
        pytest_plugins = (
            "test.plugins.tier_marks",
            "test.plugins.resource_census",
        )
        """)
    )

    test_path = tmp_path / "test_probe.py"
    test_path.write_text(test_file)

    env = os.environ.copy()
    if census_env:
        env["CAO_TEST_CENSUS"] = "1"
    else:
        env.pop("CAO_TEST_CENSUS", None)

    args = [
        sys.executable,
        "-m",
        "pytest",
        str(test_path),
        f"--resource-report={census_path}" if census_env else "",
        "-p",
        "no:cacheprovider",
        "--tb=short",
        "-q",
    ]
    if n_workers > 0:
        args.extend(["-n", str(n_workers), "--dist", "loadgroup"])

    if extra_args:
        args.extend(extra_args)

    # Filter out empty strings
    args = [a for a in args if a]

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=120,
    )
    return result.returncode, result.stdout + result.stderr, census_path


# ---------------------------------------------------------------------------
# Test data: probe suite with hogs (AC1)
# ---------------------------------------------------------------------------

_PROBE_SUITE = textwrap.dedent("""\
    import time
    import subprocess
    import sys
    import pytest

    # --- Hog: wall (2s sleep in setup fixture) ---
    @pytest.fixture
    def slow_setup_fixture():
        time.sleep(0.4)  # setup phase
        yield
        time.sleep(0.2)  # teardown phase

    def test_wall_hog(slow_setup_fixture):
        time.sleep(0.1)  # call phase

    # --- Hog: CPU (busy loop) ---
    def test_cpu_hog():
        total = 0
        for i in range(5_000_000):
            total += i

    # --- Hog: RSS (allocate 50 MB, held via fixture so it's alive at teardown) ---
    @pytest.fixture
    def rss_holder():
        data = bytearray(50 * 1024 * 1024)  # 50 MB
        yield data
        del data

    def test_rss_hog(rss_holder):
        assert len(rss_holder) > 0

    # --- Hog: subprocess (through two-level indirection for AC8) ---
    def _helper_inner():
        subprocess.run([sys.executable, "-c", "sum(range(2_000_000))"], check=True)

    def _helper_outer():
        _helper_inner()

    def test_spawn_hog():
        _helper_outer()

    # --- 20+ trivial tests to satisfy AC1 ---
    def test_trivial_00(): pass
    def test_trivial_01(): pass
    def test_trivial_02(): pass
    def test_trivial_03(): pass
    def test_trivial_04(): pass
    def test_trivial_05(): pass
    def test_trivial_06(): pass
    def test_trivial_07(): pass
    def test_trivial_08(): pass
    def test_trivial_09(): pass
    def test_trivial_10(): pass
    def test_trivial_11(): pass
    def test_trivial_12(): pass
    def test_trivial_13(): pass
    def test_trivial_14(): pass
    def test_trivial_15(): pass
    def test_trivial_16(): pass
    def test_trivial_17(): pass
    def test_trivial_18(): pass
    def test_trivial_19(): pass
""")


# ---------------------------------------------------------------------------
# AC1: Hogs rank #1 on their axis (serial AND parallel)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_workers", [0, 2], ids=["serial", "parallel"])
def test_ac1_hogs_rank_first(tmp_path: Path, n_workers: int) -> None:
    """AC1: deliberately hungry tests rank #1 on their axis."""
    exit_code, output, census_path = _run_pytest(
        tmp_path, _PROBE_SUITE, n_workers=n_workers
    )
    assert census_path.exists(), f"Census not written. Output:\n{output}"
    data = json.loads(census_path.read_text())
    tests = data["tests"]
    assert len(tests) >= 24  # 4 hogs + 20 trivials

    # Find tests by suffix
    def _find(suffix: str) -> dict:
        matches = [t for t in tests if t["nodeid"].endswith(suffix)]
        assert matches, f"No test ending with {suffix}"
        return matches[0]

    wall_hog = _find("test_wall_hog")
    cpu_hog = _find("test_cpu_hog")
    rss_hog = _find("test_rss_hog")
    spawn_hog = _find("test_spawn_hog")

    # Wall: test_wall_hog should be #1 by total wall
    by_wall = sorted(
        tests,
        key=lambda t: sum(t["wall"].values()),
        reverse=True,
    )
    assert by_wall[0]["nodeid"] == wall_hog["nodeid"], (
        f"Wall hog not #1: {by_wall[0]['nodeid']}"
    )

    # CPU: test_cpu_hog should be #1 by user+system
    by_cpu = sorted(
        tests,
        key=lambda t: (t["cpu"].get("user") or 0) + (t["cpu"].get("system") or 0),
        reverse=True,
    )
    assert by_cpu[0]["nodeid"] == cpu_hog["nodeid"], (
        f"CPU hog not #1: {by_cpu[0]['nodeid']}"
    )

    # RSS: test_rss_hog should be #1
    by_rss = sorted(
        tests,
        key=lambda t: t.get("rss_delta_kb") or 0,
        reverse=True,
    )
    assert by_rss[0]["nodeid"] == rss_hog["nodeid"], (
        f"RSS hog not #1: {by_rss[0]['nodeid']}"
    )

    # Spawns: test_spawn_hog should be #1
    by_spawns = sorted(tests, key=lambda t: t.get("spawns", 0), reverse=True)
    assert by_spawns[0]["nodeid"] == spawn_hog["nodeid"], (
        f"Spawn hog not #1: {by_spawns[0]['nodeid']}"
    )

    # Spawn hog should also show children CPU > 0
    assert (spawn_hog["cpu"].get("children") or 0) > 0


# ---------------------------------------------------------------------------
# AC3: Off-mode is byte-neutral
# ---------------------------------------------------------------------------


def test_ac3_off_mode(tmp_path: Path) -> None:
    """AC3: when CAO_TEST_CENSUS is unset, no plugin registered, no file."""
    census_path = tmp_path / "census.json"
    test_file = "def test_x(): pass\n"

    # Write conftest without census env
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent.parent)!r})
        pytest_plugins = (
            "test.plugins.tier_marks",
            "test.plugins.resource_census",
        )
        """)
    )
    test_path = tmp_path / "test_off.py"
    test_path.write_text(test_file)

    env = os.environ.copy()
    env.pop("CAO_TEST_CENSUS", None)

    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            str(test_path),
            "-p", "no:cacheprovider",
            "-q",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=30,
    )
    assert result.returncode == 0
    assert not census_path.exists()


# ---------------------------------------------------------------------------
# AC4: xdist merge determinism (nodeid list is stable)
# ---------------------------------------------------------------------------


def test_ac4_determinism(tmp_path: Path) -> None:
    """AC4: two -n 2 runs produce identical nodeid lists; -n 0 matches."""
    nodeids: list[list[str]] = []

    for run_idx in range(3):
        run_dir = tmp_path / f"run{run_idx}"
        run_dir.mkdir()
        n = 2 if run_idx < 2 else 0
        _, output, census_path = _run_pytest(
            run_dir, _PROBE_SUITE, n_workers=n
        )
        assert census_path.exists(), f"Run {run_idx} failed:\n{output}"
        data = json.loads(census_path.read_text())
        nodeids.append([t["nodeid"] for t in data["tests"]])

    # All three runs must have identical nodeid lists (sorted by nodeid)
    assert nodeids[0] == nodeids[1], "Two -n 2 runs differ in nodeid order"
    assert nodeids[0] == nodeids[2], "-n 0 differs from -n 2 nodeid order"

    # Verify no duplicates
    assert len(nodeids[0]) == len(set(nodeids[0])), "Duplicate nodeids found"


# ---------------------------------------------------------------------------
# AC5: No test lost in merge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_workers", [0, 2], ids=["serial", "parallel"])
def test_ac5_no_test_lost(tmp_path: Path, n_workers: int) -> None:
    """AC5: census test count == total passed+failed+skipped."""
    _, output, census_path = _run_pytest(
        tmp_path, _PROBE_SUITE, n_workers=n_workers
    )
    assert census_path.exists(), f"Census not written:\n{output}"
    data = json.loads(census_path.read_text())
    census_count = len(data["tests"])

    # Count expected: all probe tests should pass (24 total)
    # The test count in the header should match
    assert data["header"]["test_count"] == census_count
    assert census_count == 24  # 4 hogs + 20 trivials


# ---------------------------------------------------------------------------
# AC9: Phase split is truthful
# ---------------------------------------------------------------------------


def test_ac9_phase_split(tmp_path: Path) -> None:
    """AC9: phase split shows true setup/call/teardown, not tier_budget's shape."""
    phase_test = textwrap.dedent("""\
        import time
        import pytest

        @pytest.fixture
        def slow_phases():
            time.sleep(0.40)  # setup
            yield
            time.sleep(0.20)  # teardown

        def test_phased(slow_phases):
            time.sleep(0.10)  # call
    """)

    _, output, census_path = _run_pytest(tmp_path, phase_test, n_workers=0)
    assert census_path.exists(), f"Census missing:\n{output}"
    data = json.loads(census_path.read_text())

    t = data["tests"][0]
    wall = t["wall"]
    # AC9 bounds from blueprint
    assert 0.38 <= wall["setup"] <= 0.46, f"setup={wall['setup']}"
    assert 0.08 <= wall["call"] <= 0.16, f"call={wall['call']}"
    assert 0.18 <= wall["teardown"] <= 0.26, f"teardown={wall['teardown']}"


# ---------------------------------------------------------------------------
# AC8: slow-tier candidate flagging with spawn indirection
# ---------------------------------------------------------------------------


def test_ac8_candidate_spawns(tmp_path: Path) -> None:
    """AC8: spawn hog (unit tier, two-level indirection) appears in candidates."""
    _, output, census_path = _run_pytest(tmp_path, _PROBE_SUITE, n_workers=0)
    assert census_path.exists(), f"Census missing:\n{output}"
    data = json.loads(census_path.read_text())

    candidates = data["slow_tier_candidates"]
    spawn_candidates = [
        c for c in candidates if "test_spawn_hog" in c["nodeid"]
    ]
    assert spawn_candidates, "test_spawn_hog not in slow_tier_candidates"
    assert "spawns" in spawn_candidates[0]["trips"]


# ---------------------------------------------------------------------------
# AC11: Graceful degradation (monkeypatch /proc)
# ---------------------------------------------------------------------------


def test_ac11_degradation(tmp_path: Path) -> None:
    """AC11: with /proc unavailable, suite passes, fields are null not 0."""
    degraded_test = textwrap.dedent("""\
        import os
        import builtins

        _real_open = builtins.open
        _real_listdir = os.listdir

        def _broken_open(path, *a, **kw):
            if isinstance(path, str) and "/proc/self/" in path:
                raise OSError("monkeypatched: no /proc")
            return _real_open(path, *a, **kw)

        def _broken_listdir(path, *a, **kw):
            if isinstance(path, str) and "/proc/self/fd" in path:
                raise OSError("monkeypatched: no /proc/self/fd")
            return _real_listdir(path, *a, **kw)

        builtins.open = _broken_open
        os.listdir = _broken_listdir

        def test_still_works():
            assert True

        def test_also_works():
            x = 1 + 1
            assert x == 2
    """)

    _, output, census_path = _run_pytest(tmp_path, degraded_test, n_workers=0)
    assert census_path.exists(), f"Census missing:\n{output}"
    data = json.loads(census_path.read_text())

    for t in data["tests"]:
        # Fields that depend on /proc should be null, not 0
        assert t["rss_delta_kb"] is None, f"rss_delta_kb should be None: {t}"
        assert t["io"] is None, f"io should be None: {t}"
        assert t["fd_delta"] is None, f"fd_delta should be None: {t}"
        # CPU (from getrusage) should still work
        assert t["cpu"]["user"] is not None


# ---------------------------------------------------------------------------
# AC7: No verdict change (census doesn't fail tests)
# ---------------------------------------------------------------------------


def test_ac7_no_verdict_change(tmp_path: Path) -> None:
    """AC7: census on vs off produces same pass/fail counts."""
    test_with_failure = textwrap.dedent("""\
        def test_pass1(): pass
        def test_pass2(): pass
        def test_fail(): assert False
    """)

    # With census on
    dir_on = tmp_path / "on"
    dir_on.mkdir()
    rc_on, out_on, _ = _run_pytest(dir_on, test_with_failure, n_workers=0)

    # With census off
    dir_off = tmp_path / "off"
    dir_off.mkdir()
    rc_off, out_off, _ = _run_pytest(
        dir_off, test_with_failure, n_workers=0, census_env=False
    )

    # Exit codes must match (both should be 1 due to test_fail)
    assert rc_on == rc_off, f"Exit codes differ: on={rc_on} off={rc_off}"
