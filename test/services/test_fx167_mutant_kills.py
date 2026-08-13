"""fx167 B1: Mutant kill tests — M10, M11, M12.

M10: f138_notify_confirmed_gone_report_failed dedup (bypass-helper mutant).
M11: f162-register-inbox.sh hook exact leadSessionId match (enumerate-by-recency mutant).
M12: fx158 gate5 WARN rate-limiting (every-tick mutant).

Each test MUST fail when its targeted mutant is applied and PASS when the mutant
is reverted (proven via the kill cycle in the report).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# M10: f138_notify_confirmed_gone_report_failed routes through _f166_notify_once
# ---------------------------------------------------------------------------


class TestM10ConfirmedGoneDedup:
    """M10: Calling f138_notify_confirmed_gone_report_failed twice with the same
    failure_code emits exactly 1 notification. A mutant that bypasses the helper
    (inlines a raw create_inbox_message) emits 2 → test fails.
    """

    def test_duplicate_failure_code_emits_once(self, real_sqlite_env, monkeypatch):
        """Call f138_notify_confirmed_gone_report_failed twice with identical detail.
        Assert exactly 1 notification emitted (dedup on failure_code via _f166_notify_once).
        """
        env = real_sqlite_env
        TestSession = env["TestSession"]

        from cli_agent_orchestrator.clients.database import OrphanReconcileJobModel, ProcessIncarnationModel

        now = datetime.now(timezone.utc)
        inc_id = "inc_m10_" + str(uuid.uuid4())[:4]
        job_id = "job_m10_" + str(uuid.uuid4())[:4]
        terminal_id = "term_m10"

        with TestSession.begin() as db:
            inc = ProcessIncarnationModel(
                id=inc_id,
                terminal_id=terminal_id,
                terminal_generation=1,
                token="tok_" + inc_id,
                token_hash="hash_" + inc_id,
                owner_uid=1000,
                provider="kiro_cli",
                state="reconcile_pending",
                created_at=now,
            )
            db.add(inc)

            job = OrphanReconcileJobModel(
                id=job_id,
                incarnation_id=inc_id,
                terminal_id=terminal_id,
                terminal_generation=1,
                state="attention_required",
                attempt=8,
                gone_observed_at=now - timedelta(seconds=60),
                source="test",
                created_at=now,
                updated_at=now,
                notified_failure_code=None,
                notify_count=0,
            )
            db.add(job)

        send_calls: list[dict] = []

        def mock_create_inbox_message(**kwargs):
            send_calls.append(kwargs)

        with patch(
            "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
            return_value="supervisor_m10",
        ), patch(
            "cli_agent_orchestrator.clients.database.create_inbox_message",
            side_effect=mock_create_inbox_message,
        ):
            from cli_agent_orchestrator.services.orphan_reconcile_service import (
                f138_notify_confirmed_gone_report_failed,
            )

            # First call — should emit
            f138_notify_confirmed_gone_report_failed(
                job_id=job_id,
                terminal_id=terminal_id,
                terminal_generation=1,
                source="fifo_watchdog",
                detail="permission_denied",
                safe_reference="ref_abc",
            )

            # Second call — same detail → same failure_code → dedup suppresses
            f138_notify_confirmed_gone_report_failed(
                job_id=job_id,
                terminal_id=terminal_id,
                terminal_generation=1,
                source="fifo_watchdog",
                detail="permission_denied",
                safe_reference="ref_abc",
            )

        # M10 assertion: exactly 1 notification, not 2
        assert len(send_calls) == 1, (
            f"Expected exactly 1 notification (dedup), got {len(send_calls)}. "
            "M10 mutant (bypass _f166_notify_once) would emit 2."
        )


# ---------------------------------------------------------------------------
# M11: f162-register-inbox.sh hook selects by exact leadSessionId match only
# ---------------------------------------------------------------------------


class TestM11HookLeadSessionIdMatch:
    """M11: The hook scans config.json for leadSessionId == own session id
    and selects ONLY by exact match. An enumerate-by-recency mutant (picks
    newest session-* dir) must fail this test.
    """

    def test_selects_by_lead_session_id_not_recency(self, tmp_path):
        """Set up multiple session-* dirs under a fake $HOME; only one has
        leadSessionId matching the hook's session_id. Assert it picks that one,
        NOT the newest dir.
        """
        fake_home = tmp_path / "fakehome"
        teams_dir = fake_home / ".claude" / "teams"
        teams_dir.mkdir(parents=True)

        # Create three session dirs:
        # 1. session-aaaaaaaa: leadSessionId matches, OLDEST mtime
        # 2. session-bbbbbbbb: leadSessionId does NOT match, NEWEST mtime
        # 3. session-cccccccc: leadSessionId does NOT match, MIDDLE mtime

        target_session_id = "aaaaaaaa-1111-2222-3333-444444444444"

        dir_a = teams_dir / "session-aaaaaaaa"
        dir_a.mkdir()
        (dir_a / "config.json").write_text(json.dumps({
            "leadSessionId": target_session_id,
            "teamName": "correct-team",
        }))
        (dir_a / "inboxes").mkdir()
        (dir_a / "inboxes" / "team-lead.json").write_text("[]")
        # Set old mtime
        os.utime(dir_a, (1000000000, 1000000000))

        dir_b = teams_dir / "session-bbbbbbbb"
        dir_b.mkdir()
        (dir_b / "config.json").write_text(json.dumps({
            "leadSessionId": "bbbbbbbb-5555-6666-7777-888888888888",
            "teamName": "wrong-team-newest",
        }))
        (dir_b / "inboxes").mkdir()
        (dir_b / "inboxes" / "team-lead.json").write_text("[]")
        # Set newest mtime
        os.utime(dir_b, (2000000000, 2000000000))

        dir_c = teams_dir / "session-cccccccc"
        dir_c.mkdir()
        (dir_c / "config.json").write_text(json.dumps({
            "leadSessionId": "cccccccc-9999-aaaa-bbbb-cccccccccccc",
            "teamName": "wrong-team-middle",
        }))
        (dir_c / "inboxes").mkdir()
        (dir_c / "inboxes" / "team-lead.json").write_text("[]")

        # Hook script path
        hook_path = Path(__file__).resolve().parents[3] / ".claude" / "hooks" / "f162-register-inbox.sh"
        if not hook_path.exists():
            # Fallback: try root repo
            hook_path = Path("/home/chao/VScode_projects/cli-subagents/.claude/hooks/f162-register-inbox.sh")

        assert hook_path.exists(), f"Hook not found at {hook_path}"

        # Provide the session_id as stdin JSON (as SessionStart hook does)
        stdin_json = json.dumps({"session_id": target_session_id})

        # Mock the curl call by intercepting it — use a wrapper script
        wrapper_script = tmp_path / "run_hook.sh"
        wrapper_script.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
export HOME="{fake_home}"
export CAO_TERMINAL_ID="test_terminal"
export CAO_PORT="19999"
# Override curl to capture the payload instead of making a real request
export PATH="{tmp_path / 'bin'}:$PATH"
echo '{stdin_json}' | bash "{hook_path}"
""")
        wrapper_script.chmod(0o755)

        # Create a fake curl that captures its arguments
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl_capture = tmp_path / "curl_capture.json"
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(f"""#!/usr/bin/env bash
# Capture the -d payload argument
for i in "${{@}}"; do
    if [[ "$prev" == "-d" ]]; then
        echo "$i" > "{curl_capture}"
        exit 0
    fi
    prev="$i"
done
exit 0
""")
        fake_curl.chmod(0o755)

        result = subprocess.run(
            ["bash", str(wrapper_script)],
            capture_output=True, text=True, timeout=10,
        )

        # The hook should have called curl with the correct inbox path
        assert result.returncode == 0, f"Hook failed: stderr={result.stderr}"
        assert curl_capture.exists(), (
            f"Hook did not call curl (no metadata update). stdout={result.stdout} stderr={result.stderr}"
        )

        payload = json.loads(curl_capture.read_text())
        registered_path = payload.get("metadata", {}).get("cc_team_inbox_path", "")

        # M11 assertion: must be the dir_a inbox (matched by leadSessionId),
        # NOT dir_b (newest) or dir_c.
        expected_path = str(dir_a / "inboxes" / "team-lead.json")
        assert registered_path == expected_path, (
            f"Hook registered wrong path: {registered_path}\n"
            f"Expected (by leadSessionId match): {expected_path}\n"
            "M11 mutant (enumerate-by-recency) would pick session-bbbbbbbb instead."
        )

    def test_zero_matches_registers_nothing(self, tmp_path):
        """When no config.json has a matching leadSessionId, the hook registers
        nothing and warns to stderr."""
        fake_home = tmp_path / "fakehome"
        teams_dir = fake_home / ".claude" / "teams"
        teams_dir.mkdir(parents=True)

        # One dir that does NOT match
        dir_a = teams_dir / "session-aaaaaaaa"
        dir_a.mkdir()
        (dir_a / "config.json").write_text(json.dumps({
            "leadSessionId": "aaaaaaaa-1111-2222-3333-444444444444",
        }))
        (dir_a / "inboxes").mkdir()
        (dir_a / "inboxes" / "team-lead.json").write_text("[]")

        # Hook uses a DIFFERENT session_id that matches nothing
        target_session_id = "zzzzzzzz-0000-0000-0000-000000000000"
        stdin_json = json.dumps({"session_id": target_session_id})

        hook_path = Path("/home/chao/VScode_projects/cli-subagents/.claude/hooks/f162-register-inbox.sh")
        assert hook_path.exists()

        wrapper_script = tmp_path / "run_hook.sh"
        wrapper_script.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
export HOME="{fake_home}"
export CAO_TERMINAL_ID="test_terminal"
export CAO_PORT="19999"
export PATH="{tmp_path / 'bin'}:$PATH"
echo '{stdin_json}' | bash "{hook_path}"
""")
        wrapper_script.chmod(0o755)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl_capture = tmp_path / "curl_capture.json"
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(f"""#!/usr/bin/env bash
for i in "${{@}}"; do
    if [[ "$prev" == "-d" ]]; then
        echo "$i" > "{curl_capture}"
        exit 0
    fi
    prev="$i"
done
exit 0
""")
        fake_curl.chmod(0o755)

        result = subprocess.run(
            ["bash", str(wrapper_script)],
            capture_output=True, text=True, timeout=10,
        )

        assert result.returncode == 0, f"Hook failed: {result.stderr}"
        # Should NOT have called curl (no match → register nothing)
        assert not curl_capture.exists(), (
            f"Hook called curl despite zero leadSessionId matches. "
            f"Payload: {curl_capture.read_text() if curl_capture.exists() else 'N/A'}"
        )
        # Should have warned to stderr
        assert "warn" in result.stderr.lower() or "no match" in result.stderr.lower() or \
               "0 match" in result.stderr.lower() or result.stderr.strip() != "", (
            "Hook should warn to stderr when no match found"
        )

    def test_multiple_matches_registers_nothing(self, tmp_path):
        """When multiple config.json files have the same leadSessionId, the hook
        registers nothing (ambiguous → mute is safer than wrong-inbox)."""
        fake_home = tmp_path / "fakehome"
        teams_dir = fake_home / ".claude" / "teams"
        teams_dir.mkdir(parents=True)

        target_session_id = "aaaaaaaa-1111-2222-3333-444444444444"

        # Two dirs BOTH matching
        for name in ["session-aaaaaaaa", "session-dddddddd"]:
            d = teams_dir / name
            d.mkdir()
            (d / "config.json").write_text(json.dumps({
                "leadSessionId": target_session_id,
            }))
            (d / "inboxes").mkdir()
            (d / "inboxes" / "team-lead.json").write_text("[]")

        stdin_json = json.dumps({"session_id": target_session_id})
        hook_path = Path("/home/chao/VScode_projects/cli-subagents/.claude/hooks/f162-register-inbox.sh")
        assert hook_path.exists()

        wrapper_script = tmp_path / "run_hook.sh"
        wrapper_script.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
export HOME="{fake_home}"
export CAO_TERMINAL_ID="test_terminal"
export CAO_PORT="19999"
export PATH="{tmp_path / 'bin'}:$PATH"
echo '{stdin_json}' | bash "{hook_path}"
""")
        wrapper_script.chmod(0o755)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl_capture = tmp_path / "curl_capture.json"
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(f"""#!/usr/bin/env bash
for i in "${{@}}"; do
    if [[ "$prev" == "-d" ]]; then
        echo "$i" > "{curl_capture}"
        exit 0
    fi
    prev="$i"
done
exit 0
""")
        fake_curl.chmod(0o755)

        result = subprocess.run(
            ["bash", str(wrapper_script)],
            capture_output=True, text=True, timeout=10,
        )

        assert result.returncode == 0, f"Hook failed: {result.stderr}"
        # Should NOT have called curl (multiple matches → register nothing)
        assert not curl_capture.exists(), (
            f"Hook called curl despite multiple leadSessionId matches (ambiguous). "
            f"Payload: {curl_capture.read_text() if curl_capture.exists() else 'N/A'}"
        )


# ---------------------------------------------------------------------------
# M12: fx158 gate5 WARN rate-limiting (60s suppression with fake clock)
# ---------------------------------------------------------------------------


class TestM12Gate5WarnRateLimit:
    """M12: fx158_gate5_unregistered WARN is emitted on transition then
    suppressed inside 60s. An every-tick mutant (no rate-limit) fails.
    """

    def test_warn_once_then_suppressed_within_60s(self, real_sqlite_env, monkeypatch):
        """Drive reconcile_pull_mode_notifications across 3 ticks with an
        unregistered supervisor. Assert 1 WARN (transition) then suppression.
        Uses a fake time.monotonic to control the clock.
        """
        env = real_sqlite_env
        TestSession = env["TestSession"]

        from cli_agent_orchestrator.clients.database import (
            InboxModel,
            MailboxModel,
            TerminalModel,
        )

        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=120)

        with TestSession.begin() as db:
            terminal = TerminalModel(
                id="unreg_sup",
                tmux_session="test-sess",
                tmux_window="win-unreg",
                provider="kiro_cli",
                agent_profile="developer",
                lifecycle="sticky",
                init_state="ready",
                lifecycle_generation=1,
                metadata_json="{}",  # NO cc_team_inbox_path → unregistered
            )
            db.add(terminal)

            mailbox = MailboxModel(
                id="mb_unreg",
                session_name="test-sess",
                role="supervisor",
                current_terminal_id="unreg_sup",
                generation=1,
                consumed_through_id=0,
                schema_version=1,
            )
            db.add(mailbox)

            inbox_msg = InboxModel(
                sender_id="worker01",
                receiver_id="unreg_sup",
                logical_receiver_id="mb_unreg",
                message="pending task result",
                orchestration_type="send_message",
                status="pending",
                created_at=old,
            )
            db.add(inbox_msg)

        # Patch: pull-mode on, supervisor is pull-mode, teammate_push returns False (unregistered)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda key, default=None, override=None: {
                "supervisor.mailbox_pull": True,
                "supervisor.teammate_push": True,
            }.get(key, default)),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
            lambda tid: tid == "unreg_sup",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
            lambda tid: False,  # Unregistered
        )

        # Fake clock: starts at 1000.0, can be advanced
        fake_time = [1000.0]

        from cli_agent_orchestrator.services import inbox_service as _is_mod

        # Clear any pre-existing state in the rate-limit dict
        _is_mod._fx158_gate5_last_warn.clear()

        monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])

        # Patch list_pending helpers to prevent other code paths
        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_older_than", lambda seconds: [])
        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_with_terminal", lambda: [])

        # Capture log warnings
        warn_calls: list[str] = []
        original_warning = _is_mod.logger.warning

        def capture_warning(msg, *args):
            formatted = msg % args if args else msg
            warn_calls.append(formatted)

        monkeypatch.setattr(_is_mod.logger, "warning", capture_warning)

        from cli_agent_orchestrator.services.inbox_service import InboxService

        monkeypatch.setattr(InboxService, "recover_stale_deliveries", lambda self, **kw: None)

        svc = InboxService()

        # Tick 1: first observation → should WARN (transition)
        svc.reconcile_pull_mode_notifications()
        tick1_warns = [w for w in warn_calls if "fx158_gate5_unregistered" in w]
        assert len(tick1_warns) == 1, f"Tick 1: expected 1 WARN, got {len(tick1_warns)}: {tick1_warns}"

        # Tick 2: +30s (within 60s window) → suppressed
        fake_time[0] = 1030.0
        warn_calls.clear()
        svc.reconcile_pull_mode_notifications()
        tick2_warns = [w for w in warn_calls if "fx158_gate5_unregistered" in w]
        assert len(tick2_warns) == 0, (
            f"Tick 2 (+30s): expected 0 WARN (suppressed within 60s), got {len(tick2_warns)}. "
            "M12 mutant (every-tick emit) would emit here."
        )

        # Tick 3: +59s (still within 60s window) → suppressed
        fake_time[0] = 1059.0
        warn_calls.clear()
        svc.reconcile_pull_mode_notifications()
        tick3_warns = [w for w in warn_calls if "fx158_gate5_unregistered" in w]
        assert len(tick3_warns) == 0, (
            f"Tick 3 (+59s): expected 0 WARN (suppressed), got {len(tick3_warns)}"
        )

    def test_warn_re_emits_after_60s(self, real_sqlite_env, monkeypatch):
        """After 60s elapse, the WARN is emitted again (rate-limit window expired)."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        from cli_agent_orchestrator.clients.database import (
            InboxModel,
            MailboxModel,
            TerminalModel,
        )

        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=120)

        with TestSession.begin() as db:
            terminal = TerminalModel(
                id="unreg_s2",
                tmux_session="test-sess",
                tmux_window="win-unreg2",
                provider="kiro_cli",
                agent_profile="developer",
                lifecycle="sticky",
                init_state="ready",
                lifecycle_generation=1,
                metadata_json="{}",
            )
            db.add(terminal)

            mailbox = MailboxModel(
                id="mb_unreg2",
                session_name="test-sess",
                role="supervisor",
                current_terminal_id="unreg_s2",
                generation=1,
                consumed_through_id=0,
                schema_version=1,
            )
            db.add(mailbox)

            inbox_msg = InboxModel(
                sender_id="worker02",
                receiver_id="unreg_s2",
                logical_receiver_id="mb_unreg2",
                message="pending msg",
                orchestration_type="send_message",
                status="pending",
                created_at=old,
            )
            db.add(inbox_msg)

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda key, default=None, override=None: {
                "supervisor.mailbox_pull": True,
            }.get(key, default)),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
            lambda tid: tid == "unreg_s2",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
            lambda tid: False,
        )

        fake_time = [2000.0]

        from cli_agent_orchestrator.services import inbox_service as _is_mod
        _is_mod._fx158_gate5_last_warn.clear()

        monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])
        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_older_than", lambda seconds: [])
        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_with_terminal", lambda: [])

        warn_calls: list[str] = []

        def capture_warning(msg, *args):
            formatted = msg % args if args else msg
            warn_calls.append(formatted)

        monkeypatch.setattr(_is_mod.logger, "warning", capture_warning)

        from cli_agent_orchestrator.services.inbox_service import InboxService
        monkeypatch.setattr(InboxService, "recover_stale_deliveries", lambda self, **kw: None)

        svc = InboxService()

        # Tick 1: transition WARN
        svc.reconcile_pull_mode_notifications()
        assert any("fx158_gate5_unregistered" in w for w in warn_calls)

        # Tick 2: +61s → re-emit (window expired)
        fake_time[0] = 2061.0
        warn_calls.clear()
        svc.reconcile_pull_mode_notifications()
        tick2_warns = [w for w in warn_calls if "fx158_gate5_unregistered" in w]
        assert len(tick2_warns) == 1, (
            f"After 61s: expected WARN re-emission, got {len(tick2_warns)}"
        )
