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


@pytest.fixture(autouse=True)
def _reset_log_throttle():
    """N2: the warn-once set is process-wide state.

    Clearing it by hand in each test made a forgotten call silently suppress the
    warning a later test asserted on. An autouse fixture removes the ordering
    coupling entirely.
    """
    from cli_agent_orchestrator.backends import herdr_backend as mod

    mod._STATUS_UNKNOWN_WARNED.clear()
    yield
    mod._STATUS_UNKNOWN_WARNED.clear()


class TestUnknownIsCounted:
    def test_unknown_records_one_finding_keyed_on_the_window(self, findings):
        _backend("unknown").fetch_native_status("cao", "w3-pi")
        assert len(findings.calls) == 1
        code, terminal_id, dedupe_key, detail = findings.calls[0]
        assert code is FindingCode.DIAG_HERDR_STATUS_UNKNOWN
        assert dedupe_key == "w3-pi", "the seat is the dedupe identity"
        assert terminal_id == "", "no 8-hex suffix in this window name -> no guess"
        assert "w1:p3" in detail and "unknown" in detail

    def test_repeats_are_counted_per_seat_not_merged_across_seats(self, findings):
        backend = _backend("unknown")
        for _ in range(5):
            backend.fetch_native_status("cao", "w1-cline")
        for _ in range(3):
            backend.fetch_native_status("cao", "w3-pi")
        assert findings.counts_by_key() == {"w1-cline": 5, "w3-pi": 3}

    @pytest.mark.parametrize("resolved", ["idle", "working", "blocked", "done"])
    def test_a_classified_pane_records_nothing(self, findings, resolved):
        fetch = _backend(resolved).fetch_native_status("cao", "w2-codex")
        assert fetch.status is not None
        assert findings.calls == [], "codex resolves; only the gap is counted"

    def test_an_unrecognised_state_is_also_counted(self, findings):
        """Anything ``map_native_status`` cannot map leaves the seat without truth."""
        _backend("wedged").fetch_native_status("cao", "w4")
        assert len(findings.calls) == 1
        assert "wedged" in findings.calls[0][3]


class TestTheFallbackContractIsUnchanged:
    """The counting must not alter what the poll returns."""

    def test_unknown_keeps_failure_cause_none(self, findings):
        fetch = _backend("unknown").fetch_native_status("cao", "w3-pi")
        assert fetch.agent_status == "unknown"
        assert fetch.status is None
        assert fetch.failure_cause is None, (
            "a failure_cause becomes probe_meta['probe_failure'], which "
            "inbox_service treats as a delivery VETO — that would stop every "
            "message to every pi/cline seat"
        )

    def test_a_broken_finding_store_never_breaks_the_poll(self):
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
        wiring.reset_producers()
        fetch = _backend("unknown").fetch_native_status("cao", "w3-pi")
        assert fetch.agent_status == "unknown"
        assert fetch.failure_cause is None


class TestTheLogSaysItOnce:
    def test_warning_once_per_seat_then_debug(self, findings, caplog):
        backend = _backend("unknown")
        with caplog.at_level("WARNING", logger="cli_agent_orchestrator.backends.herdr_backend"):
            for _ in range(4):
                backend.fetch_native_status("cao", "w3-pi")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1, "the count lives in the finding, not in the log"
        assert "herdr_status_unknown" in warnings[0].getMessage()
        # ...but the finding still counted every one of them.
        assert findings.counts_by_key() == {"w3-pi": 4}


class TestTheTwoConditionsAreDistinguishable:
    """N1: herdr SAYING "unknown" is not the same as the field being absent."""

    def test_a_missing_agent_status_field_says_so(self, findings):
        backend = HerdrBackend.__new__(HerdrBackend)
        backend._resolve_pane_id_from_window = MagicMock(return_value="w1:p3")  # type: ignore[method-assign]
        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                returncode=0,
                stdout='{"id":"x","result":{"pane":{"pane_id":"w1:p3"}}}',
                stderr="",
            )
        )
        fetch = backend.fetch_native_status("cao", "w9")

        assert fetch.agent_status is None, "CAO never saw a status to report"
        assert fetch.status is None
        assert fetch.failure_cause is None, (
            "protocol drift must not become a probe_failure — that is a delivery "
            "VETO at inbox_service's safety gate"
        )
        assert len(findings.calls) == 1
        detail = findings.calls[0][3]
        assert "no agent_status field" in detail, detail
        assert "'unknown'" not in detail, "must not claim herdr said unknown"

    def test_herdr_saying_unknown_still_reads_as_herdr_saying_unknown(self, findings):
        _backend("unknown").fetch_native_status("cao", "w9")
        assert "herdr agent_status='unknown'" in findings.calls[0][3]


class TestTheFindingCarriesTheTerminal:
    """N4: `cao diag findings` renders `terminal_id or '-'`, so fill it."""

    def test_a_window_name_yields_its_terminal_id(self, findings):
        _backend("unknown").fetch_native_status("cao-x", "codex_general-919751d7")
        _, terminal_id, dedupe_key, _ = findings.calls[0]
        assert terminal_id == "919751d7"
        assert dedupe_key == "codex_general-919751d7"

    def test_a_profile_containing_dashes_still_resolves(self, findings):
        _backend("unknown").fetch_native_status("cao-x", "developer-opus-cfe93884")
        assert findings.calls[0][1] == "cfe93884"

    @pytest.mark.parametrize(
        "window",
        [
            "codex_general-abcd",  # legacy {profile}-{uuid4[:4]} form
            "codex_general-91975XYZ",  # not hex
            "nodashes",
            "codex_general-919751D7",  # minted ids are lowercase
        ],
    )
    def test_a_suffix_that_is_not_a_minted_id_is_never_guessed(self, findings, window):
        _backend("unknown").fetch_native_status("cao-x", window)
        assert findings.calls[0][1] == "", window


class TestTheWarnSetIsBounded:
    """N2: an unbounded module global with no owner is a slow leak."""

    def test_the_set_clears_rather_than_growing_without_end(self, findings):
        from cli_agent_orchestrator.backends import herdr_backend as mod

        backend = _backend("unknown")
        for i in range(mod._STATUS_UNKNOWN_WARNED_MAX + 5):
            backend.fetch_native_status("cao", f"w{i}")
        assert len(mod._STATUS_UNKNOWN_WARNED) <= mod._STATUS_UNKNOWN_WARNED_MAX

    def test_two_sessions_sharing_a_window_name_each_get_a_line(self, findings, caplog):
        backend = _backend("unknown")
        with caplog.at_level("WARNING", logger="cli_agent_orchestrator.backends.herdr_backend"):
            backend.fetch_native_status("cao-a", "w1")
            backend.fetch_native_status("cao-b", "w1")
            backend.fetch_native_status("cao-a", "w1")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 2, "the seat is (session, window), not window alone"
