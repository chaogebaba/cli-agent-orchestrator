"""F862 (#718) r6 — the send-enabled upload-complete gate's RE-PROBE arm (AC-9, D8).

Amendment C, "Code owed before certification", last row:

    A probe arm re-calibrating the send-enabled upload-complete gate — it
    currently rests on one live operator observation plus the r3 stall-DOM
    artifact, not a probe.

r3 changed the readiness predicate from "spinner gone AND send enabled" to
"send enabled" alone, on the strength of a single live observation that
unrelated page chrome keeps a spinner node mounted after the upload finishes.
That is an anecdote. r6 makes every upload record its full signal TRAJECTORY, so
runs accumulate samples, and reduces each to the one fact the change turns on:
``send_enabled_while_spinning``.

These tests pin the reduction offline, over synthetic trajectories, so the live
samples are read with a rule that was fixed in advance rather than fitted to
them afterwards.
"""

from __future__ import annotations

import json

import pytest

from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import Transport

pytestmark = pytest.mark.unit

_summarize = Transport.summarize_readiness_probe


def _s(t, spinning, enabled, present=True):
    return {"t": t, "spinning": spinning, "send_present": present, "send_enabled": enabled}


def test_the_r3_observation_is_the_discriminating_case(tmp_path):
    """The trajectory r3 rests on: send becomes enabled WHILE the spinner is
    still mounted. Under the old conjunction this upload would have waited to
    ``attach_timeout`` with the upload already complete."""
    trajectory = [_s(0.0, True, False), _s(1.0, True, False), _s(2.0, True, True)]
    out = _summarize(trajectory)
    assert out["send_enabled_while_spinning"] is True
    assert out["send_enabled_at"] == 2.0
    assert out["spinner_gone_at"] is None
    assert out["spinner_still_up_at_completion"] is True


def test_an_ordinary_upload_does_not_contradict_the_old_conjunction():
    """The other shape: the spinner clears BEFORE send enables. A sample like
    this is consistent with both predicates and is NOT evidence for the r3
    change — recording it as such is the calibration error this arm exists to
    prevent."""
    trajectory = [_s(0.0, True, False), _s(1.0, False, False), _s(2.0, False, True)]
    out = _summarize(trajectory)
    assert out["send_enabled_while_spinning"] is False
    assert out["spinner_gone_at"] == 1.0
    assert out["send_enabled_at"] == 2.0


def test_a_timed_out_upload_records_a_sample_with_no_enable():
    """An ``attach_timeout`` is a sample too — the arm must record the failures,
    or the calibration only ever sees successes."""
    trajectory = [_s(0.0, True, False), _s(30.0, True, False), _s(89.0, True, False)]
    out = _summarize(trajectory)
    assert out["send_enabled_at"] is None
    assert out["send_enabled_while_spinning"] is False
    assert out["samples"] == 3


def test_empty_trajectory_is_reduced_without_raising():
    out = _summarize([])
    assert out["samples"] == 0
    assert out["send_enabled_at"] is None
    assert out["spinner_still_up_at_completion"] is False


def test_recorder_writes_a_readable_sample_under_the_artifacts_dir(tmp_path, monkeypatch):
    """The recorder's own contract: one JSON file per upload, holding the summary
    and the raw trajectory, under ``CAO_ARTIFACTS_DIR/readiness-probe``. This is
    what makes "one live observation → three, recorded" checkable after a run."""
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    transport = Transport.__new__(Transport)  # no page needed for the recorder
    trajectory = [_s(0.0, True, False), _s(2.0, True, True)]
    transport._record_readiness_probe("bundle.txt", trajectory, outcome="complete")

    files = list((tmp_path / "readiness-probe").glob("readiness-*.json"))
    assert len(files) == 1
    sample = json.loads(files[0].read_text(encoding="utf-8"))
    assert sample["outcome"] == "complete"
    assert sample["filename"] == "bundle.txt"
    assert sample["summary"]["send_enabled_while_spinning"] is True
    assert sample["trajectory"] == trajectory


def test_recorded_sample_carries_no_page_text(tmp_path, monkeypatch):
    """AC-11 hygiene: the sample is booleans, elapsed times and a filename. A
    trajectory entry never carries page text, so the probe cannot become an
    accidental transcript."""
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    transport = Transport.__new__(Transport)
    transport._record_readiness_probe("bundle.txt", [_s(0.0, True, True)], outcome="complete")
    raw = list((tmp_path / "readiness-probe").glob("*.json"))[0].read_text(encoding="utf-8")
    payload = json.loads(raw)
    allowed_keys = {"t", "spinning", "send_present", "send_enabled"}
    for entry in payload["trajectory"]:
        assert set(entry) <= allowed_keys
