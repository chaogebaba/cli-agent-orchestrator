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

        from cli_agent_orchestrator.clients.database import (
            OrphanReconcileJobModel,
            ProcessIncarnationModel,
        )

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

        with (
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
                return_value="supervisor_m10",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.create_inbox_message",
                side_effect=mock_create_inbox_message,
            ),
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
# M11: f162-register-inbox.sh hook selects by exact teamName key match only
# ---------------------------------------------------------------------------


def _find_hook_path() -> Path:
    """Locate f162-register-inbox.sh via ROOT_REPO conftest helper (worktree-safe)."""
    from test.conftest import ROOT_REPO

    if ROOT_REPO is not None:
        candidate = ROOT_REPO / ".claude" / "hooks" / "f162-register-inbox.sh"
        if candidate.exists():
            return candidate
    # Direct walk fallback
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".claude" / "hooks" / "f162-register-inbox.sh"
        if candidate.exists():
            return candidate
    pytest.skip("f162-register-inbox.sh not found in any ancestor")


class TestM11HookLeadSessionIdMatch:
    """M11: The D9 hook scans subagent meta.json files for exact teamName key
    match on the OWN session_id. An enumerate-by-recency mutant (picks newest
    team dir by mtime) must fail this test.
    """

    def test_selects_by_lead_session_id_not_recency(self, tmp_path):
        """Set up multiple project session dirs under a fake $HOME; only one has
        a meta.json with a teamName. Create teams dirs with varying mtime.
        Assert the hook picks the dir whose teamName matches (exact key), NOT
        the newest teams/ dir by mtime.
        """
        fake_home = tmp_path / "fakehome"

        target_session_id = "aaaaaaaa-1111-2222-3333-444444444444"

        # D9 hook scans: ~/.claude/projects/*/SESSION_ID/subagents/*.meta.json
        project_dir = (
            fake_home / ".claude" / "projects" / "myproject" / target_session_id / "subagents"
        )
        project_dir.mkdir(parents=True)
        (project_dir / "agent1.meta.json").write_text(
            json.dumps(
                {
                    "teamName": "correct-team",
                }
            )
        )

        # A second project dir for a DIFFERENT session (should NOT be scanned)
        other_session = "bbbbbbbb-5555-6666-7777-888888888888"
        other_dir = fake_home / ".claude" / "projects" / "myproject" / other_session / "subagents"
        other_dir.mkdir(parents=True)
        (other_dir / "agent2.meta.json").write_text(
            json.dumps(
                {
                    "teamName": "wrong-team",
                }
            )
        )

        # Create the teams directories — the hook will register
        # ~/.claude/teams/<teamName>/inboxes/team-lead.json
        correct_team_dir = fake_home / ".claude" / "teams" / "correct-team"
        correct_team_dir.mkdir(parents=True)
        (correct_team_dir / "inboxes").mkdir()
        (correct_team_dir / "inboxes" / "team-lead.json").write_text("[]")
        # Set OLDEST mtime on the correct team dir
        os.utime(correct_team_dir, (1000000000, 1000000000))

        wrong_team_dir = fake_home / ".claude" / "teams" / "wrong-team"
        wrong_team_dir.mkdir(parents=True)
        (wrong_team_dir / "inboxes").mkdir()
        (wrong_team_dir / "inboxes" / "team-lead.json").write_text("[]")
        # Set NEWEST mtime — a recency mutant would pick this
        os.utime(wrong_team_dir, (2000000000, 2000000000))

        hook_path = _find_hook_path()

        stdin_json = json.dumps({"session_id": target_session_id})

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
            capture_output=True,
            text=True,
            timeout=10,
        )

        # The hook should have called curl with the correct inbox path
        assert result.returncode == 0, f"Hook failed: stderr={result.stderr}"
        assert (
            curl_capture.exists()
        ), f"Hook did not call curl (no metadata update). stdout={result.stdout} stderr={result.stderr}"

        payload = json.loads(curl_capture.read_text())
        registered_path = payload.get("metadata", {}).get("cc_team_inbox_path", "")

        # M11 assertion: must be the correct-team inbox (matched by exact
        # teamName key from OWN session's meta.json), NOT wrong-team (newest
        # mtime). A recency mutant would pick wrong-team.
        expected_path = str(correct_team_dir / "inboxes" / "team-lead.json")
        assert registered_path == expected_path, (
            f"Hook registered wrong path: {registered_path}\n"
            f"Expected (by exact teamName match): {expected_path}\n"
            "M11 mutant (enumerate-by-recency) would pick wrong-team instead."
        )

    def test_zero_matches_registers_nothing(self, tmp_path):
        """When no meta.json has a teamName for the given session, the hook
        registers nothing and warns to stderr."""
        fake_home = tmp_path / "fakehome"

        # Session with NO meta.json files at all
        target_session_id = "zzzzzzzz-0000-0000-0000-000000000000"
        # Create the projects dir structure but no meta.json
        project_dir = (
            fake_home / ".claude" / "projects" / "myproject" / target_session_id / "subagents"
        )
        project_dir.mkdir(parents=True)

        stdin_json = json.dumps({"session_id": target_session_id})
        hook_path = _find_hook_path()

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
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0, f"Hook failed: {result.stderr}"
        # Should NOT have called curl (no teamName match → register nothing)
        assert not curl_capture.exists(), (
            f"Hook called curl despite zero teamName matches. "
            f"Payload: {curl_capture.read_text() if curl_capture.exists() else 'N/A'}"
        )
        # Should have warned to stderr
        assert (
            "warn" in result.stderr.lower()
            or "no match" in result.stderr.lower()
            or "0 match" in result.stderr.lower()
            or result.stderr.strip() != ""
        ), "Hook should warn to stderr when no match found"

    def test_multiple_matches_registers_nothing(self, tmp_path):
        """When multiple meta.json files yield distinct teamNames for the same
        session, the hook registers nothing (ambiguous → mute is safer than
        wrong-inbox)."""
        fake_home = tmp_path / "fakehome"

        target_session_id = "aaaaaaaa-1111-2222-3333-444444444444"

        # Two meta.json files with DIFFERENT teamNames under the same session
        project_dir = (
            fake_home / ".claude" / "projects" / "myproject" / target_session_id / "subagents"
        )
        project_dir.mkdir(parents=True)
        (project_dir / "agent1.meta.json").write_text(
            json.dumps(
                {
                    "teamName": "team-alpha",
                }
            )
        )
        (project_dir / "agent2.meta.json").write_text(
            json.dumps(
                {
                    "teamName": "team-beta",
                }
            )
        )

        # Create both team dirs
        for tn in ["team-alpha", "team-beta"]:
            td = fake_home / ".claude" / "teams" / tn
            td.mkdir(parents=True)
            (td / "inboxes").mkdir()
            (td / "inboxes" / "team-lead.json").write_text("[]")

        stdin_json = json.dumps({"session_id": target_session_id})
        hook_path = _find_hook_path()

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
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0, f"Hook failed: {result.stderr}"
        # Should NOT have called curl (multiple distinct teamNames → ambiguous)
        assert not curl_capture.exists(), (
            f"Hook called curl despite multiple distinct teamNames (ambiguous). "
            f"Payload: {curl_capture.read_text() if curl_capture.exists() else 'N/A'}"
        )


# ---------------------------------------------------------------------------
# M12: the native_fallback_engaged WARN rate-limit (60s window, fake clock)
# ---------------------------------------------------------------------------
# WP-ARCH 3c K2 took M12's DRIVER and its state, but not its subject.
#
# Both arms used to seed an unregistered supervisor, turn on
# ``supervisor.mailbox_pull`` + ``supervisor.teammate_push``, and tick
# ``InboxService.reconcile_pull_mode_notifications`` three times, counting WARNs
# against ``inbox_service._fx158_gate5_last_warn``. The reconciler, both flags and
# that dict are all deleted with the legacy pull-mode carrier.
#
# The RATE LIMIT itself survived K2: it moved, with the rest of the native health
# probe, into ``services/native_delivery_health.log_native_fallback_engaged``,
# keyed by ``_native_fallback_last_warn`` and bounded by
# ``NATIVE_FALLBACK_WARN_INTERVAL_S``. Its production caller is the
# ``/terminals/{id}/native-delivery`` probe. So M12 follows the mechanism to its
# new home rather than dying with the sweep that used to drive it.
#
# ONE of the two arms survives here, and deliberately only one.
# ``test_warn_once_then_suppressed_within_60s`` asserted "emit on transition, then
# suppress inside the window" — exactly what
# ``test_f747_native_default.test_engagement_warn_is_rate_limited`` already
# asserts against the surviving function, and keeping a second copy of it would be
# the duplicate this file's mutant-kill framing has no use for. What that arm does
# NOT cover is the other direction, and it is the direction M12's mutant lives in:
# an "emit once per terminal, ever" mutant passes a suppression-only test and
# fails this one.


class TestM12WarnRateLimit:
    """M12: the engagement WARN re-emits once the 60s window expires.

    The paired direction (emit on transition, suppress inside the window) is
    owned by ``test_f747_native_default.test_engagement_warn_is_rate_limited``.
    Together they pin a RATE LIMIT rather than a one-shot latch.
    """

    def test_warn_re_emits_after_the_window(self, monkeypatch):
        """MUTANT: a latch that never re-emits survives a suppression-only test."""
        from cli_agent_orchestrator.services import native_delivery_health as ndh

        ndh._native_fallback_last_warn.clear()

        fake_time = [2000.0]
        monkeypatch.setattr(ndh.time, "monotonic", lambda: fake_time[0])

        warn_calls: list[str] = []

        def capture_warning(msg, *args):
            warn_calls.append(msg % args if args else msg)

        monkeypatch.setattr(ndh.logger, "warning", capture_warning)

        # Transition: the WARN is emitted.
        assert ndh.log_native_fallback_engaged("unreg_sup", "no_inbox_path") is True

        # Inside the window: suppressed, and nothing reached the logger.
        fake_time[0] = 2000.0 + ndh.NATIVE_FALLBACK_WARN_INTERVAL_S - 1.0
        assert ndh.log_native_fallback_engaged("unreg_sup", "no_inbox_path") is False
        assert len(warn_calls) == 1, warn_calls

        # Past the window: re-emitted. A one-shot latch fails HERE.
        fake_time[0] = 2000.0 + ndh.NATIVE_FALLBACK_WARN_INTERVAL_S + 1.0
        assert ndh.log_native_fallback_engaged("unreg_sup", "no_inbox_path") is True
        reemits = [w for w in warn_calls if "native_fallback_engaged" in w]
        assert len(reemits) == 2, f"expected a re-emission past the window, got {warn_calls}"
        assert "terminal=unreg_sup" in reemits[1]
        assert "reason=no_inbox_path" in reemits[1]
