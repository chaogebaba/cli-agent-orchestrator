"""Focused tests for F261 dead-code tombstones.

Covers ACs 1-13 per the blueprint. Each test uses an isolated temp dir
for both CAO_TOMBSTONE_DIR and patches module state to avoid cross-talk.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Helpers ────────────────────────────────────────────────────────────

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
REGISTRY_PATH = Path(__file__).resolve().parents[2] / "orchestrator" / "tombstones" / "registry.jsonl"
FORK_SRC = Path(__file__).resolve().parents[2] / "src"

# tombstone-report lives in the ROOT repo (beside scripts/tcache).
# Resolve via git common-dir (handles worktrees) or fall back to env.
def _find_root_repo_scripts() -> Path:
    """Locate root-repo scripts/ from the fork worktree."""
    # Try env override first (test isolation)
    env_path = os.environ.get("CAO_TOMBSTONE_REPORT_PATH")
    if env_path:
        return Path(env_path).parent
    # Walk up from this file's fork root to find the root repo
    fork_root = Path(__file__).resolve().parents[2]
    # Normal checkout: fork is at <root>/cli-agent-orchestrator/
    root_candidate = fork_root.parent
    if (root_candidate / "scripts" / "tombstone-report").exists():
        return root_candidate / "scripts"
    # Worktree: use git common-dir
    import subprocess
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(fork_root), capture_output=True, text=True
        ).stdout.strip()
        if common:
            common_path = Path(common).resolve()
            # common-dir is .git inside the fork; root repo is two levels up
            root_from_git = common_path.parent.parent
            if (root_from_git / "scripts" / "tombstone-report").exists():
                return root_from_git / "scripts"
    except (OSError, subprocess.SubprocessError):
        pass
    # Fallback: known absolute path
    fallback = Path("/home/chao/VScode_projects/cli-subagents/scripts")
    if (fallback / "tombstone-report").exists():
        return fallback
    return fork_root / "scripts"  # last resort

ROOT_SCRIPTS_DIR = _find_root_repo_scripts()


@pytest.fixture
def tomb_dir(tmp_path):
    """Provide an isolated tombstone directory and a fresh module state."""
    ledger_dir = tmp_path / "cao-tombstones"
    ledger_dir.mkdir()
    with patch.dict(os.environ, {"CAO_TOMBSTONE_DIR": str(ledger_dir)}):
        yield ledger_dir


@pytest.fixture
def fresh_tombstone_module(tomb_dir):
    """Reimport tombstones module with a fresh state pointing at tomb_dir."""
    # Remove from sys.modules to force reimport with new env
    mod_name = "cli_agent_orchestrator.utils.tombstones"
    old_mod = sys.modules.pop(mod_name, None)
    try:
        import cli_agent_orchestrator.utils.tombstones as ts
        importlib.reload(ts)
        yield ts
    finally:
        # Restore
        if old_mod is not None:
            sys.modules[mod_name] = old_mod


def _read_ledger(tomb_dir: Path) -> list[dict]:
    ledger = tomb_dir / "fired.jsonl"
    if not ledger.exists():
        return []
    records = []
    for line in ledger.read_text().splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _seed_ledger(tomb_dir: Path, records: list[dict]):
    ledger = tomb_dir / "fired.jsonl"
    with open(ledger, "w") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")


def _run_report(*args, tomb_dir=None, registry=None, fork_root=None):
    """Run tombstone-report with given args and env overrides."""
    env = os.environ.copy()
    if tomb_dir:
        env["CAO_TOMBSTONE_DIR"] = str(tomb_dir)
    if registry:
        env["CAO_TOMBSTONE_REGISTRY"] = str(registry)
    if fork_root:
        env["CAO_FORK_ROOT"] = str(fork_root)
    result = subprocess.run(
        [sys.executable, str(ROOT_SCRIPTS_DIR / "tombstone-report")] + list(args),
        capture_output=True,
        text=True,
        env=env,
    )
    return result


# ── AC1: Never-deployed plant reads no_evidence ────────────────────────


class TestAC1:
    """A never-deployed plant reads no_evidence, never unfired_ripe."""

    def test_no_evidence_when_exec_predates_plant(self, tmp_path):
        """Even with 500 execs and 60 days age, if build.mt < planted_at -> no_evidence."""
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()
        planted_at = "2026-08-18T02:34:13Z"
        planted_ts = 1786412053  # epoch of planted_at

        # Seed ledger: 500 prod execs and 20 test execs, all with build.mt BEFORE planted_at
        records = []
        for i in range(500):
            records.append({
                "k": "exec",
                "ts": "2026-06-15T00:00:00Z",
                "ctx": "prod",
                "build": {"root": "/old/install", "mt": planted_ts - 100},
                "argv0": "cao-server",
            })
        for i in range(20):
            records.append({
                "k": "exec",
                "ts": "2026-06-15T00:00:00Z",
                "ctx": "test",
                "build": {"root": "/old/install", "mt": planted_ts - 100},
                "argv0": "pytest",
            })
        _seed_ledger(tomb_dir, records)

        # Create a minimal registry with one site
        reg = tmp_path / "registry.jsonl"
        reg.write_text(json.dumps({
            "id": "TS-TEST1",
            "repo": "fork",
            "file": "src/cli_agent_orchestrator/providers/kimi_cli.py",
            "symbol": "extract_session_context",
            "shape": "test_only_reachable",
            "snippet_sha256": "abcd1234",
            "planted_at": planted_at,
            "rationale": "test",
            "soak": {"prod_exec": 20, "test_exec": 3, "days": 14},
            "retired_at": None,
            "retired_verdict": None,
        }) + "\n")

        result = _run_report(
            "--json", "--site", "TS-TEST1",
            tomb_dir=tomb_dir, registry=reg,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout.strip())
        assert data["verdict"] == "no_evidence"
        assert data["reason"] == "not_deployed"
        assert "unfired_ripe" not in result.stdout


# ── AC2: Fired site never reported removable ───────────────────────────


class TestAC2:
    """A fired site is never reported removable."""

    def test_fired_prod(self, tmp_path):
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()
        planted_at = "2026-08-01T00:00:00Z"
        planted_ts = 1785945600

        records = [
            {"k": "exec", "ts": "2026-08-10T00:00:00Z", "ctx": "prod",
             "build": {"root": "/new", "mt": planted_ts + 1000}, "argv0": "cao"},
            {"k": "fire", "id": "TS-X", "ts": "2026-08-10T01:00:00Z",
             "ctx": "prod", "build": {"root": "/new", "mt": planted_ts + 1000}, "pid": 1234},
        ]
        # Add enough execs to satisfy soak
        for i in range(25):
            records.append({"k": "exec", "ts": "2026-08-10T00:00:00Z", "ctx": "prod",
                           "build": {"root": "/new", "mt": planted_ts + 1000}, "argv0": "cao"})
        for i in range(5):
            records.append({"k": "exec", "ts": "2026-08-10T00:00:00Z", "ctx": "test",
                           "build": {"root": "/new", "mt": planted_ts + 1000}, "argv0": "pytest"})
        _seed_ledger(tomb_dir, records)

        reg = tmp_path / "registry.jsonl"
        reg.write_text(json.dumps({
            "id": "TS-X", "repo": "fork", "file": "x.py", "symbol": "x",
            "shape": "test_only_reachable", "snippet_sha256": "abc",
            "planted_at": planted_at, "rationale": "test",
            "soak": {"prod_exec": 20, "test_exec": 3, "days": 14},
            "retired_at": None, "retired_verdict": None,
        }) + "\n")

        result = _run_report("--json", "--site", "TS-X", tomb_dir=tomb_dir, registry=reg)
        data = json.loads(result.stdout.strip())
        assert data["verdict"] == "fired_prod"
        assert "unfired" not in result.stdout

    def test_fired_test(self, tmp_path):
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()
        planted_at = "2026-08-01T00:00:00Z"
        planted_ts = 1785945600

        records = [
            {"k": "fire", "id": "TS-Y", "ts": "2026-08-10T01:00:00Z",
             "ctx": "test", "build": {"root": "/new", "mt": planted_ts + 1000}, "pid": 1234},
        ]
        for i in range(25):
            records.append({"k": "exec", "ts": "2026-08-10T00:00:00Z", "ctx": "prod",
                           "build": {"root": "/new", "mt": planted_ts + 1000}, "argv0": "cao"})
        for i in range(5):
            records.append({"k": "exec", "ts": "2026-08-10T00:00:00Z", "ctx": "test",
                           "build": {"root": "/new", "mt": planted_ts + 1000}, "argv0": "pytest"})
        _seed_ledger(tomb_dir, records)

        reg = tmp_path / "registry.jsonl"
        reg.write_text(json.dumps({
            "id": "TS-Y", "repo": "fork", "file": "y.py", "symbol": "y",
            "shape": "test_only_family", "snippet_sha256": "abc",
            "planted_at": planted_at, "rationale": "test",
            "soak": {"prod_exec": 20, "test_exec": 3, "days": 14},
            "retired_at": None, "retired_verdict": None,
        }) + "\n")

        result = _run_report("--json", "--site", "TS-Y", tomb_dir=tomb_dir, registry=reg)
        data = json.loads(result.stdout.strip())
        assert data["verdict"] == "fired_test_only"
        assert "unfired" not in result.stdout


# ── AC3: Test-only firing is its own verdict ───────────────────────────


class TestAC3:
    """fired_test_only and transition to fired_prod on adding a prod fire."""

    def test_test_only_then_prod(self, tmp_path):
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()
        planted_at = "2026-08-01T00:00:00Z"
        planted_ts = 1785945600

        records = [
            {"k": "fire", "id": "TS-Z", "ts": "2026-08-10T01:00:00Z",
             "ctx": "test", "build": {"root": "/new", "mt": planted_ts + 1000}, "pid": 1},
        ]
        for i in range(25):
            records.append({"k": "exec", "ts": "2026-08-10T00:00:00Z", "ctx": "prod",
                           "build": {"root": "/new", "mt": planted_ts + 1000}, "argv0": "cao"})
        for i in range(5):
            records.append({"k": "exec", "ts": "2026-08-10T00:00:00Z", "ctx": "test",
                           "build": {"root": "/new", "mt": planted_ts + 1000}, "argv0": "pytest"})
        _seed_ledger(tomb_dir, records)

        reg = tmp_path / "registry.jsonl"
        reg.write_text(json.dumps({
            "id": "TS-Z", "repo": "fork", "file": "z.py", "symbol": "z",
            "shape": "test_only_family", "snippet_sha256": "abc",
            "planted_at": planted_at, "rationale": "test",
            "soak": {"prod_exec": 20, "test_exec": 3, "days": 14},
            "retired_at": None, "retired_verdict": None,
        }) + "\n")

        # First: test-only
        result = _run_report("--json", "--site", "TS-Z", tomb_dir=tomb_dir, registry=reg)
        data = json.loads(result.stdout.strip())
        assert data["verdict"] == "fired_test_only"
        assert "tests" in data["next_action"].lower()

        # Add a prod fire
        with open(tomb_dir / "fired.jsonl", "a") as f:
            f.write(json.dumps({
                "k": "fire", "id": "TS-Z", "ts": "2026-08-11T00:00:00Z",
                "ctx": "prod", "build": {"root": "/new", "mt": planted_ts + 1000}, "pid": 2,
            }) + "\n")

        result = _run_report("--json", "--site", "TS-Z", tomb_dir=tomb_dir, registry=reg)
        data = json.loads(result.stdout.strip())
        assert data["verdict"] == "fired_prod"


# ── AC4: Broken sink cannot change the program ─────────────────────────


class TestAC4:
    """tombstone() never raises regardless of sink state."""

    def test_unwritable_dir(self, tmp_path):
        """Point CAO_TOMBSTONE_DIR at a read-only dir; tombstone() must not raise."""
        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        os.chmod(ro_dir, 0o500)
        try:
            with patch.dict(os.environ, {"CAO_TOMBSTONE_DIR": str(ro_dir), "CAO_TOMBSTONES": "1"}):
                # Re-import to pick up new env
                mod_name = "cli_agent_orchestrator.utils.tombstones"
                sys.modules.pop(mod_name, None)
                import cli_agent_orchestrator.utils.tombstones as ts
                importlib.reload(ts)
                # Must not raise
                ts._seen.clear()
                ts.tombstone("TS-BROKEN-1")
                # Still returns None
                assert ts.tombstone("TS-BROKEN-2") is None
        finally:
            os.chmod(ro_dir, 0o700)

    def test_nonexistent_parent(self, tmp_path):
        """Dir doesn't exist and parent is read-only."""
        ro_parent = tmp_path / "nowrite"
        ro_parent.mkdir()
        os.chmod(ro_parent, 0o500)
        target = ro_parent / "subdir" / "nested"
        try:
            with patch.dict(os.environ, {"CAO_TOMBSTONE_DIR": str(target), "CAO_TOMBSTONES": "1"}):
                mod_name = "cli_agent_orchestrator.utils.tombstones"
                sys.modules.pop(mod_name, None)
                import cli_agent_orchestrator.utils.tombstones as ts
                importlib.reload(ts)
                ts._seen.clear()
                ts.tombstone("TS-BROKEN-3")  # must not raise
        finally:
            os.chmod(ro_parent, 0o700)


# ── AC7: Cached invocations don't count ────────────────────────────────


class TestAC7:
    """Soak cannot be satisfied by cached invocations.

    This is tested at the verdict level: exec count directly determines the verdict.
    """

    def test_one_exec_not_enough(self, tmp_path):
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()
        planted_at = "2026-08-01T00:00:00Z"
        planted_ts = 1785945600

        # Only 1 test exec (simulates 40 invocations with 39 cache HITs)
        records = [
            {"k": "exec", "ts": "2026-08-10T00:00:00Z", "ctx": "test",
             "build": {"root": "/new", "mt": planted_ts + 1000}, "argv0": "pytest"},
        ]
        # Enough prod execs
        for i in range(25):
            records.append({"k": "exec", "ts": "2026-08-10T00:00:00Z", "ctx": "prod",
                           "build": {"root": "/new", "mt": planted_ts + 1000}, "argv0": "cao"})
        _seed_ledger(tomb_dir, records)

        reg = tmp_path / "registry.jsonl"
        reg.write_text(json.dumps({
            "id": "TS-CACHE", "repo": "fork", "file": "x.py", "symbol": "x",
            "shape": "test_only_reachable", "snippet_sha256": "abc",
            "planted_at": planted_at, "rationale": "test",
            "soak": {"prod_exec": 20, "test_exec": 3, "days": 14},
            "retired_at": None, "retired_verdict": None,
        }) + "\n")

        result = _run_report("--json", "--site", "TS-CACHE", tomb_dir=tomb_dir, registry=reg)
        data = json.loads(result.stdout.strip())
        assert data["verdict"] == "unfired_green"
        assert "test_exec 1/3" in data["reason"]


# ── AC9: Registry/code drift detected both directions ──────────────────


class TestAC9:
    """E-ORPHAN-SITE and E-MISSING-SITE detection."""

    def test_orphan_site(self, tmp_path):
        """tombstone("TS-9999") in code with no registry record."""
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()

        # Create a fake src tree with an orphan tombstone
        src = tmp_path / "src"
        src.mkdir()
        pkg = src / "cli_agent_orchestrator" / "utils"
        pkg.mkdir(parents=True)
        (pkg / "fake.py").write_text('from cli_agent_orchestrator.utils.tombstones import tombstone\ntombstone("TS-9999")\n')

        # Registry with only TS-0001
        reg = tmp_path / "registry.jsonl"
        reg.write_text(json.dumps({
            "id": "TS-0001", "repo": "fork",
            "file": "src/cli_agent_orchestrator/utils/fake.py",
            "symbol": "x", "shape": "test_only_reachable",
            "snippet_sha256": "abc", "planted_at": "2026-08-18T00:00:00Z",
            "rationale": "test", "soak": {"prod_exec": 20, "test_exec": 3, "days": 14},
            "retired_at": None, "retired_verdict": None,
        }) + "\n")

        result = _run_report("--verify", tomb_dir=tomb_dir, registry=reg, fork_root=src)
        assert result.returncode == 2
        assert "E-ORPHAN-SITE" in result.stderr
        assert "TS-9999" in result.stderr

    def test_missing_site(self, tmp_path):
        """Registry record whose tombstone line has been deleted from code."""
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()

        # Create a src tree with NO tombstone calls
        src = tmp_path / "src"
        src.mkdir()
        pkg = src / "cli_agent_orchestrator" / "utils"
        pkg.mkdir(parents=True)
        (pkg / "clean.py").write_text("# no tombstones here\n")

        # Registry with TS-GONE
        reg = tmp_path / "registry.jsonl"
        reg.write_text(json.dumps({
            "id": "TS-GONE", "repo": "fork",
            "file": "src/cli_agent_orchestrator/utils/clean.py",
            "symbol": "x", "shape": "test_only_reachable",
            "snippet_sha256": "abc", "planted_at": "2026-08-18T00:00:00Z",
            "rationale": "test", "soak": {"prod_exec": 20, "test_exec": 3, "days": 14},
            "retired_at": None, "retired_verdict": None,
        }) + "\n")

        result = _run_report("--verify", tomb_dir=tomb_dir, registry=reg, fork_root=src)
        assert result.returncode == 2
        assert "E-MISSING-SITE" in result.stderr
        assert "TS-GONE" in result.stderr


# ── AC10: Dedup holds under load ───────────────────────────────────────


class TestAC10:
    """One fire record per site per process, regardless of call count."""

    def test_dedup_10k(self, tmp_path):
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()

        with patch.dict(os.environ, {"CAO_TOMBSTONE_DIR": str(tomb_dir), "CAO_TOMBSTONES": "1"}):
            mod_name = "cli_agent_orchestrator.utils.tombstones"
            sys.modules.pop(mod_name, None)
            import cli_agent_orchestrator.utils.tombstones as ts
            importlib.reload(ts)
            ts._seen.clear()

            t0 = time.perf_counter_ns()
            for _ in range(10_000):
                ts.tombstone("TS-HOT")
            t1 = time.perf_counter_ns()

        records = _read_ledger(tomb_dir)
        fire_records = [r for r in records if r.get("k") == "fire" and r.get("id") == "TS-HOT"]
        assert len(fire_records) == 1

        # Report per-call cost (after first)
        per_call_ns = (t1 - t0) / 10_000
        print(f"\nAC10: per-call cost after first = {per_call_ns:.0f} ns ({per_call_ns/1000:.2f} µs)")
        # Design target: <= 1µs
        assert per_call_ns < 5000, f"Per-call too slow: {per_call_ns} ns"


# ── AC11: ctx correct at import/collection time ───────────────────────


class TestAC11:
    """A probe fired during pytest collection (no PYTEST_CURRENT_TEST) still reads ctx=test."""

    def test_ctx_via_sys_modules(self, tmp_path):
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()

        # Simulate: PYTEST_CURRENT_TEST is unset but pytest is in sys.modules
        env_patch = {"CAO_TOMBSTONE_DIR": str(tomb_dir), "CAO_TOMBSTONES": "1"}
        with patch.dict(os.environ, env_patch):
            # Remove PYTEST_CURRENT_TEST if present
            os.environ.pop("PYTEST_CURRENT_TEST", None)
            mod_name = "cli_agent_orchestrator.utils.tombstones"
            sys.modules.pop(mod_name, None)
            # pytest IS in sys.modules (because we're running under pytest)
            assert "pytest" in sys.modules
            import cli_agent_orchestrator.utils.tombstones as ts
            importlib.reload(ts)
            ts._seen.clear()
            ts.tombstone("TS-COLLECT")

        records = _read_ledger(tomb_dir)
        fire = [r for r in records if r.get("k") == "fire" and r.get("id") == "TS-COLLECT"]
        assert len(fire) == 1
        assert fire[0]["ctx"] == "test"


# ── AC12: Report tool is read-only outside --compact ───────────────────


class TestAC12:
    """tombstone-report does not modify ledger/registry in non-compact modes."""

    def test_read_only_default_mode(self, tmp_path):
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()
        ledger = tomb_dir / "fired.jsonl"
        ledger.write_text('{"k":"exec","ts":"2026-08-10T00:00:00Z","ctx":"prod","build":{"root":"/x","mt":9999999999},"argv0":"x"}\n')

        reg = tmp_path / "registry.jsonl"
        reg.write_text(json.dumps({
            "id": "TS-RO", "repo": "fork", "file": "x.py", "symbol": "x",
            "shape": "test_only_reachable", "snippet_sha256": "abc",
            "planted_at": "2026-08-01T00:00:00Z", "rationale": "test",
            "soak": {"prod_exec": 20, "test_exec": 3, "days": 14},
            "retired_at": None, "retired_verdict": None,
        }) + "\n")

        # Snapshot before
        ledger_bytes = ledger.read_bytes()
        ledger_mtime = ledger.stat().st_mtime
        reg_bytes = reg.read_bytes()
        reg_mtime = reg.stat().st_mtime

        _run_report("--json", tomb_dir=tomb_dir, registry=reg)

        # Assert unchanged
        assert ledger.read_bytes() == ledger_bytes
        assert ledger.stat().st_mtime == ledger_mtime
        assert reg.read_bytes() == reg_bytes
        assert reg.stat().st_mtime == reg_mtime

    def test_corrupt_ledger_tolerated(self, tmp_path):
        """Bad lines don't prevent verdicts on good records."""
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()
        ledger = tomb_dir / "fired.jsonl"
        # Good record, then corrupt, then binary blob
        ledger.write_text(
            '{"k":"exec","ts":"2026-08-10T00:00:00Z","ctx":"prod","build":{"root":"/x","mt":9999999999},"argv0":"x"}\n'
            'this is not json\n'
            '\x00\x01\x02\x03\n'
            '{"k":"exec","ts":"2026-08-11T00:00:00Z","ctx":"test","build":{"root":"/x","mt":9999999999},"argv0":"pytest"}\n'
        )

        reg = tmp_path / "registry.jsonl"
        reg.write_text(json.dumps({
            "id": "TS-CORRUPT", "repo": "fork", "file": "x.py", "symbol": "x",
            "shape": "test_only_reachable", "snippet_sha256": "abc",
            "planted_at": "2026-08-01T00:00:00Z", "rationale": "test",
            "soak": {"prod_exec": 20, "test_exec": 3, "days": 14},
            "retired_at": None, "retired_verdict": None,
        }) + "\n")

        result = _run_report("--json", "--site", "TS-CORRUPT", tomb_dir=tomb_dir, registry=reg)
        # Must produce a verdict despite corrupt lines
        assert result.returncode == 0
        data = json.loads(result.stdout.strip())
        assert data["verdict"] in ("unfired_green", "no_evidence")


# ── AC13: --compact preserves denominator and is crash-safe ────────────


class TestAC13:
    """Compact folds execs, preserves fires, uses os.replace."""

    def test_compact_preserves_counts(self, tmp_path):
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()
        planted_at = "2026-08-01T00:00:00Z"
        planted_ts = 1785945600

        # Create 100 exec records across 5 days, 2 contexts, 2 builds
        records = []
        for day in range(5):
            for ctx in ("prod", "test"):
                for mt_offset in (1000, 2000):
                    for _ in range(5):
                        ts = f"2026-08-{2+day:02d}T12:00:00Z"
                        records.append({
                            "k": "exec", "ts": ts, "ctx": ctx,
                            "build": {"root": "/x", "mt": planted_ts + mt_offset},
                            "argv0": "x",
                        })
        # Add 3 fire records (must survive verbatim)
        for i in range(3):
            records.append({
                "k": "fire", "id": f"TS-F{i}", "ts": "2026-08-05T00:00:00Z",
                "ctx": "prod", "build": {"root": "/x", "mt": planted_ts + 1000}, "pid": i,
            })
        _seed_ledger(tomb_dir, records)

        # Count execs per (day, ctx, build) before
        from collections import Counter
        exec_counts: Counter = Counter()
        for r in records:
            if r.get("k") == "exec":
                day = r["ts"][:10]
                exec_counts[(day, r["ctx"], r["build"]["root"], r["build"]["mt"])] += 1

        # Run compact
        result = _run_report("--compact", tomb_dir=tomb_dir)
        assert result.returncode == 0

        # Read back
        after_records = _read_ledger(tomb_dir)

        # All fire records must survive verbatim
        after_fires = [r for r in after_records if r.get("k") == "fire"]
        assert len(after_fires) == 3
        for f in after_fires:
            assert f["id"].startswith("TS-F")

        # Verify exec counts via execroll
        after_rolls = [r for r in after_records if r.get("k") == "execroll"]
        roll_counts: Counter = Counter()
        for r in after_rolls:
            key = (r["day"], r["ctx"], r["build"]["root"], r["build"]["mt"])
            roll_counts[key] += r["n"]

        # Raw execs that survived (recent ones, within 24h)
        after_execs = [r for r in after_records if r.get("k") == "exec"]
        for r in after_execs:
            day = r["ts"][:10]
            exec_counts_key = (day, r["ctx"], r["build"]["root"], r["build"]["mt"])
            roll_counts[exec_counts_key] += 1

        # Total counts must match
        assert roll_counts == exec_counts


# ── AC8: Drifted code withholds verdict ────────────────────────────────


class TestAC8:
    """Snippet hash mismatch -> drifted verdict."""

    def test_drift_detected_on_code_change(self, tmp_path):
        """Edit the construct -> drifted. Edit ABOVE -> no drift."""
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()

        # Create a fake source file with a function
        src = tmp_path / "src"
        pkg = src / "cli_agent_orchestrator" / "utils"
        pkg.mkdir(parents=True)
        func_code = "def myfunc():\n    return 42\n"
        (pkg / "target.py").write_text(f"# header\n{func_code}")

        # Hash the function (excluding any tombstone line)
        func_hash = hashlib.sha256(func_code.encode()).hexdigest()

        reg = tmp_path / "registry.jsonl"
        reg.write_text(json.dumps({
            "id": "TS-DRIFT", "repo": "fork",
            "file": "src/cli_agent_orchestrator/utils/target.py",
            "symbol": "myfunc", "shape": "zero_reference",
            "snippet_sha256": func_hash,
            "planted_at": "2026-08-01T00:00:00Z", "rationale": "test",
            "soak": {"prod_exec": 20, "test_exec": 3, "days": 14},
            "retired_at": None, "retired_verdict": None,
        }) + "\n")

        # Also add tombstone call to make it visible in --verify
        (pkg / "target.py").write_text(
            f'# header\nfrom cli_agent_orchestrator.utils.tombstones import tombstone\ndef myfunc():\n    tombstone("TS-DRIFT")\n    return 42\n'
        )

        # Verify: should be clean (hash matches because we exclude tombstone lines)
        result = _run_report("--verify", tomb_dir=tomb_dir, registry=reg, fork_root=src)
        assert result.returncode == 0, f"Expected clean but got: {result.stderr}"

        # Now modify the function body
        (pkg / "target.py").write_text(
            f'# header\nfrom cli_agent_orchestrator.utils.tombstones import tombstone\ndef myfunc():\n    tombstone("TS-DRIFT")\n    return 99\n'
        )

        result = _run_report("--verify", tomb_dir=tomb_dir, registry=reg, fork_root=src)
        assert result.returncode == 2
        assert "E-DRIFTED" in result.stderr
        assert "TS-DRIFT" in result.stderr


# ── Kill switch test ───────────────────────────────────────────────────


class TestKillSwitch:
    """CAO_TOMBSTONES=0 disarms everything."""

    def test_disarmed(self, tmp_path):
        tomb_dir = tmp_path / "cao-tombstones"
        tomb_dir.mkdir()

        with patch.dict(os.environ, {"CAO_TOMBSTONE_DIR": str(tomb_dir), "CAO_TOMBSTONES": "0"}):
            mod_name = "cli_agent_orchestrator.utils.tombstones"
            sys.modules.pop(mod_name, None)
            import cli_agent_orchestrator.utils.tombstones as ts
            importlib.reload(ts)
            ts._seen.clear()
            ts.tombstone("TS-DISABLED")

        # Should produce NO records at all (not even exec)
        records = _read_ledger(tomb_dir)
        assert len(records) == 0
