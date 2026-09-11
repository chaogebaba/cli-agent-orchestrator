"""F926 (#778): an unclassifiable herdr ``agent_status`` must be COUNTED, not silent.

Under ``terminal.backend = herdr``, ``herdr pane get`` answers
``agent_status: unknown`` for every pi/cline pane while codex resolves.  CAO then
falls back to tmux-style pane scraping for exactly the cheap lanes (grunt,
secretary, lite gate) that make up most of the fleet — correctly, but silently,
which is indistinguishable from native status working.

The cause is herdr's, measured on the pinned 0.9.0 binary (protocol 22) by
reading its own bundled agent-detection manifests
(``~/.local/state/herdr/agent-detection/remote/*.toml``):

    pi.toml     1 rule   -> states: working
    cline.toml  2 rules  -> states: working, blocked
    codex.toml  24 rules -> states: working, blocked, idle, unknown

Only 10 of the 21 shipped manifests can produce ``idle`` at all.  A pi or cline
pane sitting at its prompt matches no rule, so herdr reports ``unknown``
indefinitely.  CAO cannot fix a manifest it does not own; what it owes is a
counted row instead of silence.

The second half of the contract is the one that would be expensive to get wrong:
``unknown`` must NOT become a ``failure_cause``.  ``receiver_state_view`` copies
``failure_cause`` into the probe meta as ``probe_failure``, and
``inbox_service`` vetoes delivery whenever that key is present — so typing the
unknown would stop every message to the very seats this finding is about.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.adapters.truth import wiring
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.core.findings import FindingCode


class _RecordingFindingStore:
    """Captures ``record`` calls and folds repeats the way SQLite's store does."""

    def __init__(self) -> None:
        self.calls: list[tuple[FindingCode, str, str, str]] = []

    def record(self, code, *, terminal_id="", dedupe_key="", detail="", sample_event_id=None):
        self.calls.append((code, terminal_id, dedupe_key, detail))
        return MagicMock()

    def counts_by_key(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, _, key, _ in self.calls:
            out[key] = out.get(key, 0) + 1
        return out


@pytest.fixture
def findings():
    """Install a producer runtime carrying only a FindingStore, then disarm."""
    store = _RecordingFindingStore()
    wiring.install_producers(
        wiring.ProducerRuntime(store=MagicMock(), clock=MagicMock(), findings=store)
    )
    try:
        yield store
    finally:
        wiring.reset_producers()


def _backend(agent_status: str):
    backend = HerdrBackend.__new__(HerdrBackend)
    backend._resolve_pane_id_from_window = MagicMock(return_value="w1:p3")  # type: ignore[method-assign]
    backend._run_herdr = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            returncode=0,
            stdout='{"id":"x","result":{"pane":{"pane_id":"w1:p3","agent_status":"%s"}}}'
            % agent_status,
            stderr="",
        )
    )
    return backend


def _reset_log_throttle():
    from cli_agent_orchestrator.backends import herdr_backend as mod

    mod._STATUS_UNKNOWN_WARNED.clear()


class TestUnknownIsCounted:
    def test_unknown_records_one_finding_keyed_on_the_window(self, findings):
        _reset_log_throttle()
        _backend("unknown").fetch_native_status("cao", "w3-pi")
        assert len(findings.calls) == 1
        code, terminal_id, dedupe_key, detail = findings.calls[0]
        assert code is FindingCode.DIAG_HERDR_STATUS_UNKNOWN
        assert dedupe_key == "w3-pi", "the seat is the dedupe identity"
        assert terminal_id == "", "fleet-wide row: never NULL, never a fake id"
        assert "w1:p3" in detail and "unknown" in detail

    def test_repeats_are_counted_per_seat_not_merged_across_seats(self, findings):
        _reset_log_throttle()
        backend = _backend("unknown")
        for _ in range(5):
            backend.fetch_native_status("cao", "w1-cline")
        for _ in range(3):
            backend.fetch_native_status("cao", "w3-pi")
        assert findings.counts_by_key() == {"w1-cline": 5, "w3-pi": 3}

    @pytest.mark.parametrize("resolved", ["idle", "working", "blocked", "done"])
    def test_a_classified_pane_records_nothing(self, findings, resolved):
        _reset_log_throttle()
        fetch = _backend(resolved).fetch_native_status("cao", "w2-codex")
        assert fetch.status is not None
        assert findings.calls == [], "codex resolves; only the gap is counted"

    def test_an_unrecognised_state_is_also_counted(self, findings):
        """Anything ``map_native_status`` cannot map leaves the seat without truth."""
        _reset_log_throttle()
        _backend("wedged").fetch_native_status("cao", "w4")
        assert len(findings.calls) == 1
        assert "wedged" in findings.calls[0][3]


class TestTheFallbackContractIsUnchanged:
    """The counting must not alter what the poll returns."""

    def test_unknown_keeps_failure_cause_none(self, findings):
        _reset_log_throttle()
        fetch = _backend("unknown").fetch_native_status("cao", "w3-pi")
        assert fetch.agent_status == "unknown"
        assert fetch.status is None
        assert fetch.failure_cause is None, (
            "a failure_cause becomes probe_meta['probe_failure'], which "
            "inbox_service treats as a delivery VETO — that would stop every "
            "message to every pi/cline seat"
        )

    def test_a_broken_finding_store_never_breaks_the_poll(self):
        _reset_log_throttle()
        exploding = MagicMock()
        exploding.record.side_effect = RuntimeError("store is down")
        wiring.install_producers(
            wiring.ProducerRuntime(store=MagicMock(), clock=MagicMock(), findings=exploding)
        )
        try:
            fetch = _backend("unknown").fetch_native_status("cao", "w3-pi")
        finally:
            wiring.reset_producers()
        assert fetch.agent_status == "unknown"
        assert fetch.failure_cause is None

    def test_ingestion_off_is_silent_and_harmless(self):
        _reset_log_throttle()
        wiring.reset_producers()
        fetch = _backend("unknown").fetch_native_status("cao", "w3-pi")
        assert fetch.agent_status == "unknown"
        assert fetch.failure_cause is None


class TestTheLogSaysItOnce:
    def test_warning_once_per_seat_then_debug(self, findings, caplog):
        _reset_log_throttle()
        backend = _backend("unknown")
        with caplog.at_level("WARNING", logger="cli_agent_orchestrator.backends.herdr_backend"):
            for _ in range(4):
                backend.fetch_native_status("cao", "w3-pi")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1, "the count lives in the finding, not in the log"
        assert "herdr_status_unknown" in warnings[0].getMessage()
        # ...but the finding still counted every one of them.
        assert findings.counts_by_key() == {"w3-pi": 4}
