"""F619 (#475): terminal-log rotation/retention + E_DISK_LOW spawn guard.

Covers the four caps the incident demanded and the disk guard:
  - rotation at logs.max_file_mb (1 backup kept)
  - delete-time prune of a terminal's logs
  - startup prune by age, with a LIVE terminal's log left untouched
  - whole-dir total cap enforced oldest-first
  - E_DISK_LOW fires below the floor / does NOT fire above it
    (shutil.disk_usage monkeypatched)

Includes the two mutation checks from the brief as explicit assertions:
  - retention filter inverted -> RED  (test_mutation_retention_filter_inverted)
  - E_DISK_LOW threshold removed -> RED (test_mutation_disk_threshold_removed)
"""

import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services import cleanup_service, log_writer
from cli_agent_orchestrator.services.settings_service import get_disk_settings, get_logs_settings
from cli_agent_orchestrator.utils import disk_guard


@pytest.fixture
def settings_file(tmp_path):
    """Point the providers.toml loader at a temp file (mirrors test_settings_service).

    ``get_logs_settings`` / ``get_disk_settings`` read PROVIDER_DEFAULTS_FILE
    through ``get_provider_defaults`` with NO caching, so patching the path is
    sufficient; the tests write ``tmp_path / 'providers.toml'`` themselves.
    """
    fake_settings = tmp_path / "settings.json"
    with (
        patch(
            "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE",
            fake_settings,
        ),
        patch(
            "cli_agent_orchestrator.services.settings_service.PROVIDER_DEFAULTS_FILE",
            tmp_path / "providers.toml",
        ),
    ):
        yield fake_settings


# ---------------------------------------------------------------------------
# Config loaders
# ---------------------------------------------------------------------------
class TestConfigLoaders:
    def _write_toml(self, tmp_path, body):
        (tmp_path / "providers.toml").write_text(body, encoding="utf-8")

    def test_defaults_when_file_missing(self, settings_file):
        assert get_logs_settings() == {
            "max_file_mb": 50,
            "retention_hours": 24,
            "max_total_mb": 2048,
        }
        assert get_disk_settings() == {"min_free_gb": 5}

    def test_reads_logs_and_disk_sections(self, settings_file, tmp_path):
        self._write_toml(
            tmp_path,
            "[logs]\nmax_file_mb = 10\nretention_hours = 6\nmax_total_mb = 100\n"
            "[disk]\nmin_free_gb = 2\n",
        )
        assert get_logs_settings() == {
            "max_file_mb": 10,
            "retention_hours": 6,
            "max_total_mb": 100,
        }
        assert get_disk_settings() == {"min_free_gb": 2}

    @pytest.mark.parametrize("bad", ["0", "-5", '"nope"', "true"])
    def test_invalid_values_fall_back_to_default(self, settings_file, tmp_path, bad):
        # A non-positive / wrong-type value must NOT disable the cap.
        self._write_toml(tmp_path, f"[logs]\nmax_file_mb = {bad}\n[disk]\nmin_free_gb = {bad}\n")
        assert get_logs_settings()["max_file_mb"] == 50
        assert get_disk_settings()["min_free_gb"] == 5

    def test_partial_section_keeps_other_defaults(self, settings_file, tmp_path):
        self._write_toml(tmp_path, "[logs]\nretention_hours = 1\n")
        s = get_logs_settings()
        assert s["retention_hours"] == 1
        assert s["max_file_mb"] == 50
        assert s["max_total_mb"] == 2048


# ---------------------------------------------------------------------------
# Rotation (log_writer._rotate_if_needed / _write)
# ---------------------------------------------------------------------------
class TestRotation:
    def test_rotate_rolls_to_backup_at_cap(self, tmp_path):
        path = tmp_path / "term.log"
        path.write_text("x" * 100, encoding="utf-8")
        log_writer._rotate_if_needed(path, max_bytes=50)
        # active moved to .log.1; active recreated fresh on next write
        assert (tmp_path / "term.log.1").read_text() == "x" * 100
        assert not path.exists()

    def test_no_rotate_below_cap(self, tmp_path):
        path = tmp_path / "term.log"
        path.write_text("x" * 10, encoding="utf-8")
        log_writer._rotate_if_needed(path, max_bytes=50)
        assert path.exists()
        assert not (tmp_path / "term.log.1").exists()

    def test_rotate_keeps_only_one_backup(self, tmp_path):
        path = tmp_path / "term.log"
        (tmp_path / "term.log.1").write_text("OLD", encoding="utf-8")
        path.write_text("y" * 100, encoding="utf-8")
        log_writer._rotate_if_needed(path, max_bytes=50)
        # old backup replaced, not accumulated
        assert (tmp_path / "term.log.1").read_text() == "y" * 100
        assert not (tmp_path / "term.log.2").exists()

    def test_zero_cap_disables_rotation(self, tmp_path):
        path = tmp_path / "term.log"
        path.write_text("z" * 100, encoding="utf-8")
        log_writer._rotate_if_needed(path, max_bytes=0)
        assert path.exists()
        assert not (tmp_path / "term.log.1").exists()

    def test_write_rotates_then_appends(self, tmp_path):
        path = tmp_path / "term.log"
        path.write_text("A" * 60, encoding="utf-8")
        log_writer.LogWriter._write(path, "B" * 5, max_bytes=50)
        assert (tmp_path / "term.log.1").read_text() == "A" * 60
        assert path.read_text() == "B" * 5


# ---------------------------------------------------------------------------
# Retention: delete-prune, startup age-prune (live untouched), total cap
# ---------------------------------------------------------------------------
@pytest.fixture
def term_log_dir(tmp_path, monkeypatch):
    d = tmp_path / "terminal"
    d.mkdir()
    monkeypatch.setattr(cleanup_service, "TERMINAL_LOG_DIR", d)
    monkeypatch.setattr(disk_guard, "TERMINAL_LOG_DIR", d)
    return d


def _age(path: Path, hours: float) -> None:
    ts = (cleanup_service._utcnow() - timedelta(hours=hours)).timestamp()
    os.utime(path, (ts, ts))


class TestDeletePrune:
    def test_prune_removes_log_and_backup(self, term_log_dir):
        (term_log_dir / "abc.log").write_text("data")
        (term_log_dir / "abc.log.1").write_text("old")
        removed = cleanup_service.prune_terminal_log("abc")
        assert removed == 2
        assert not (term_log_dir / "abc.log").exists()
        assert not (term_log_dir / "abc.log.1").exists()

    def test_prune_missing_is_noop(self, term_log_dir):
        assert cleanup_service.prune_terminal_log("ghost") == 0

    def test_prune_leaves_other_terminals(self, term_log_dir):
        (term_log_dir / "abc.log").write_text("a")
        (term_log_dir / "xyz.log").write_text("b")
        cleanup_service.prune_terminal_log("abc")
        assert (term_log_dir / "xyz.log").exists()


class TestStartupAgePrune:
    def _patch_live(self, monkeypatch, live_ids):
        monkeypatch.setattr(cleanup_service, "_live_terminal_ids", lambda: set(live_ids))

    def test_old_dead_terminal_log_pruned(self, term_log_dir, monkeypatch, settings_file, tmp_path):
        (tmp_path / "providers.toml").write_text("[logs]\nretention_hours = 24\n")
        old = term_log_dir / "dead.log"
        old.write_text("stale")
        _age(old, hours=48)
        self._patch_live(monkeypatch, [])
        counters = cleanup_service.prune_terminal_logs_at_startup()
        assert not old.exists()
        assert counters["pruned_age"] == 1

    def test_recent_dead_terminal_log_kept(self, term_log_dir, monkeypatch, settings_file):
        recent = term_log_dir / "dead.log"
        recent.write_text("fresh")  # mtime = now
        self._patch_live(monkeypatch, [])
        cleanup_service.prune_terminal_logs_at_startup()
        assert recent.exists()

    def test_live_terminal_log_never_pruned_even_if_old(
        self, term_log_dir, monkeypatch, settings_file
    ):
        live = term_log_dir / "alive.log"
        live.write_text("running")
        _age(live, hours=999)
        self._patch_live(monkeypatch, ["alive"])  # DB still has this terminal
        counters = cleanup_service.prune_terminal_logs_at_startup()
        assert live.exists(), "a LIVE terminal's log must never be age-pruned"
        assert counters["pruned_age"] == 0

    def test_mutation_retention_filter_inverted(
        self, term_log_dir, monkeypatch, settings_file, tmp_path
    ):
        """Mutant: if the age filter were inverted (prune NEW, keep OLD) this is RED.

        A fresh dead-terminal log must SURVIVE and an old one must be DELETED.
        An inverted `st_mtime < cutoff` predicate flips both assertions.
        """
        (tmp_path / "providers.toml").write_text("[logs]\nretention_hours = 24\n")
        old = term_log_dir / "old.log"
        new = term_log_dir / "new.log"
        old.write_text("o")
        new.write_text("n")
        _age(old, hours=100)
        _age(new, hours=1)
        self._patch_live(monkeypatch, [])
        cleanup_service.prune_terminal_logs_at_startup()
        assert not old.exists()
        assert new.exists()


class TestStartupTotalCap:
    def _patch_live(self, monkeypatch, live_ids):
        monkeypatch.setattr(cleanup_service, "_live_terminal_ids", lambda: set(live_ids))

    def test_total_cap_deletes_oldest_first(
        self, term_log_dir, monkeypatch, settings_file, tmp_path
    ):
        # Cap = 1 MB total; three 512KB recent dead logs => must drop oldest until under.
        (tmp_path / "providers.toml").write_text(
            "[logs]\nretention_hours = 240\nmax_total_mb = 1\n"
        )
        blob = "x" * (512 * 1024)
        a = term_log_dir / "a.log"
        b = term_log_dir / "b.log"
        c = term_log_dir / "c.log"
        for f in (a, b, c):
            f.write_text(blob)
        _age(a, hours=10)  # oldest
        _age(b, hours=5)
        _age(c, hours=1)  # newest
        self._patch_live(monkeypatch, [])
        counters = cleanup_service.prune_terminal_logs_at_startup()
        # 3 * 512KB = 1.5MB > 1MB cap; oldest (a) dropped first, brings total to 1MB.
        assert not a.exists(), "oldest must be deleted first"
        assert b.exists()
        assert c.exists()
        assert counters["pruned_total_cap"] >= 1

    def test_total_cap_skips_live_terminal(
        self, term_log_dir, monkeypatch, settings_file, tmp_path
    ):
        (tmp_path / "providers.toml").write_text(
            "[logs]\nretention_hours = 240\nmax_total_mb = 1\n"
        )
        blob = "x" * (900 * 1024)
        live = term_log_dir / "live.log"
        dead = term_log_dir / "dead.log"
        live.write_text(blob)
        dead.write_text(blob)
        _age(live, hours=100)  # older, but LIVE
        _age(dead, hours=1)
        self._patch_live(monkeypatch, ["live"])
        cleanup_service.prune_terminal_logs_at_startup()
        # live is never counted/deleted; only the dead one can be dropped.
        assert live.exists()


# ---------------------------------------------------------------------------
# E_DISK_LOW spawn guard
# ---------------------------------------------------------------------------
class TestDiskGuard:
    def _usage(self, free_gb):
        total = 100 * disk_guard._BYTES_PER_GB
        free = int(free_gb * disk_guard._BYTES_PER_GB)
        return SimpleNamespace(total=total, used=total - free, free=free)

    def test_fires_below_floor(self, tmp_path, monkeypatch, settings_file):
        (tmp_path / "providers.toml").write_text("[disk]\nmin_free_gb = 5\n")
        monkeypatch.setattr(disk_guard, "TERMINAL_LOG_DIR", tmp_path)
        monkeypatch.setattr(disk_guard.shutil, "disk_usage", lambda p: self._usage(3.1))
        result = disk_guard.check_spawn_disk(str(tmp_path))
        assert result is not None
        assert result.startswith("E_DISK_LOW:")
        assert "3.1GB" in result

    def test_does_not_fire_above_floor(self, tmp_path, monkeypatch, settings_file):
        (tmp_path / "providers.toml").write_text("[disk]\nmin_free_gb = 5\n")
        monkeypatch.setattr(disk_guard, "TERMINAL_LOG_DIR", tmp_path)
        monkeypatch.setattr(disk_guard.shutil, "disk_usage", lambda p: self._usage(50))
        assert disk_guard.check_spawn_disk(str(tmp_path)) is None

    def test_names_the_offending_path(self, tmp_path, monkeypatch, settings_file):
        (tmp_path / "providers.toml").write_text("[disk]\nmin_free_gb = 5\n")
        monkeypatch.setattr(disk_guard, "TERMINAL_LOG_DIR", tmp_path)
        monkeypatch.setattr(disk_guard.shutil, "disk_usage", lambda p: self._usage(1))
        wt = tmp_path / "wt"
        wt.mkdir()
        result = disk_guard.check_spawn_disk(str(wt))
        assert str(wt) in result or str(tmp_path) in result

    def test_startup_warning_logs_and_returns(self, tmp_path, monkeypatch, settings_file, caplog):
        (tmp_path / "providers.toml").write_text("[disk]\nmin_free_gb = 5\n")
        monkeypatch.setattr(disk_guard, "TERMINAL_LOG_DIR", tmp_path)
        monkeypatch.setattr(disk_guard.shutil, "disk_usage", lambda p: self._usage(2))
        with caplog.at_level("WARNING"):
            result = disk_guard.warn_if_disk_low_at_startup(str(tmp_path))
        assert result is not None and result.startswith("E_DISK_LOW:")
        assert any("E_DISK_LOW" in r.message for r in caplog.records)

    def test_mutation_disk_threshold_removed(self, tmp_path, monkeypatch, settings_file):
        """Mutant: remove the `free < floor` threshold (always None) -> RED here.

        With only 1GB free and a 5GB floor, the guard MUST return a string. A
        mutant that dropped the comparison and returned None unconditionally
        would fail this assertion.
        """
        (tmp_path / "providers.toml").write_text("[disk]\nmin_free_gb = 5\n")
        monkeypatch.setattr(disk_guard, "TERMINAL_LOG_DIR", tmp_path)
        monkeypatch.setattr(disk_guard.shutil, "disk_usage", lambda p: self._usage(1))
        assert disk_guard.check_spawn_disk(str(tmp_path)) is not None


# ---------------------------------------------------------------------------
# Ordering: snapshot/scrollback capture MUST happen BEFORE prune_terminal_log
# ---------------------------------------------------------------------------
class TestDeletePruneOrdering:
    """Guards the delete-path invariant: the .log is captured into the restore
    snapshot BEFORE it is pruned.

    The prune call sits just after the scrollback/snapshot block in
    ``_delete_terminal_under_lease``. If a refactor moved the prune ABOVE that
    block, the pipe-pane byte log would be deleted before it could be captured
    — silently losing the very evidence the snapshot exists to preserve — and
    the plain snapshot test would NOT notice (it never reads the .log).

    This test makes the two observably dependent: the mocked ``get_history``
    (which feeds the scrollback snapshot) reads the on-disk ``<id>.log``. In the
    correct order the log is still present, so the scrollback captures its
    content; under the prune-first mutation the log is already gone, so the
    captured scrollback is empty -> the assertion below goes RED.
    """

    def test_scrollback_captures_log_before_prune(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import cleanup_service as cs
        from cli_agent_orchestrator.services import terminal_service as ts

        term_id = "abc12345"
        log_content = "PIPE-PANE-LOG-BYTES-marker-line1\nmarker-line2\n"
        (tmp_path / f"{term_id}.log").write_text(log_content, encoding="utf-8")

        # BOTH modules resolve the log dir to the SAME real tmp dir: the snapshot
        # writes via terminal_service.TERMINAL_LOG_DIR, the prune reads via
        # cleanup_service.TERMINAL_LOG_DIR. Real Paths (not a MagicMock) so
        # os.replace/unlink/stat behave normally.
        monkeypatch.setattr(ts, "TERMINAL_LOG_DIR", tmp_path)
        monkeypatch.setattr(cs, "TERMINAL_LOG_DIR", tmp_path)

        # get_history reads the on-disk <id>.log — this is what couples snapshot
        # ordering to the prune. Missing file (prune ran first) -> "".
        def _get_history(session, window, strip_escapes=True, full_history=True):
            p = tmp_path / f"{term_id}.log"
            return p.read_text(encoding="utf-8") if p.exists() else ""

        backend = MagicMock()
        backend.get_history.side_effect = _get_history
        backend.get_pane_working_directory.return_value = "/home/user/project"

        meta = {
            "id": term_id,
            "tmux_session": "cao-test",
            "tmux_window": "dev-abc1",
            "provider": "kiro_cli",
            "agent_profile": "developer",
            "allowed_tools": None,
        }

        with (
            patch("cli_agent_orchestrator.backends.registry._backend", backend),
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                return_value=meta,
            ),
            patch("cli_agent_orchestrator.services.terminal_service.provider_manager"),
            patch(
                "cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent",
                return_value={"terminal_deleted": True, "intent_deleted": False},
            ),
        ):
            ts._delete_terminal_core(term_id)

        # Snapshot ran BEFORE prune: scrollback captured the log content...
        scrollback = (tmp_path / f"{term_id}.scrollback").read_text(encoding="utf-8")
        assert scrollback == log_content, (
            "scrollback must capture the .log BEFORE prune_terminal_log removes it; "
            "an empty scrollback means the prune ran first (ordering mutation)"
        )
        # ...and the prune DID subsequently remove the .log (feature still works).
        assert not (tmp_path / f"{term_id}.log").exists()
