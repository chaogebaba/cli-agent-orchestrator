"""F643 (#498): forked-rollout fallback for resumed codex sessions.

Trigger (distinct from F640's barrier fire-gate, and from F435's ordinary
"paste never submitted"):

A "fresh" codex terminal in CAO is launched by RESUMING the seed rollout
(``seed_resume_identity`` runs ``codex exec ... 'Reply ... SEED_OK'`` to mint a
native rollout, then the seat launches ``codex --resume <seed_uuid>``).
``provider_session_id`` is pinned to the SEED uuid at spawn, so
``_resolve_rollout_file`` globs ``rollout-*{seed_uuid}*.jsonl`` and returns the
STALE seed file (~49KB of preamble). But modern Codex resume can FORK the
transcript into a brand-NEW rollout file with a NEW uuid (openai/codex
``InitialHistory::Forked`` — "a new id and file"). The dispatched task turn is
written to that NEW file, so ``_rollout_has_user_event`` scanning the seed file
after its offset never matches → F435 declares delivery "structurally
unconfirmed" → deferred_init retries → composer-unreadable → teardown.

The observed journal chain (2026-08-30 18:40, terminal acc543b1):
  pane_liveness rule-3a vetoed (diagnostic; withholds nothing)
    → F435 submit-verify 3x "no stuck chip visible; re-checking rollout"
    → "rollout JSONL has no matching user-turn record after offset 49753"
    → deferred_init retries → draft_guard "Composer state is unreadable"
    → exposure_crossed=True → deferred_init_internal teardown.

These tests reproduce the FORK at the ``verify_submission_after_send`` seam and
assert the fallback confirms delivery from the sibling file, plus the two guards
that keep the fallback from confirming an unrelated / pre-existing turn.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.codex import (
    CodexProvider,
    CodexSubmitBaseline,
    CodexSubmitStuckError,
)

METADATA_BASE = {"tmux_session": "sess", "tmux_window": "win"}
FOOTER = "  ~/VScode_Projects/cli-subagents · main · gpt-5.6-sol high"

# The seed uuid is what CAO pins as provider_session_id for a resumed "fresh"
# terminal; the fork uuid is what codex actually writes the live turn under.
SEED_UUID = "seeduuid-0000-0000-0000-000000000000"
FORK_UUID = "forkuuid-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("cli_agent_orchestrator.providers.codex.time.sleep", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _fast_monotonic():
    counter = {"t": 0.0}

    def _mono():
        counter["t"] += 100.0
        return counter["t"]

    with patch("cli_agent_orchestrator.providers.codex.time.monotonic", side_effect=_mono):
        yield


def _provider() -> CodexProvider:
    return CodexProvider("term1234", "sess", "win")


def _metadata(session_uuid: str = SEED_UUID) -> dict[str, Any]:
    return {**METADATA_BASE, "provider_session_id": session_uuid}


def _backend_no_chip() -> MagicMock:
    """Backend whose pane shows an idle composer (no stuck chip)."""
    backend = MagicMock()
    pane = "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"
    backend.get_history.return_value = pane
    return backend


def _enter_calls(backend: MagicMock) -> int:
    return sum(
        1
        for call in backend.send_special_key.call_args_list
        if "Enter" in call.args or call.kwargs.get("key") == "Enter"
    )


def _write_session_meta(rollout_path: Path, uuid: str) -> None:
    with rollout_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"id": uuid}}) + "\n")


def _write_user_event(rollout_path: Path, message: str) -> None:
    record = {"type": "event_msg", "payload": {"type": "user_message", "message": message}}
    with rollout_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


@pytest.fixture()
def patched_codex_home(tmp_path: Path):
    with patch(
        "cli_agent_orchestrator.providers.codex._resolved_codex_home",
        return_value=tmp_path,
    ):
        yield tmp_path


def _seed_rollout(home: Path, *, preamble_bytes: int = 49_000) -> Path:
    """Create the stale SEED rollout with a large preamble, as codex resume sees.

    The preamble simulates the ~49KB agent-profile + SEED_OK exchange that makes
    the pinned-file byte offset large (offset 49753 in the incident).
    """
    sessions = home / "sessions" / "2026" / "08" / "30"
    sessions.mkdir(parents=True, exist_ok=True)
    seed = sessions / f"rollout-2026-08-30T18-00-00-{SEED_UUID}.jsonl"
    _write_session_meta(seed, SEED_UUID)
    # Pad with a large assistant record so the offset is realistically big.
    pad = {"type": "event_msg", "payload": {"type": "assistant_message", "message": "x" * preamble_bytes}}
    with seed.open("a", encoding="utf-8") as f:
        f.write(json.dumps(pad) + "\n")
    return seed


def _fork_rollout(home: Path) -> Path:
    """Create the live FORKED rollout file (new uuid, empty but for session_meta)."""
    sessions = home / "sessions" / "2026" / "08" / "30"
    sessions.mkdir(parents=True, exist_ok=True)
    fork = sessions / f"rollout-2026-08-30T18-40-45-{FORK_UUID}.jsonl"
    _write_session_meta(fork, FORK_UUID)
    return fork


def _baseline_pinning_seed(seed: Path, *, baseline_wall: float) -> CodexSubmitBaseline:
    """A baseline that pins the STALE seed file at its full current offset."""
    return CodexSubmitBaseline(
        rollout_path=seed,
        rollout_offset=seed.stat().st_size,
        baseline_wall=baseline_wall,
        captured_ok=True,
    )


# ===========================================================================
# THE F643 TRIGGER
# ===========================================================================


class TestForkedRolloutFallback:
    def test_task_delivered_to_forked_file_confirms(
        self, patched_codex_home: Path
    ):
        """Regression: task turn lands in the FORKED file, not the pinned seed.

        This is the exact F643 chain. WITHOUT the fallback this raises
        CodexSubmitStuckError('structurally unconfirmed'); WITH it, delivery is
        confirmed via the sibling file and no recovery Enter is sent.
        """
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        baseline = _baseline_pinning_seed(seed, baseline_wall=baseline_wall)

        # The dispatched task turn is written to a NEW forked file, AFTER baseline.
        msg = "Root-cause and fix F643 per the assignment [callback: terminal 9064394e]"
        fork = _fork_rollout(home)
        _write_user_event(fork, msg)
        # Ensure fork mtime is strictly at/after baseline.
        future = baseline_wall + 5
        os.utime(fork, (future, future))
        # The seed predates the dispatch.
        past = baseline_wall - 100
        os.utime(seed, (past, past))

        provider = _provider()
        backend = _backend_no_chip()
        # Must NOT raise, and must not send a recovery Enter.
        provider.verify_submission_after_send(
            _metadata(), backend, message=msg, baseline=baseline
        )
        assert _enter_calls(backend) == 0

    def test_pinned_seed_only_still_raises(self, patched_codex_home: Path):
        """Control: no forked file at all → genuine failure still raises.

        Guards against the fallback masking a real never-delivered dispatch.
        """
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        os.utime(seed, (baseline_wall - 100, baseline_wall - 100))
        baseline = _baseline_pinning_seed(seed, baseline_wall=baseline_wall)

        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(
                _metadata(), backend, message="Never delivered", baseline=baseline
            )

    def test_preexisting_sibling_before_baseline_does_not_confirm(
        self, patched_codex_home: Path
    ):
        """Guard 1: a sibling whose mtime PREDATES the dispatch must not confirm.

        Even if an OLD sibling happens to contain the same text (e.g. a prior
        run of an identical task), a file untouched since before this dispatch
        cannot hold this dispatch's turn.
        """
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        msg = "An identical task message from a previous run"

        # Pre-existing sibling with the SAME text but stamped BEFORE baseline.
        stale_sibling = _fork_rollout(home)
        _write_user_event(stale_sibling, msg)
        past = baseline_wall - 50
        os.utime(stale_sibling, (past, past))
        os.utime(seed, (past, past))

        baseline = _baseline_pinning_seed(seed, baseline_wall=baseline_wall)
        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )

    def test_unset_baseline_wall_disables_fallback(self, patched_codex_home: Path):
        """Guard 2: baseline_wall==0.0 (unset) → fallback is off, fail-safe.

        If the baseline predates this fix (no wall captured), the fallback must
        NOT fire — it would otherwise scan with an unbounded window.
        """
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline = CodexSubmitBaseline(
            rollout_path=seed,
            rollout_offset=seed.stat().st_size,
            baseline_wall=0.0,  # unset
            captured_ok=True,
        )
        msg = "Task that did land in a forked file"
        fork = _fork_rollout(home)
        _write_user_event(fork, msg)
        now = time.time() + 5
        os.utime(fork, (now, now))

        provider = _provider()
        backend = _backend_no_chip()
        # baseline_wall unset → fallback disabled → still raises.
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )

    def test_direct_forked_match_helper(self, patched_codex_home: Path):
        """Unit-level: _forked_rollout_match finds the sibling; guards hold."""
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        os.utime(seed, (baseline_wall - 100, baseline_wall - 100))
        msg = "unit level forked match probe"
        fork = _fork_rollout(home)
        _write_user_event(fork, msg)
        os.utime(fork, (baseline_wall + 5, baseline_wall + 5))

        provider = _provider()
        # Positive: matches the forked sibling.
        assert provider._forked_rollout_match(seed, msg, baseline_wall) is True
        # Negative: empty message / unset wall / wrong content.
        assert provider._forked_rollout_match(seed, "", baseline_wall) is False
        assert provider._forked_rollout_match(seed, msg, 0.0) is False
        assert provider._forked_rollout_match(seed, "different text", baseline_wall) is False
