"""F514 (#369, P1) — pane-nudge fallback must survive a cao-server restart.

Root cause (per orchestrator/tmp/orch/f213-quirk-report.md §3/§4/§6):
when cao-server is bounced, the supervisor's terminal row can be recreated
without its ``cc_team_inbox_path`` metadata ("Terminal metadata not found",
then a fresh row with no inbox path). The old ``_should_teammate_push`` gate
checked the raw metadata key and returned False on that loss, so the fx168
pane-nudge fallback ring was rejected with ``not_registered_fallback`` on
EVERY message until a full re-registration — silently killing the supervisor
wake safety net.

The fix routes ``_should_teammate_push`` through ``_resolve_inbox_path``, which
self-heals by re-deriving the path from the persisted ``working_directory`` +
provider (F152 lazy-derive) and persists it back for future calls. It is
deliberately INDEPENDENT of ``supervisor.wake.native`` — the fallback is the
net that has to work regardless of the native flip state — and it does NOT
touch the F337 create-time gate (``_maybe_derive_cc_team_inbox_path``), which
stays gated on ``wake.native``.

These tests simulate the restart shape: metadata with a claude_code provider
and a ``working_directory`` but NO ``cc_team_inbox_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

from cli_agent_orchestrator.services.teammate_push_service import (
    _should_teammate_push,
    _resolve_inbox_path,
)


def _post_restart_metadata(working_directory: str) -> Dict[str, Any]:
    """Terminal metadata as recreated by a server restart: claude_code provider,
    a persisted working_directory, but NO cc_team_inbox_path (it was lost)."""
    return {
        "id": "sup-001",
        "tmux_session": "cao-claude-orch5",
        "tmux_window": "chao_supervisor-sup-001",
        "provider": "claude_code",
        "agent_profile": "chao_supervisor",
        "working_directory": working_directory,
        "metadata": {},  # cc_team_inbox_path lost on restart
        "last_active": None,
    }


def _non_claude_metadata(working_directory: str) -> Dict[str, Any]:
    """A non-claude_code terminal with a working_directory but no inbox path —
    the self-heal must NOT derive here (derivation is claude_code-only)."""
    md = _post_restart_metadata(working_directory)
    md["provider"] = "kiro_cli"
    return md


class TestFallbackGateSurvivesRestart:
    """_should_teammate_push must re-derive the lost path after a restart."""

    def test_gate_true_after_restart_rederives_path(self, tmp_path: Path) -> None:
        """Restart shape: no cc_team_inbox_path but derivable => gate True."""
        derived = tmp_path / "team-lead.json"
        persisted: Dict[str, Any] = {}

        def _fake_update(terminal_id: str, new_md: Dict[str, Any]) -> None:
            persisted["terminal_id"] = terminal_id
            persisted["metadata"] = new_md

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=_post_restart_metadata(str(tmp_path)),
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._derive_cc_team_inbox_path",
                return_value=derived,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.update_terminal_metadata",
                side_effect=_fake_update,
            ),
        ):
            mock_cfg.get.return_value = True
            # The gate that was returning False on the restarted terminal.
            assert _should_teammate_push("sup-001") is True

        # Durable persistence: the re-derived path was written back to metadata
        # so subsequent lookups are cheap and consistent (survives restart).
        assert persisted["terminal_id"] == "sup-001"
        assert persisted["metadata"]["cc_team_inbox_path"] == str(derived)

    def test_gate_independent_of_wake_native(self, tmp_path: Path) -> None:
        """The fallback net must work regardless of supervisor.wake.native.

        F337 gates create-time derivation on wake.native, but the fallback path
        is the safety net and must NOT consult that flag. We assert the gate is
        True even when wake.native is False.
        """
        derived = tmp_path / "team-lead.json"
        config_values = {
            "supervisor.teammate_push": True,
            "supervisor.wake.native": False,  # native OFF — fallback must still arm
        }

        def _mock_get(key, default=None, **_kw):
            return config_values.get(key, default)

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=_post_restart_metadata(str(tmp_path)),
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._derive_cc_team_inbox_path",
                return_value=derived,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.update_terminal_metadata",
            ),
        ):
            mock_cfg.get.side_effect = _mock_get
            assert _should_teammate_push("sup-001") is True

    def test_gate_false_when_flag_off_even_if_derivable(self, tmp_path: Path) -> None:
        """teammate_push flag OFF short-circuits before any derivation."""
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._derive_cc_team_inbox_path",
            ) as mock_derive,
        ):
            mock_cfg.get.return_value = False
            assert _should_teammate_push("sup-001") is False
            mock_derive.assert_not_called()

    def test_gate_false_when_not_derivable_non_claude(self, tmp_path: Path) -> None:
        """A non-claude_code terminal is not derivable => gate stays False.

        Guards against the self-heal over-firing for providers that have no CC
        inbox to write to.
        """
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=_non_claude_metadata(str(tmp_path)),
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._derive_cc_team_inbox_path",
            ) as mock_derive,
        ):
            mock_cfg.get.return_value = True
            assert _should_teammate_push("sup-001") is False
            # Derivation is claude_code-only; must not be attempted here.
            mock_derive.assert_not_called()

    def test_gate_false_when_no_working_directory(self) -> None:
        """No working_directory (nothing to derive from) => gate stays False."""
        md = {
            "id": "sup-001",
            "provider": "claude_code",
            "working_directory": None,
            "metadata": {},
        }
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=md,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._derive_cc_team_inbox_path",
            ) as mock_derive,
        ):
            mock_cfg.get.return_value = True
            assert _should_teammate_push("sup-001") is False
            mock_derive.assert_not_called()


class TestDoorbellFallbackArmsAfterRestart:
    """End-to-end at the doorbell gate: after a restart, ring_supervisor_doorbell
    must reach the fx168 pane-nudge fallback instead of skipping with
    ``not_registered_fallback``."""

    def test_fallback_rings_after_restart(self, tmp_path: Path) -> None:
        from cli_agent_orchestrator.services.doorbell_service import (
            ring_supervisor_doorbell,
        )

        derived = tmp_path / "team-lead.json"

        with (
            # doorbell outer switches on
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ConfigService"
            ) as mock_db_cfg,
            # native OFF so we exercise the fallback branch specifically
            patch(
                "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending",
                return_value=True,
            ),
            # real _should_teammate_push, but with a restarted-terminal metadata
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_tp_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=_post_restart_metadata(str(tmp_path)),
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._derive_cc_team_inbox_path",
                return_value=derived,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.update_terminal_metadata",
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_gated_ring",
                return_value="rang",
            ) as mock_ring,
        ):
            def _db_get(key, default=None, **_kw):
                mapping = {
                    "supervisor.doorbell": True,
                    "supervisor.wake.native": False,
                }
                return mapping.get(key, default)

            mock_db_cfg.get.side_effect = _db_get
            mock_tp_cfg.get.return_value = True

            decision = ring_supervisor_doorbell("sup-001", 1081, written_count=1)

        # Before the fix this returned "skipped_disabled"/not_registered_fallback.
        assert decision == "rang"
        mock_ring.assert_called_once()
