"""F969 (#818): identity must not depend on an edge herdr never pushes.

The marker was written in exactly ONE place — the inbox service's socket event
loop — from a pushed frame carrying a non-empty ``agent``. On herdr 0.9.0 the
subscription that loop holds cannot deliver one: an agent-status transition
emits ``pane.agent_status_changed`` (per-pane), never the broadcast
``pane.updated`` the loop listens to. Measured; upstream
ogulcancelik/herdr#2115.

So the marker was never stamped, `read_native_identity` answered ``unavailable``
forever, and the pane carrier deferred EVERY delivery for EVERY provider:

    IDTRACE2 terminal=de35bc5e expected_agent='codex' resolved_pane='wB:p1'
             marker=None svc_pane='wB:p1' fg=None
    delivery_wake ... carrier=pane outcome=veto_unverified
                      detail=deferred:identity_unverified

The fix reads the same fact as a LEVEL from the `pane get` reply the function
was ALREADY making for `foreground_process`, and stamps the marker from it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.services.herdr_inbox_service import (
    HerdrInboxService,
    IdentityMarker,
    _IdentityRecord,
)

PANE = "wB:p1"
TID = "de35bc5e"


def _service(bound_pane: str | None = PANE, record=None, gen: int = 0):
    """A HerdrInboxService with only the identity state the proof reads."""
    import threading

    svc = HerdrInboxService.__new__(HerdrInboxService)
    svc._identity_guard = threading.RLock()
    svc._terminal_to_pane = {TID: bound_pane} if bound_pane else {}
    # The bind guard is bidirectional (review r1 N2), so the fixture must be too.
    svc._pane_to_terminal = {bound_pane: TID} if bound_pane else {}
    svc._native_event_gen = {(TID, bound_pane): gen} if bound_pane else {}
    svc._identity_records = {}
    if record is not None:
        svc._identity_records[(TID, bound_pane, gen)] = record
    return svc


def _backend(pane_payload: dict | None, svc, rc: int = 0):
    b = HerdrBackend.__new__(HerdrBackend)
    b._resolve_pane_id_from_window = MagicMock(return_value=PANE)  # type: ignore[method-assign]
    body = json.dumps({"id": "x", "result": {"pane": pane_payload or {}}})
    b._run_herdr = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(returncode=rc, stdout=body, stderr="")
    )
    import cli_agent_orchestrator.services.herdr_inbox_registry as reg

    reg.set_herdr_inbox_service(svc)
    return b


@pytest.fixture(autouse=True)
def _clear_registry():
    import cli_agent_orchestrator.services.herdr_inbox_registry as reg

    yield
    reg.set_herdr_inbox_service(None)


class TestTheSnapshotProvesIdentity:
    """The defect, stated as the box trace found it."""

    def test_a_pane_snapshot_naming_the_agent_proves_identity(self):
        svc = _service()
        assert svc.read_identity_marker(TID) is None, "precondition: no pushed edge ever arrived"

        b = _backend({"pane_id": PANE, "agent": "codex"}, svc)
        res = b.read_native_identity(TID, "cao", "w1", "codex")

        assert res.verdict == "match", res
        assert res.agent == "codex"

    def test_it_stamps_so_the_next_call_takes_the_marker_path(self):
        svc = _service()
        b = _backend({"pane_id": PANE, "agent": "codex"}, svc)
        b.read_native_identity(TID, "cao", "w1", "codex")

        marker = svc.read_identity_marker(TID)
        assert marker is not None
        assert marker.agent == "codex"
        assert marker.pane_id == PANE

    def test_a_wrong_kind_of_agent_is_a_mismatch_not_a_match(self):
        """The proof must still be able to say no."""
        svc = _service()
        b = _backend({"pane_id": PANE, "agent": "claude"}, svc)
        assert b.read_native_identity(TID, "cao", "w1", "codex").verdict == "mismatch"

    def test_no_agent_in_the_snapshot_is_still_unavailable(self):
        """A pane with nothing detected proves nothing — that part was right."""
        svc = _service()
        b = _backend({"pane_id": PANE}, svc)
        assert b.read_native_identity(TID, "cao", "w1", "codex").verdict == "unavailable"

    def test_an_unreadable_pane_is_still_unavailable(self):
        svc = _service()
        b = _backend(None, svc, rc=1)
        assert b.read_native_identity(TID, "cao", "w1", "codex").verdict == "unavailable"


class TestTheMarkerPathIsUnchanged:
    """A pushed edge must keep working, and must keep WINNING."""

    def test_an_existing_marker_is_used_without_stamping(self):
        rec = _IdentityRecord(IdentityMarker("codex", PANE, 0), 0.0)
        svc = _service(record=rec)
        b = _backend({"pane_id": PANE, "agent": "codex"}, svc)
        assert b.read_native_identity(TID, "cao", "w1", "codex").verdict == "match"
        assert svc.read_identity_marker(TID) is rec.marker

    def test_a_pushed_edge_outranks_a_later_snapshot(self):
        """If the edge already spoke for this incarnation, it is authoritative."""
        rec = _IdentityRecord(IdentityMarker("claude", PANE, 0), 0.0)
        svc = _service(record=rec)
        stamped = svc.stamp_identity_from_snapshot(TID, PANE, "codex")
        assert stamped is not None and stamped.agent == "claude"


class TestStampingRefusesWhatItCannotVouchFor:
    """A missing proof is bad. A WRONG proof is worse."""

    def test_it_refuses_a_pane_the_service_does_not_bind(self):
        svc = _service()
        assert svc.stamp_identity_from_snapshot(TID, "wZ:p9", "codex") is None
        assert svc.read_identity_marker(TID) is None

    def test_it_refuses_when_the_terminal_has_no_binding_at_all(self):
        svc = _service(bound_pane=None)
        assert svc.stamp_identity_from_snapshot(TID, PANE, "codex") is None

    @pytest.mark.parametrize("agent,pane", [("", PANE), ("codex", "")])
    def test_it_refuses_empty_inputs(self, agent, pane):
        svc = _service()
        assert svc.stamp_identity_from_snapshot(TID, pane, agent) is None


class TestTheSubscriptionCarriesThePerPaneSpec:
    """The lesser half: panes present at connect also get the edge."""

    def test_it_batches_one_spec_per_known_pane(self):
        import asyncio
        import threading

        svc = HerdrInboxService.__new__(HerdrInboxService)
        svc._identity_guard = threading.RLock()
        svc._terminal_to_pane = {"t1": "w1:p1", "t2": "w1:p2"}
        sent: list = []

        class _C:
            async def subscribe(self, subs):
                sent.append(subs)

        svc._client = _C()
        asyncio.run(svc._subscribe_all_events())

        types = [s["type"] for s in sent[0]]
        assert "pane.updated" in types
        per_pane = [s for s in sent[0] if s["type"] == "pane.agent_status_changed"]
        assert sorted(s["pane_id"] for s in per_pane) == ["w1:p1", "w1:p2"]
        assert len(sent) == 1, "herdr resets the connection on a SECOND events.subscribe"

    def test_it_reuses_the_h1_spelling_rather_than_a_second_copy(self):
        """Dots, not underscores — and defined once."""
        from cli_agent_orchestrator.adapters.truth.herdr_runtime import (
            PANE_AGENT_STATUS_CHANGED,
        )

        assert PANE_AGENT_STATUS_CHANGED == "pane.agent_status_changed"


class TestEdgePrecedenceIsGatedOnTrust:
    """Review r1 B1: the "pushed edge wins" branch must not resurrect evidence
    the reader just rejected.

    `stamp_identity_from_snapshot` is only ever called after
    `read_identity_marker` returned None. So a record still present at that
    point is one the reader REFUSED — quarantined, or inside the reconnect
    grace. Gating precedence on `not quarantined` alone let the grace case win
    over a fresh level read, which was probed live as
    `verdict=mismatch, agent=claude` against a pane whose `pane get` said
    `codex`, and symmetrically as a FALSE `match` after an in-place relaunch.
    """

    def _svc_with(self, record):
        import threading

        from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService

        svc = HerdrInboxService.__new__(HerdrInboxService)
        svc._identity_guard = threading.RLock()
        svc._terminal_to_pane = {TID: PANE}
        svc._pane_to_terminal = {PANE: TID}
        svc._native_event_gen = {(TID, PANE): 0}
        svc._identity_records = {(TID, PANE, 0): record}
        return svc

    def test_a_grace_window_marker_does_NOT_beat_the_snapshot(self):
        import time as _t

        stale = _IdentityRecord(IdentityMarker("claude", PANE, 0), 0.0)
        stale.grace_started = _t.monotonic()  # inside RECONNECT_GRACE_S
        svc = self._svc_with(stale)

        assert svc.read_identity_marker(TID) is None, "precondition: the reader distrusts it"
        m = svc.stamp_identity_from_snapshot(TID, PANE, "codex")
        assert (
            m is not None and m.agent == "codex"
        ), "the live pane must win over grace-held evidence"

    def test_a_quarantined_marker_does_NOT_beat_the_snapshot(self):
        rec = _IdentityRecord(IdentityMarker("claude", PANE, 0), 0.0)
        rec.quarantined = True
        svc = self._svc_with(rec)
        m = svc.stamp_identity_from_snapshot(TID, PANE, "codex")
        assert m is not None and m.agent == "codex"

    def test_a_TRUSTED_edge_still_wins(self):
        rec = _IdentityRecord(IdentityMarker("claude", PANE, 0), 0.0)
        svc = self._svc_with(rec)
        assert svc.read_identity_marker(TID) is not None, "precondition: the reader trusts it"
        m = svc.stamp_identity_from_snapshot(TID, PANE, "codex")
        assert m is not None and m.agent == "claude", "a trusted edge outranks a later snapshot"

    def test_the_two_readers_share_one_trust_predicate(self):
        """The drift this guards: two copies of 'is this record usable'."""
        import time as _t

        from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService

        rec = _IdentityRecord(IdentityMarker("claude", PANE, 0), 0.0)
        rec.grace_started = _t.monotonic()
        assert HerdrInboxService._record_is_trusted(rec, _t.monotonic()) is False
        rec.grace_started = None
        assert HerdrInboxService._record_is_trusted(rec, _t.monotonic()) is True
        assert HerdrInboxService._record_is_trusted(None, _t.monotonic()) is False


class TestTheBindGuardIsBidirectional:
    """Review r1 N2: a half-consistent binding is what a recycle passes through."""

    def _svc(self, fwd, rev):
        import threading

        from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService

        svc = HerdrInboxService.__new__(HerdrInboxService)
        svc._identity_guard = threading.RLock()
        svc._terminal_to_pane = fwd
        svc._pane_to_terminal = rev
        svc._native_event_gen = {}
        svc._identity_records = {}
        return svc

    def test_forward_ok_reverse_points_elsewhere_is_refused(self):
        svc = self._svc({TID: PANE}, {PANE: "someone-else"})
        assert svc.stamp_identity_from_snapshot(TID, PANE, "codex") is None

    def test_reverse_ok_forward_points_elsewhere_is_refused(self):
        svc = self._svc({TID: "wZ:p9"}, {PANE: TID})
        assert svc.stamp_identity_from_snapshot(TID, PANE, "codex") is None

    def test_both_consistent_is_accepted(self):
        svc = self._svc({TID: PANE}, {PANE: TID})
        assert svc.stamp_identity_from_snapshot(TID, PANE, "codex") is not None


class TestEveryRoutedProviderCanAttemptTheProof:
    """Review r1 N6: a provider absent from the map fails at `expected_agent is
    None` — the proof is not even attempted, so those seats are dead on herdr."""

    def test_pi_and_cline_are_mapped(self):
        from cli_agent_orchestrator.backends.herdr_backend import _PROVIDER_AGENT_MARKERS

        assert _PROVIDER_AGENT_MARKERS.get("pi_cli") == "pi"
        assert _PROVIDER_AGENT_MARKERS.get("cline_cli") == "cline"

    def test_the_marker_names_match_herdr_manifest_ids(self):
        """These are herdr's own manifest ids, not CAO's provider names."""
        from cli_agent_orchestrator.backends.herdr_backend import _PROVIDER_AGENT_MARKERS

        assert _PROVIDER_AGENT_MARKERS["claude_code"] == "claude"
        assert _PROVIDER_AGENT_MARKERS["kiro_cli"] == "kiro"
