"""F634 (#489) D15 — box-plane server-side recovery refusal (AC20 third clause).

Box lanes are EXCLUDED from server-side auto-recovery, keyed on
``CAO_SERVER_PLANE=box`` read by the serving process. Both recover services
(``epoch_recovery_service.recover_epoch``,
``provider_rebind_service.recover_provider_reauth``) refuse when the process is
box-plane, raising the typed ``BoxPlaneRecoveryRefused`` — and they refuse at
ENTRY, before any selection or terminal creation, so NO replacement terminal is
created (AC20's "NO replacement terminal created").

On a laptop/default server (``CAO_SERVER_PLANE`` unset) the refusal is a no-op
and the existing recovery path is unchanged.

MUTANT (AC20): "launch the box server WITHOUT the plane marker" -> the refusal
has no key and the recovery door reopens. Modelled here by clearing
``CAO_SERVER_PLANE``: the refusal must NOT fire, proving the guard is keyed on
the plane marker and nothing else.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services import epoch_recovery_service, provider_rebind_service
from cli_agent_orchestrator.utils import server_plane
from cli_agent_orchestrator.utils.server_plane import (
    BoxPlaneRecoveryRefused,
    is_box_plane,
    refuse_recovery_if_box_plane,
)
from cli_agent_orchestrator.utils.server_plane import server_plane as read_server_plane


class TestServerPlaneReader:
    def test_absent_is_empty_and_not_box(self, monkeypatch):
        monkeypatch.delenv("CAO_SERVER_PLANE", raising=False)
        assert read_server_plane() == ""
        assert is_box_plane() is False

    def test_box_marker_detected_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("CAO_SERVER_PLANE", " Box ")
        assert read_server_plane() == "box"
        assert is_box_plane() is True

    def test_other_plane_is_not_box(self, monkeypatch):
        monkeypatch.setenv("CAO_SERVER_PLANE", "laptop")
        assert is_box_plane() is False

    def test_refuse_helper_raises_only_on_box(self, monkeypatch):
        monkeypatch.setenv("CAO_SERVER_PLANE", "box")
        with pytest.raises(BoxPlaneRecoveryRefused):
            refuse_recovery_if_box_plane("unit")
        monkeypatch.delenv("CAO_SERVER_PLANE", raising=False)
        assert refuse_recovery_if_box_plane("unit") is None  # no-op on laptop

    def test_typed_refusal_detail_shape(self):
        exc = BoxPlaneRecoveryRefused("epoch recovery")
        d = exc.detail()
        assert d["code"] == "E-BOX-PLANE-NO-RECOVER"
        assert d["reason"] == "epoch recovery"
        assert "box-plane" in d["message"]


@pytest.mark.asyncio
class TestEpochRecoveryBoxPlaneRefusal:
    async def test_box_plane_refuses_before_any_side_effect(self, monkeypatch):
        monkeypatch.setenv("CAO_SERVER_PLANE", "box")
        # If the refusal did NOT fire first, recover_epoch would consult the
        # backend (session_exists). Make that blow up so a leak past the guard
        # is unmistakable rather than silently passing.
        backend = MagicMock()
        backend.session_exists.side_effect = AssertionError(
            "recover_epoch reached session_exists on a box-plane server"
        )
        with patch.object(epoch_recovery_service, "get_backend", lambda: backend):
            with pytest.raises(BoxPlaneRecoveryRefused) as ei:
                await epoch_recovery_service.recover_epoch("cao-s")
        assert ei.value.reason == "epoch recovery"
        backend.session_exists.assert_not_called()

    async def test_laptop_plane_does_not_refuse(self, monkeypatch):
        # MUTANT witness: no plane marker -> the guard is inert and control
        # reaches the normal path (which we stop at session_missing so the test
        # needs no DB). The point is that NO BoxPlaneRecoveryRefused is raised.
        monkeypatch.delenv("CAO_SERVER_PLANE", raising=False)
        backend = MagicMock()
        backend.session_exists.return_value = False
        with patch.object(epoch_recovery_service, "get_backend", lambda: backend):
            with pytest.raises(ValueError, match="session_missing"):
                await epoch_recovery_service.recover_epoch("cao-s")
        backend.session_exists.assert_called_once()


@pytest.mark.asyncio
class TestProviderRebindBoxPlaneRefusal:
    async def test_box_plane_refuses_before_any_side_effect(self, monkeypatch):
        monkeypatch.setenv("CAO_SERVER_PLANE", "box")
        # list_terminals_by_session is the first selection side effect; make it
        # loud so any leak past the entry guard fails rather than passes.
        boom = MagicMock(
            side_effect=AssertionError(
                "recover_provider_reauth reached selection on a box-plane server"
            )
        )
        with patch.object(provider_rebind_service, "list_terminals_by_session", boom):
            with pytest.raises(BoxPlaneRecoveryRefused) as ei:
                await provider_rebind_service.recover_provider_reauth("cao-s")
        assert ei.value.reason == "provider-reauth recovery"
        boom.assert_not_called()

    async def test_laptop_plane_does_not_refuse(self, monkeypatch):
        monkeypatch.delenv("CAO_SERVER_PLANE", raising=False)
        # No plane marker: the guard is inert, selection runs. Empty roster ->
        # no work, returns a dict, no BoxPlaneRecoveryRefused.
        with patch.object(provider_rebind_service, "list_terminals_by_session", lambda s: []):
            result = await provider_rebind_service.recover_provider_reauth("cao-s")
        assert isinstance(result, dict)
