"""F210: supervisor send-keys exemption, fire-time draft re-check, nudge kill-switch.

AC1-AC12 of ``orchestrator/blueprints/f210-supervisor-nudge-kill.md`` (root repo
``cli-subagents``). Every case drives the real
``cli_agent_orchestrator.services.delivery_service`` functions against an
in-memory SQLite database; only the boundaries are mocked — ``tmux_client.send_keys``,
``subprocess.run`` (tmux display-message / set-option), and the provider handle
that owns the composer read.

The non-supervisor obligation rows (AC4-AC9) have no production producer:
obligations are created for supervisor mailboxes only
(``clients/database.py`` ``_is_supervisor_mailbox_id``). They are sanctioned
setup for the rung that F210 keeps as a gated liveness reserve (D14).
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    DeliveryObligationModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    _utcnow,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.services import config_service as cs
from cli_agent_orchestrator.services.boundary_pull_service import boundary_pull_service
from cli_agent_orchestrator.services.config_service import ConfigService
from cli_agent_orchestrator.services.delivery_service import (
    DeliveryTarget,
    _drive_one_obligation,
    _fire_due_nudges,
    attempt_rung2,
    convergence_tick,
)
from cli_agent_orchestrator.services.nudge_discipline import nudge_discipline

SUP_TERMINAL = "sup_f210"
SUP_MAILBOX = "mb_f210_sup"
SUP_SESSION = "cao-test-f210"
WRK_TERMINAL = "wrk_f210"
WRK_MAILBOX = "mb_f210_wrk"

# Deterministic timer values — the real ConfigService reads the developer's own
# settings file, which must not decide whether an assertion holds.
BASE_CONFIG = {
    "delivery.phase": "shadow",
    "delivery.tick_s": 5.0,
    "delivery.escalate_after_s": 120.0,
    "delivery.interrupt_after_s": 30.0,
    "delivery.jitter": "off",
    "delivery.nudge_sendkeys_enabled": True,
    "supervisor.mailbox_pull": False,
}


def config_patch(**overrides):
    """Patch ConfigService.get with a deterministic table.

    Unknown keys fall through to the caller's own default, so a config read this
    test does not care about still behaves as the production call site expects.
    """
    table = {**BASE_CONFIG, **overrides}

    def _get(path, default=None, override=None):
        if override is not None:
            return override
        if path in table:
            return table[path]
        return default

    return patch.object(ConfigService, "get", staticmethod(_get))


def display_message_calls(mock_run):
    """Only the operator-facing banner — tmux is also queried with display-message -p."""
    return [
        c
        for c in mock_run.call_args_list
        if "display-message" in _argv(c) and any("[cao]" in a for a in _argv(c))
    ]


def pending_option_calls(mock_run):
    return [c for c in mock_run.call_args_list if "@cao_pending" in " ".join(_argv(c))]


def _argv(mock_call):
    args = mock_call[0][0] if mock_call[0] else mock_call[1].get("args", [])
    return [str(a) for a in args]


@pytest.fixture
def f210_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", TestSession)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.mailbox_service.SessionLocal", TestSession)
    # The two service singletons carry per-terminal state across tests.
    boundary_pull_service._states.clear()
    nudge_discipline._states.clear()
    yield TestSession
    boundary_pull_service._states.clear()
    nudge_discipline._states.clear()


@pytest.fixture
def supervisor(f210_db):
    """Supervisor mailbox whose current incarnation is a live terminal."""
    with f210_db() as db:
        db.add(
            TerminalModel(
                id=SUP_TERMINAL,
                tmux_session=SUP_SESSION,
                tmux_window="supervisor",
                provider="claude_code",
                agent_profile="chao_supervisor",
            )
        )
        db.add(
            MailboxModel(
                id=SUP_MAILBOX,
                session_name=SUP_SESSION,
                role="supervisor",
                current_terminal_id=SUP_TERMINAL,
                generation=1,
                consumed_through_id=0,
                cc_inbox_path=None,
            )
        )
        db.add(
            MailboxIncarnationModel(mailbox_id=SUP_MAILBOX, generation=1, terminal_id=SUP_TERMINAL)
        )
        db.commit()
    return f210_db


@pytest.fixture
def worker(f210_db):
    """Non-supervisor mailbox — the only shape that still reaches the injection."""
    with f210_db() as db:
        db.add(
            TerminalModel(
                id=WRK_TERMINAL,
                tmux_session=SUP_SESSION,
                tmux_window="worker",
                provider="claude_code",
                agent_profile="kiro_dev",
            )
        )
        db.add(
            MailboxModel(
                id=WRK_MAILBOX,
                session_name=SUP_SESSION,
                role="worker",
                current_terminal_id=WRK_TERMINAL,
                generation=1,
                consumed_through_id=0,
                cc_inbox_path=None,
            )
        )
        db.add(
            MailboxIncarnationModel(mailbox_id=WRK_MAILBOX, generation=1, terminal_id=WRK_TERMINAL)
        )
        db.commit()
    return f210_db


def seed_waiting_message(db_factory, mailbox_id, receiver_id, age_s):
    """One PENDING message plus its OPEN obligation, both aged by age_s."""
    now = _utcnow()
    with db_factory() as db:
        msg = InboxModel(
            sender_id="worker01",
            receiver_id=receiver_id,
            logical_receiver_id=mailbox_id,
            message="worker report",
            status=MessageStatus.PENDING.value,
            orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            created_at=now - timedelta(seconds=age_s),
        )
        db.add(msg)
        db.flush()
        db.add(
            DeliveryObligationModel(
                inbox_row_id=msg.id,
                mailbox_id=mailbox_id,
                state="OPEN",
                accepted_at=now - timedelta(seconds=age_s),
                first_attempt_at=now - timedelta(seconds=age_s),
                next_attempt_at=now - timedelta(seconds=1),
                attempts=1,
            )
        )
        db.commit()
        return msg.id


def worker_target():
    return DeliveryTarget(
        terminal_id=WRK_TERMINAL,
        tmux_session=SUP_SESSION,
        tmux_window="worker",
        cc_inbox_path=None,
        has_registry=False,
    )


def supervisor_target():
    return DeliveryTarget(
        terminal_id=SUP_TERMINAL,
        tmux_session=SUP_SESSION,
        tmux_window="supervisor",
        cc_inbox_path=None,
        has_registry=False,
    )


def provider_patch(provider):
    return patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        return_value=provider,
    )


def empty_composer_provider():
    """Provider exposing the authority reader, reporting a stable empty composer."""
    provider = MagicMock(spec=["read_composer_draft", "read_composer_draft_authority"])
    provider.read_composer_draft.return_value = ""
    provider.read_composer_draft_authority.return_value = ("empty", False)
    return provider


# ---------------------------------------------------------------------------
# AC1 / AC2: the supervisor red leg
# ---------------------------------------------------------------------------


class TestAC1SupervisorRedLeg:
    """AC1: every gate says "inject" — and the supervisor pane is still not typed into."""

    def test_escalation_tick_fires_display_message_and_never_send_keys(self, supervisor):
        msg_id = seed_waiting_message(supervisor, SUP_MAILBOX, SUP_TERMINAL, age_s=200)

        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys") as mock_send_keys,
            patch("subprocess.run") as mock_run,
            patch("cli_agent_orchestrator.utils.tmux_command.tmux_socket_name", return_value=None),
            # Composer empty, terminal idle: the pre-F210 gates all clear.
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            provider_patch(empty_composer_provider()),
            # supervisor.mailbox_pull is False here: the exemption must not
            # depend on it (D2 / Do-NOT 3).
            config_patch(),
        ):
            mock_run.return_value.returncode = 0
            convergence_tick()

            banners = display_message_calls(mock_run)

        assert mock_send_keys.call_count == 0
        assert len(banners) == 1
        assert SUP_SESSION in _argv(banners[0])

        with supervisor() as db:
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
            assert obl.state == "ESCALATED"

    def test_exemption_defers_and_keeps_the_draft_defer_shape(self, supervisor):
        """AC2: decision/reason/delivered, and the same settle shape a draft defer produces."""
        msg_id = seed_waiting_message(supervisor, SUP_MAILBOX, SUP_TERMINAL, age_s=200)

        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys"),
            patch("subprocess.run") as mock_run,
            patch("cli_agent_orchestrator.utils.tmux_command.tmux_socket_name", return_value=None),
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            provider_patch(empty_composer_provider()),
            config_patch(),
        ):
            mock_run.return_value.returncode = 0
            result = attempt_rung2(supervisor_target(), msg_id, oldest_age_s=200.0)
            convergence_tick()

        assert result.delivered is False
        assert result.decision == "defer"
        assert result.reason == "supervisor_role_exempt"

        with supervisor() as db:
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
            assert obl.state == "ESCALATED"
            assert obl.terminal_reason == "supervisor_role_exempt"
            # F206a cadence armed exactly as a user_draft_present defer arms it.
            assert obl.next_attempt_at is not None
            assert obl.terminal_at is not None


# ---------------------------------------------------------------------------
# AC3: the interrupt rung speaks
# ---------------------------------------------------------------------------


class TestAC3InterruptRungSpeaks:
    """AC3: below the escalation threshold the supervisor still learns of the message."""

    def test_interrupt_rung_banners_once_and_stays_masked(self, supervisor):
        msg_id = seed_waiting_message(supervisor, SUP_MAILBOX, SUP_TERMINAL, age_s=60)
        boundary_pull_service.register_terminal(SUP_TERMINAL, SUP_MAILBOX)

        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys") as mock_send_keys,
            patch("subprocess.run") as mock_run,
            patch("cli_agent_orchestrator.utils.tmux_command.tmux_socket_name", return_value=None),
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            patch.object(
                boundary_pull_service,
                "mark_interrupt_fired",
                wraps=boundary_pull_service.mark_interrupt_fired,
            ) as spy_mask,
            provider_patch(empty_composer_provider()),
            config_patch(),
        ):
            mock_run.return_value.returncode = 0

            # Age 60s: past interrupt_after_s (30), below escalate_after_s (120).
            with supervisor() as db:
                obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
                _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
                db.commit()
            _fire_due_nudges()

            first_round = display_message_calls(mock_run)

            # Five further ticks, no new message and no consumption boundary.
            for _ in range(5):
                with supervisor() as db:
                    obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
                    _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
                    db.commit()
                _fire_due_nudges()

            all_rounds = display_message_calls(mock_run)

        assert mock_send_keys.call_count == 0
        assert len(first_round) == 1
        assert "1" in " ".join(_argv(first_round[0]))
        assert spy_mask.call_count == 1
        assert len(all_rounds) == 1, "the interrupt banner must not be re-emitted per tick"


# ---------------------------------------------------------------------------
# AC4-AC9: the gated rung, addressed at a non-supervisor mailbox
# ---------------------------------------------------------------------------


class TestAC4NonSupervisorLeg:
    """AC4: the gate is on role, not on "always off"."""

    def test_non_supervisor_target_still_injects(self, worker):
        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys") as mock_send_keys,
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            provider_patch(empty_composer_provider()),
            config_patch(),
        ):
            result = attempt_rung2(worker_target(), 7, message_count=2, oldest_age_s=40.0)

        assert result.delivered is True
        assert mock_send_keys.call_count == 1


class TestAC5FireTimeRecheck:
    """AC5: a draft that appears after the gate still vetoes at the sink."""

    def test_draft_appearing_after_the_gate_blocks_injection(self, worker):
        provider = MagicMock(spec=["read_composer_draft", "read_composer_draft_authority"])
        provider.read_composer_draft.return_value = ""
        provider.read_composer_draft_authority.return_value = ("nonempty", False)

        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys") as mock_send_keys,
            # The pre-fire gate saw an empty composer — on pre-F210 HEAD this
            # same setup injects, because the sink never re-read.
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            provider_patch(provider),
            config_patch(),
        ):
            result = attempt_rung2(worker_target(), 7, oldest_age_s=40.0)

        assert mock_send_keys.call_count == 0
        assert result.delivered is False
        assert result.reason == "user_draft_present"


class TestAC6UnresolvedIsFailClosed:
    """AC6: the double-capture disagreed — refuse."""

    def test_unresolved_defers(self, worker):
        provider = MagicMock(spec=["read_composer_draft", "read_composer_draft_authority"])
        provider.read_composer_draft.return_value = ""
        provider.read_composer_draft_authority.return_value = ("unresolved", False)

        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys") as mock_send_keys,
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            provider_patch(provider),
            config_patch(),
        ):
            result = attempt_rung2(worker_target(), 7, oldest_age_s=40.0)

        assert mock_send_keys.call_count == 0
        assert result.reason == "draft_unresolved"


class TestAC7CapabilityProbeDegrades:
    """AC7: providers without the authority reader (grok/codex/kiro shape) are unchanged."""

    def test_provider_without_authority_reader_still_delivers(self, worker):
        provider = MagicMock(spec=["read_composer_draft"])
        provider.read_composer_draft.return_value = None

        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys") as mock_send_keys,
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            provider_patch(provider),
            config_patch(),
        ):
            result = attempt_rung2(worker_target(), 7, oldest_age_s=40.0)

        assert result.delivered is True
        assert mock_send_keys.call_count == 1


class TestAC8NoNewlineInPayload:
    """AC8: the payload cannot submit a half-typed line by itself."""

    def test_payload_carries_no_newline_and_no_enter_count_override(self, worker):
        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys") as mock_send_keys,
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            provider_patch(empty_composer_provider()),
            config_patch(),
        ):
            attempt_rung2(worker_target(), 42, message_count=3, oldest_age_s=41.0)

        args, kwargs = mock_send_keys.call_args
        keys = args[2] if len(args) > 2 else kwargs["keys"]
        assert "\n" not in keys
        assert (
            keys
            == "[cao] 3 message(s) waiting (oldest id 42, 41s). Run list_messages to surface them."
        )
        assert "enter_count" not in kwargs


# ---------------------------------------------------------------------------
# AC9-AC11: the kill-switch
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Keep the real ConfigService but off the developer's own settings file."""
    fake_settings = tmp_path / "settings.json"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake_settings
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(cs, "LEGACY_CONFIG_FILE", tmp_path / "config.json")
    for env_name in cs.ENV_REGISTRY:
        monkeypatch.delenv(env_name, raising=False)
    return fake_settings


class TestAC9KnobKillsTheRungHot:
    """AC9: flipped live, through the real (stateless) ConfigService."""

    def test_knob_flips_both_ways_within_one_process(self, worker, isolated_settings, monkeypatch):
        provider = empty_composer_provider()
        gate = MagicMock(return_value=None)

        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys") as mock_send_keys,
            patch("cli_agent_orchestrator.services.delivery_service._check_safety_gates", gate),
            provider_patch(provider),
        ):
            # Default (armed)
            assert attempt_rung2(worker_target(), 7, oldest_age_s=40.0).delivered is True

            gate.reset_mock()
            provider.read_composer_draft_authority.reset_mock()
            monkeypatch.setenv("CAO_DELIVERY_NUDGE_SENDKEYS_ENABLED", "false")

            disabled = attempt_rung2(worker_target(), 7, oldest_age_s=40.0)

            assert disabled.delivered is False
            assert disabled.decision == "defer"
            assert disabled.reason == "sendkeys_disabled"
            assert mock_send_keys.call_count == 1, "no second injection while disabled"
            # The knob short-circuits ahead of BOTH composer captures.
            assert gate.call_count == 0
            assert provider.read_composer_draft_authority.call_count == 0

            monkeypatch.setenv("CAO_DELIVERY_NUDGE_SENDKEYS_ENABLED", "true")
            assert attempt_rung2(worker_target(), 7, oldest_age_s=40.0).delivered is True
            assert mock_send_keys.call_count == 2

    def test_escalation_floor_still_fires_while_disabled(self, supervisor, monkeypatch):
        """The kill-switch silences the injection, never the operator's banner."""
        seed_waiting_message(supervisor, SUP_MAILBOX, SUP_TERMINAL, age_s=200)

        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys") as mock_send_keys,
            patch("subprocess.run") as mock_run,
            patch("cli_agent_orchestrator.utils.tmux_command.tmux_socket_name", return_value=None),
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            provider_patch(empty_composer_provider()),
            config_patch(**{"delivery.nudge_sendkeys_enabled": False}),
        ):
            mock_run.return_value.returncode = 0
            convergence_tick()
            banners = display_message_calls(mock_run)

        assert mock_send_keys.call_count == 0
        assert len(banners) == 1


class TestAC10KeyIsRegistered:
    """AC10: an unregistered key fails at least one of these three."""

    def test_default_env_and_set_roundtrip(self, isolated_settings, monkeypatch):
        # Every delivery.* key carries its default at the call site (the section
        # is not in _OWNED_DEFAULTS), so the registered default is the one the
        # env registry declares and the one the sink passes.
        assert cs.ENV_REGISTRY["CAO_DELIVERY_NUDGE_SENDKEYS_ENABLED"] == (
            "delivery.nudge_sendkeys_enabled",
            "bool",
            True,
        )
        assert ConfigService.get("delivery.nudge_sendkeys_enabled", True) is True

        monkeypatch.setenv("CAO_DELIVERY_NUDGE_SENDKEYS_ENABLED", "false")
        assert ConfigService.get("delivery.nudge_sendkeys_enabled", True) is False
        monkeypatch.delenv("CAO_DELIVERY_NUDGE_SENDKEYS_ENABLED")

        ConfigService.set("delivery.nudge_sendkeys_enabled", False)
        assert ConfigService.get("delivery.nudge_sendkeys_enabled", True) is False
        written = json.loads(isolated_settings.read_text())
        assert written["delivery"]["nudge_sendkeys_enabled"] is False


class TestAC11KnobCannotReArmSupervisors:
    """AC11: the exemption outranks the knob in BOTH states (D10 / Do-NOT 6)."""

    @pytest.mark.parametrize("knob", [True, False])
    def test_supervisor_never_injected_in_either_knob_state(self, supervisor, knob):
        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys") as mock_send_keys,
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            provider_patch(empty_composer_provider()),
            config_patch(
                **{
                    "delivery.nudge_sendkeys_enabled": knob,
                    "supervisor.mailbox_pull": True,
                }
            ),
        ):
            result = attempt_rung2(supervisor_target(), 7, oldest_age_s=200.0, is_escalation=True)

        assert mock_send_keys.call_count == 0
        assert result.reason == "supervisor_role_exempt"


# ---------------------------------------------------------------------------
# AC12: the unregistered supervisor keeps a persistent indicator
# ---------------------------------------------------------------------------


class TestAC12PendingSurvivesEscalation:
    """AC12 (D16): @cao_pending must not be unset at the moment of urgency."""

    def test_pending_set_after_escalation_then_cleared_on_consume(self, supervisor):
        # No registry record and no cc_inbox_path: rung1 defers no_registry_records,
        # which is the F213 pre-fix degraded shape this AC is about.
        msg_id = seed_waiting_message(supervisor, SUP_MAILBOX, SUP_TERMINAL, age_s=200)

        with (
            patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys"),
            patch("subprocess.run") as mock_run,
            patch("cli_agent_orchestrator.utils.tmux_command.tmux_socket_name", return_value=None),
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            provider_patch(empty_composer_provider()),
            config_patch(),
        ):
            mock_run.return_value.returncode = 0
            convergence_tick()
            after_escalation = pending_option_calls(mock_run)

            with supervisor() as db:
                obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
                assert obl.state == "ESCALATED"

            mock_run.reset_mock()
            mock_run.return_value.returncode = 0

            # The negative half: consumption must still clear the indicator.
            with supervisor() as db:
                mailbox = db.query(MailboxModel).filter_by(id=SUP_MAILBOX).one()
                mailbox.consumed_through_id = msg_id
                msg = db.query(InboxModel).filter_by(id=msg_id).one()
                msg.status = MessageStatus.DELIVERED.value
                db.commit()

            convergence_tick()
            after_consume = pending_option_calls(mock_run)

        assert len(after_escalation) == 1, "escalation must leave a standing @cao_pending"
        assert _argv(after_escalation[0])[-2:] == ["@cao_pending", "1"]
        assert len(after_consume) == 1
        assert "-u" in _argv(after_consume[0])
