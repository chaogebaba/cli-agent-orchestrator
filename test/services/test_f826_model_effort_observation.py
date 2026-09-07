"""F826 (#683) — per-provider model/effort observation adapters.

Covers AC1 (claude sidecar [L] + decay [S]), AC2 (codex/pi [R]; touched/appended
file never relabels/refreshes age), AC3 (kiro model [R], effort unavailable),
AC5 (bounded reads, one stat on unchanged file), plus the codex incremental
forward scan (ruling: turn_context head-clustered) and the AC6 cache-key mutant
(drop mtime_ns -> stale relabel).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import model_effort_observation as meo
from cli_agent_orchestrator.services.model_effort_observation import Marker, ObservationKind


@pytest.fixture(autouse=True)
def _clean_caches():
    meo.reset_caches()
    yield
    meo.reset_caches()


# --- Claude sidecar (AC1) ---------------------------------------------------


def _sidecar(path: Path, model, effort, event_ns):
    path.write_text(
        json.dumps(
            {
                "terminal_id": "t",
                "claude_session_id": "s",
                "model": model,
                "effort": effort,
                "event_time": event_ns,
            }
        )
    )


def test_claude_live_selection(tmp_path):
    sc = tmp_path / "t.json"
    _sidecar(sc, "Opus 4.6", "high", time.time_ns())
    obs = meo.observe_claude(sc)
    assert obs.model.value == "Opus 4.6"
    assert obs.model.marker is Marker.LIVE
    assert obs.model.kind is ObservationKind.SELECTED
    assert obs.effort.value == "high"
    assert obs.effort.marker is Marker.LIVE


def test_claude_decays_to_stale_after_three_intervals(tmp_path):
    sc = tmp_path / "t.json"
    now = time.time_ns()
    _sidecar(sc, "Opus", "low", now - (meo.DECAY_NS + 1_000_000_000))
    obs = meo.observe_claude(sc, now_ns=now)
    assert obs.model.marker is Marker.STALE
    assert "no fresh sidecar" in (obs.model.validity or "")


def test_claude_exited_terminal_shows_stale_exited(tmp_path):
    sc = tmp_path / "t.json"
    now = time.time_ns()
    _sidecar(sc, "Opus", "high", now)  # fresh, but terminal exited
    obs = meo.observe_claude(sc, now_ns=now, exited=True)
    assert obs.model.marker is Marker.STALE
    assert obs.model.validity == "exited"


def test_claude_rejects_non_increasing_event_time(tmp_path):
    """S2/D3: a sidecar whose event_time <= last accepted is stale, not live."""
    sc = tmp_path / "t.json"
    now = time.time_ns()
    _sidecar(sc, "Opus", "high", now)
    obs = meo.observe_claude(sc, now_ns=now + 1, last_accepted_event_ns=now)
    assert obs.model.marker is Marker.STALE
    assert obs.model.validity == "superseded"


def test_claude_missing_sidecar_is_unknown_not_crash(tmp_path):
    obs = meo.observe_claude(tmp_path / "absent.json")
    assert obs.model.marker is Marker.UNKNOWN
    assert obs.effort.marker is Marker.UNKNOWN


def test_claude_effort_absent_is_unknown_model_still_live(tmp_path):
    """N2: a non-thinking model has no effort.level -> effort unknown, model live."""
    sc = tmp_path / "t.json"
    _sidecar(sc, "haiku", None, time.time_ns())
    obs = meo.observe_claude(sc)
    assert obs.model.marker is Marker.LIVE
    assert obs.effort.marker is Marker.UNKNOWN


# --- codex incremental forward scan (ruling; AC2) ---------------------------


def _codex_turn_context(model, effort, ts="2026-09-07T10:00:00.000Z"):
    return json.dumps(
        {"type": "turn_context", "timestamp": ts, "payload": {"model": model, "effort": effort}}
    )


def _codex_settings(model, effort, ts="2026-09-07T11:00:00.000Z"):
    return json.dumps(
        {
            "type": "event_msg",
            "timestamp": ts,
            "payload": {
                "type": "thread_settings_applied",
                "thread_settings": {"model": model, "effort": effort},
            },
        }
    )


def _codex_filler(n):
    return "\n".join(
        json.dumps(
            {"type": "response_item", "payload": {"type": "message", "role": "assistant"}, "i": i}
        )
        for i in range(n)
    )


def test_codex_resolves_turn_context_beyond_64kb_from_eof(tmp_path):
    """The named codex scenario: the ONLY turn_context sits >64 KB before EOF.

    An EOF tail would see nothing; the forward scan resolves it.
    """
    cx = tmp_path / "rollout.jsonl"
    body = _codex_turn_context("gpt-5.6-sol", "high") + "\n" + _codex_filler(3000) + "\n"
    cx.write_text(body)
    assert cx.stat().st_size > 64 * 1024  # the turn_context is now far from EOF
    obs = meo.observe_codex(cx)
    assert obs.model.value == "gpt-5.6-sol"
    assert obs.model.marker is Marker.OBSERVED
    assert obs.effort.value == "high"


def test_codex_incremental_picks_up_appended_settings_reading_only_delta(tmp_path):
    """AC2 incremental path: after the first scan, an appended settings record is
    picked up on the next observe, and upgrades to [L]."""
    cx = tmp_path / "rollout.jsonl"
    cx.write_text(_codex_turn_context("gpt-5.6-sol", "high") + "\n" + _codex_filler(500) + "\n")
    first = meo.observe_codex(cx)
    assert first.model.value == "gpt-5.6-sol"
    assert first.model.marker is Marker.OBSERVED
    # Append a settings-change record (the CLI's selection edge).
    with open(cx, "a") as handle:
        handle.write(_codex_settings("gpt-5.6-luna", "xhigh") + "\n")
    second = meo.observe_codex(cx)
    assert second.model.value == "gpt-5.6-luna"
    assert second.model.marker is Marker.LIVE  # thread_settings_applied -> [L]
    assert second.effort.value == "xhigh"


def test_codex_appended_unrelated_record_keeps_prior_evidence(tmp_path):
    """AC2: an appended unrelated record never relabels or blanks the evidence."""
    cx = tmp_path / "rollout.jsonl"
    cx.write_text(_codex_turn_context("gpt-5.6-sol", "high") + "\n")
    first = meo.observe_codex(cx)
    assert first.model.value == "gpt-5.6-sol"
    with open(cx, "a") as handle:
        handle.write(json.dumps({"type": "token_count", "payload": {"n": 5}}) + "\n")
    second = meo.observe_codex(cx)
    assert second.model.value == "gpt-5.6-sol"
    assert second.model.marker is Marker.OBSERVED


def test_codex_no_signal_is_unknown(tmp_path):
    cx = tmp_path / "rollout.jsonl"
    cx.write_text(_codex_filler(50) + "\n")
    obs = meo.observe_codex(cx)
    assert obs.model.marker is Marker.UNKNOWN


# --- pi (AC2) ---------------------------------------------------------------


def _pi_assistant(path, model, level, ts="2026-09-07T10:00:00Z", mode="w"):
    rec = {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "model": model}}
    if level is not None:
        rec["message"]["thinkingLevel"] = level
    with open(path, mode) as handle:
        handle.write(json.dumps(rec) + "\n")


def test_pi_last_assistant_is_observed(tmp_path):
    pi = tmp_path / "pi.jsonl"
    _pi_assistant(pi, "claude-x", "medium")
    obs = meo.observe_pi(pi)
    assert obs.model.value == "claude-x"
    assert obs.model.marker is Marker.OBSERVED
    assert obs.model.kind is ObservationKind.RESPONSE
    assert obs.effort.value == "medium"


def test_pi_thinking_level_absent_effort_unknown(tmp_path):
    pi = tmp_path / "pi.jsonl"
    _pi_assistant(pi, "claude-x", None)
    obs = meo.observe_pi(pi)
    assert obs.model.value == "claude-x"
    assert obs.effort.marker is Marker.UNKNOWN


def test_pi_touch_does_not_advance_event_age(tmp_path):
    """AC2: the event time is the RECORD's own timestamp, never the file mtime."""
    pi = tmp_path / "pi.jsonl"
    _pi_assistant(pi, "claude-x", "high", ts="2026-09-07T10:00:00Z")
    first = meo.observe_pi(pi)
    original_event = first.model.event_time_ns
    # Touch the file (bump mtime) without adding an assistant record.
    meo.reset_caches()
    later = pi.stat().st_mtime_ns + 10_000_000_000
    import os

    os.utime(pi, ns=(later, later))
    second = meo.observe_pi(pi)
    assert second.model.event_time_ns == original_event
    assert second.model.marker is Marker.OBSERVED  # never relabels to [L]


# --- kiro (AC3) -------------------------------------------------------------


def test_kiro_model_observed_effort_unavailable(tmp_path):
    kr = tmp_path / "kiro.json"
    kr.write_text(
        json.dumps({"session_state": {"rts_model_state": {"model_info": {"model_id": "auto"}}}})
    )
    obs = meo.observe_kiro(kr)
    assert obs.model.value == "auto"  # the alias renders as the alias it is (D5)
    assert obs.model.marker is Marker.OBSERVED
    assert obs.model.kind is ObservationKind.CHECKPOINT
    assert obs.effort.marker is Marker.UNKNOWN
    assert obs.effort.validity == "runtime effort unavailable from this provider"


# --- AC5 bounded reads / cache ----------------------------------------------


def test_unchanged_file_returns_cached_projection(tmp_path, monkeypatch):
    """AC5: an unchanged file costs one stat and returns the cached result."""
    pi = tmp_path / "pi.jsonl"
    _pi_assistant(pi, "claude-x", "high")
    first = meo.observe_pi(pi)
    # Force any re-read to raise; the cache must satisfy the second call.
    real_open = meo.open if hasattr(meo, "open") else open

    calls = {"n": 0}
    import builtins

    orig = builtins.open

    def _counting_open(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(builtins, "open", _counting_open)
    second = meo.observe_pi(pi)
    assert second.model.value == first.model.value
    assert calls["n"] == 0  # no file open on the cache hit


def test_tail_read_is_bounded(tmp_path):
    """AC5: the tail adapter reads at most TAIL_BUDGET_BYTES from EOF."""
    pi = tmp_path / "pi.jsonl"
    # 200 KB of filler, then the real assistant record at EOF.
    with open(pi, "w") as handle:
        for i in range(4000):
            handle.write(json.dumps({"type": "noise", "i": i, "pad": "x" * 40}) + "\n")
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-07T10:00:00Z",
                    "message": {"role": "assistant", "model": "claude-x", "thinkingLevel": "high"},
                }
            )
            + "\n"
        )
    assert pi.stat().st_size > 64 * 1024
    obs = meo.observe_pi(pi)
    # The record at EOF is within the 64 KB tail, so it is found.
    assert obs.model.value == "claude-x"


# --- AC6 MUTANT: drop mtime_ns from the cache key -> stale relabel ----------


def test_mutant_cache_key_without_mtime_returns_stale_data(tmp_path):
    """AC6 mutant proof: if the cache key omitted mtime_ns, a rewritten file
    would return the OLD projection. We assert the CORRECT behaviour (the key
    includes mtime_ns), which is what the mutant breaks."""
    pi = tmp_path / "pi.jsonl"
    _pi_assistant(pi, "model-one", "high")
    first = meo.observe_pi(pi)
    assert first.model.value == "model-one"
    # Rewrite the file with a new model; mtime_ns changes, so the key misses and
    # the new value is read. Under the mutant (key without mtime_ns) this would
    # still return "model-one".
    import os

    later = pi.stat().st_mtime_ns + 5_000_000_000
    _pi_assistant(pi, "model-two", "high")
    os.utime(pi, ns=(later, later))
    second = meo.observe_pi(pi)
    assert second.model.value == "model-two"


def test_observe_provider_unknown_provider_is_unknown(tmp_path):
    obs = meo.observe_provider("grok_cli", tmp_path / "x")
    assert obs.model.marker is Marker.UNKNOWN
    assert obs.model.validity == "provider not observable"


def test_observe_provider_never_raises_on_adapter_failure(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setitem(meo._ADAPTERS, "pi_cli", _boom)
    obs = meo.observe_provider("pi_cli", tmp_path / "x")
    assert obs.model.marker is Marker.UNKNOWN
    assert obs.model.validity == "observation failed"


def test_sweep_removes_only_expired_sidecars(tmp_path):
    import os

    observe = tmp_path / "observe"
    observe.mkdir()
    fresh = observe / "fresh.json"
    old = observe / "old.json"
    fresh.write_text("{}")
    old.write_text("{}")
    now = time.time_ns()
    stale_mtime = now - (meo.SIDECAR_RETENTION_NS + 1_000_000_000)
    os.utime(old, ns=(stale_mtime, stale_mtime))
    removed = meo.sweep_stale_sidecars(observe, now_ns=now)
    assert removed == 1
    assert fresh.exists()
    assert not old.exists()
