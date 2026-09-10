"""F862 (#718) — browser-ownership + runtime tests (D5, AC-12/AC-13).

Exercises the profile lock + review lease and the attach-identity refusals with
a real temp filesystem (no browser). Follows the blueprint's browser-ownership
test class: flagged-profile refusal, live-owner lock, CDP port conflict, and the
serialize/owner-only-stop contract — all provable without launching Chromium.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError, RunnerErrorCode
from cli_agent_orchestrator.chatgpt_web_runner.runtime import (
    ProfileLock,
    assert_attach_identity,
    assert_cdp_port_allowed,
    build_launch_options,
    pin_fingerprint_seed,
    resolve_profile_dir,
)

pytestmark = pytest.mark.unit

APPROVED_ROOT = "/data/cao-scratch/chatgpt-web"


@pytest.fixture()
def profile_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # A per-account dir UNDER the approved root (D5). Uses the real approved root
    # so resolve_profile_dir accepts it; a unique subdir keeps tests isolated.
    base = Path(APPROVED_ROOT) / "test-profiles"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"p-{os.getpid()}-{id(tmp_path_factory)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_resolve_profile_dir_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("C2C_DRIVER_PROFILE", raising=False)
    with pytest.raises(RunnerError) as ei:
        resolve_profile_dir()
    assert ei.value.code is RunnerErrorCode.ACCESS_DENIED


def test_resolve_profile_dir_refuses_outside_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("C2C_DRIVER_PROFILE", "/home/chao/.config/google-chrome")
    with pytest.raises(RunnerError) as ei:
        resolve_profile_dir()
    assert ei.value.code is RunnerErrorCode.ACCESS_DENIED


def test_resolve_profile_dir_refuses_grok_web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("C2C_DRIVER_PROFILE", "/home/chao/.local/share/grok-web/profile")
    with pytest.raises(RunnerError) as ei:
        resolve_profile_dir()
    assert ei.value.code is RunnerErrorCode.ACCESS_DENIED


def test_resolve_profile_dir_accepts_under_root(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path
) -> None:
    monkeypatch.setenv("C2C_DRIVER_PROFILE", str(profile_dir))
    assert resolve_profile_dir() == profile_dir.resolve()


def test_pin_fingerprint_seed_is_five_digits_and_stable(profile_dir: Path) -> None:
    seed = pin_fingerprint_seed(profile_dir)
    assert seed.isdigit() and len(seed) == 5
    # Stable across calls — one seed per profile (D5).
    assert pin_fingerprint_seed(profile_dir) == seed
    seed_file = profile_dir / "fingerprint-seed"
    assert seed_file.exists()
    # mode 0600
    assert (seed_file.stat().st_mode & 0o777) == 0o600


def test_launch_options_headful_humanize_false(profile_dir: Path) -> None:
    opts = build_launch_options(profile_dir, "12345")
    assert opts["headless"] is False
    assert opts["humanize"] is False
    assert opts["fingerprint_seed"] == "12345"


# --- AC-13: CDP port + attach-identity refusals ---------------------------------
def test_ac13_forbidden_cdp_port_refused() -> None:
    with pytest.raises(RunnerError) as ei:
        assert_cdp_port_allowed(9337)  # grok-web daemon
    assert ei.value.code is RunnerErrorCode.ACCESS_DENIED


def test_ac13_allowed_cdp_port_ok() -> None:
    assert_cdp_port_allowed(9340)  # no raise


def test_ac13_attach_pid_mismatch_refused() -> None:
    with pytest.raises(RunnerError):
        assert_attach_identity(
            recorded_pid=100,
            observed_pid=200,
            recorded_start_time=1.0,
            observed_start_time=1.0,
            recorded_profile="/data/cao-scratch/chatgpt-web/profile",
            observed_profile="/data/cao-scratch/chatgpt-web/profile",
        )


def test_ac13_attach_start_time_mismatch_refused() -> None:
    with pytest.raises(RunnerError):
        assert_attach_identity(
            recorded_pid=100,
            observed_pid=100,
            recorded_start_time=1.0,
            observed_start_time=99.0,
            recorded_profile="/data/cao-scratch/chatgpt-web/profile",
            observed_profile="/data/cao-scratch/chatgpt-web/profile",
        )


def test_ac13_attach_profile_mismatch_refused() -> None:
    with pytest.raises(RunnerError):
        assert_attach_identity(
            recorded_pid=100,
            observed_pid=100,
            recorded_start_time=1.0,
            observed_start_time=1.0,
            recorded_profile="/data/cao-scratch/chatgpt-web/profile",
            observed_profile="/data/cao-scratch/chatgpt-web/other",
        )


def test_ac13_attach_all_agree_ok() -> None:
    assert_attach_identity(
        recorded_pid=100,
        observed_pid=100,
        recorded_start_time=1.0,
        observed_start_time=1.2,
        recorded_profile="/data/cao-scratch/chatgpt-web/profile",
        observed_profile="/data/cao-scratch/chatgpt-web/profile",
    )


# --- AC-12: profile lock serialization + owner-only release ---------------------
def test_ac12_second_worker_queues_then_reports_busy(profile_dir: Path) -> None:
    first = ProfileLock(profile_dir, "term-A")
    first.acquire()
    assert first.owns()
    second = ProfileLock(profile_dir, "term-B")
    with pytest.raises(RunnerError) as ei:
        # Short deadline so the test does not block; the second worker never
        # consumes a review turn — it reports busy (nothing-sent).
        second.acquire(queue_deadline_s=0.1, poll_s=0.05)
    assert ei.value.code is RunnerErrorCode.SUBMIT_UNKNOWN
    first.release()
    assert not first.owns()


def test_ac12_release_only_own_lease(profile_dir: Path) -> None:
    owner = ProfileLock(profile_dir, "term-A")
    owner.acquire()
    # A different worker object that never acquired must not stop the browser.
    intruder = ProfileLock(profile_dir, "term-B")
    assert intruder.owns() is False
    intruder.release()  # no-op: does not own
    # Owner still holds the lease.
    assert owner.owns() is True
    owner.release()


def test_ac13_live_owner_lock_not_deleted(profile_dir: Path) -> None:
    # A live owner (this very process) holds the lock; a contender must NOT
    # delete it — it queues and reports busy instead (AC-13).
    owner = ProfileLock(profile_dir, "term-A")
    owner.acquire()
    lock_file = profile_dir / ".cao-review.lock"
    assert lock_file.exists()
    contender = ProfileLock(profile_dir, "term-B")
    with pytest.raises(RunnerError):
        contender.acquire(queue_deadline_s=0.1, poll_s=0.05)
    # Lock still present and owned by A.
    assert lock_file.exists()
    assert owner.owns()
    owner.release()
