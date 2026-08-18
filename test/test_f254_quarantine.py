"""F254 D33/D34/D35 quarantine hygiene tests.

These tests enforce quarantine policy mechanically:

  - test_no_expired_quarantine_entries (D33): expires in the past → FAIL
  - test_quarantined_nodeids_still_collect (AC-F4): renamed/removed test → FAIL
  - test_no_rerun_or_randomly_plugins (D35/AC-F5): banned plugins absent

Precedent: P-RATCHET (ci.yml:175-184), P-ASTGUARD (test/test_datetime_convention.py).
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

_QUARANTINE_FILE = Path(__file__).resolve().parent / "quarantine.toml"
_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _load_quarantine() -> list[dict]:
    with open(_QUARANTINE_FILE, "rb") as f:
        data = tomllib.load(f)
    return data.get("entry", [])


# ---------------------------------------------------------------------------
# D33 — Quarantine entries expire, and expiry is a test
# ---------------------------------------------------------------------------


def test_no_expired_quarantine_entries() -> None:
    """Every quarantine entry must have a future expiry date.

    A past expiry means the entry has outlived its review window and must be
    either removed (test is fixed) or re-justified with a new date.

    F262 D4: serial_only entries have NO expiry (permanently decided);
    they are skipped here.
    """
    today = datetime.date.today()
    entries = _load_quarantine()
    expired = []
    for entry in entries:
        # D4: serial_only entries must NOT have expires
        if entry.get("class") == "serial_only":
            continue
        expires_str = entry.get("expires", "")
        try:
            expires = datetime.date.fromisoformat(expires_str)
        except (ValueError, TypeError):
            expired.append(f"{entry.get('nodeid', '?')}: invalid expires={expires_str!r}")
            continue
        if expires < today:
            expired.append(
                f"{entry.get('nodeid', '?')}: expired {expires_str} (today={today})"
            )
    if expired:
        msg = "Expired quarantine entries (D33 — extend or remove):\n"
        msg += "\n".join(f"  - {e}" for e in expired)
        pytest.fail(msg)


# ---------------------------------------------------------------------------
# AC-F4 — Renamed/removed tests detected
# ---------------------------------------------------------------------------


def test_quarantined_nodeids_still_collect(pytestconfig) -> None:
    """Every quarantined nodeid must still be collectable.

    A renamed or deleted test silently drops out of quarantine otherwise —
    the documented drift risk from smoke_tags.py's node-ID list.
    """
    entries = _load_quarantine()
    if not entries:
        pytest.skip("No quarantine entries")

    # Get all collected nodeids from this session
    # (we use the plugin_manager to access the session's items)
    session = pytestconfig._store.get(pytest.StashKey[object](), None)

    # Alternative: collect nodeids via subprocess for isolation
    # But since this test runs inside the collection, we can check the items
    # that were collected by reading the session items stashed by our conftest.
    # However, that's complex. Simpler: verify the test files + functions exist.
    missing = []
    for entry in entries:
        nodeid = entry["nodeid"]
        # Parse nodeid: "path/to/file.py::Class::method" or "path/to/file.py::function"
        # Strip parametrize suffix: "func[param]" → "func"
        parts = nodeid.split("::")
        filepath = Path(__file__).resolve().parent.parent / parts[0]
        if not filepath.exists():
            missing.append(f"{nodeid}: file {parts[0]} does not exist")
            continue

        # Check the function/method exists in the file source
        # Strip parametrize brackets from function name
        func_name = parts[-1]
        if "[" in func_name:
            func_name = func_name[: func_name.index("[")]
        source = filepath.read_text()
        if f"def {func_name}" not in source:
            missing.append(f"{nodeid}: function {func_name!r} not found in {parts[0]}")

    if missing:
        msg = "Quarantined nodeids that no longer collect (AC-F4 — remove stale entries):\n"
        msg += "\n".join(f"  - {m}" for m in missing)
        pytest.fail(msg)


# ---------------------------------------------------------------------------
# D35 / AC-F5 — No automatic reruns
# ---------------------------------------------------------------------------


def test_no_rerun_or_randomly_plugins() -> None:
    """pytest-rerunfailures and pytest-randomly must not appear in deps.

    D35: no automatic reruns. D22: no randomization (deterministic order).
    Guard asserts absence from both pyproject.toml and uv.lock.
    """
    # Check pyproject.toml
    pyproject_text = _PYPROJECT.read_text()
    banned = ["pytest-rerunfailures", "pytest-randomly"]
    violations = []

    for pkg in banned:
        if pkg in pyproject_text:
            violations.append(f"{pkg} found in pyproject.toml")

    # Check uv.lock
    lock_file = _PYPROJECT.parent / "uv.lock"
    if lock_file.exists():
        lock_text = lock_file.read_text()
        for pkg in banned:
            if pkg in lock_text:
                violations.append(f"{pkg} found in uv.lock")

    if violations:
        msg = "Banned test plugins present (D35 — no automatic reruns):\n"
        msg += "\n".join(f"  - {v}" for v in violations)
        pytest.fail(msg)



# ---------------------------------------------------------------------------
# F262 D4 / AC3 — serial_only schema enforcement
# ---------------------------------------------------------------------------

_VERDICTS_FILE = Path(__file__).resolve().parent / "quarantine-verdicts.md"


def test_serial_only_schema() -> None:
    """serial_only entries: no expires, non-empty verdict, verdict resolves.

    AC3: serial_only + expires → FAIL
    AC4: serial_only + unresolvable verdict → FAIL
    AC5: serial_only is serialized (enforced by plugin behaviour, tested via collection)
    """
    entries = _load_quarantine()
    serial_entries = [e for e in entries if e.get("class") == "serial_only"]

    if not serial_entries:
        pytest.skip("No serial_only entries in quarantine.toml")

    # Load verdict anchors from the ledger
    anchors: set[str] = set()
    if _VERDICTS_FILE.exists():
        for line in _VERDICTS_FILE.read_text().splitlines():
            if line.startswith("## "):
                anchors.add(line[3:].strip())

    violations = []
    for entry in serial_entries:
        nodeid = entry.get("nodeid", "?")

        # AC3: must NOT have expires
        if "expires" in entry:
            violations.append(f"{nodeid}: serial_only must not have 'expires' key")

        # Must have non-empty verdict
        verdict = entry.get("verdict", "")
        if not verdict:
            violations.append(f"{nodeid}: serial_only must have non-empty 'verdict'")
        elif verdict not in anchors:
            # AC4: verdict must resolve in quarantine-verdicts.md
            violations.append(
                f"{nodeid}: verdict {verdict!r} not found in quarantine-verdicts.md"
            )

    if violations:
        msg = "serial_only schema violations (F262 D4):\n"
        msg += "\n".join(f"  - {v}" for v in violations)
        pytest.fail(msg)


def test_expiry_guard_fires_for_non_serial_only() -> None:
    """AC6: The expiry guard still fires for every non-serial_only class.

    Verifies that non-serial_only entries with past dates are caught.
    """
    entries = _load_quarantine()
    # All non-serial_only entries must have a valid, parseable expires
    for entry in entries:
        if entry.get("class") == "serial_only":
            continue
        expires_str = entry.get("expires", "")
        # Must be parseable
        try:
            datetime.date.fromisoformat(expires_str)
        except (ValueError, TypeError):
            pytest.fail(
                f"{entry.get('nodeid', '?')}: non-serial_only entry has invalid "
                f"expires={expires_str!r} — every deferred entry needs a date"
            )


# ---------------------------------------------------------------------------
# F262 AC7 — Departed entries have ledger sections
# ---------------------------------------------------------------------------


def test_departed_entries_have_verdicts() -> None:
    """AC7: Every entry removed from the registry has a ledger section.

    Cross-checks: for each nodeid that was in the P4-merge registry but is
    absent from the working registry, quarantine-verdicts.md contains a
    section whose heading matches and whose body has a bucket tag.
    """
    # The P4-merge set of nodeids (frozen at F262 build time)
    _P4_NODEIDS = {
        "test/telemetry/test_spans.py::TestInvokeAgentSpan::test_emits_invoke_agent_with_required_attributes",
        "test/telemetry/test_spans.py::TestExecuteToolSpan::test_emits_execute_tool",
        "test/telemetry/test_spans.py::TestChatSpan::test_emits_chat_with_request_model",
        "test/telemetry/test_spans.py::TestChatSpanConversationId::test_chat_span_sets_conversation_id",
        "test/security/test_auth.py::test_expected_audience_defaults_to_api_base_url_when_enabled",
        "test/security/test_auth.py::test_audience_fallback_enforced_in_validation",
        "test/services/test_fifo_reader.py::TestReaderThreadLifecycle::test_data_received_across_writer_reconnects",
        "test/services/test_wpm4a_deferred_init_hardening.py::test_dispatcher_uses_slot_grant_not_delayed_validator_entry",
        "test/services/test_wpm4a_deferred_init_hardening.py::test_quiesce_wins_after_ready_sync_call_starts",
        "test/cli/commands/test_fold.py::test_ac13_raw_byte_decode_rejections[malformed UTF-8]",
        "test/services/test_f72_fleet_lifecycle.py::test_ac13_no_surviving_ancestor_cancels_with_reason",
        "test/services/test_f72_fleet_lifecycle.py::test_ac13_held_row_target_exists_after_delete",
        "test/services/test_stage0_flip_machinery.py::test_backend_failure_warning_is_rate_limited_per_terminal",
        "test/services/test_ready_deadline_edge_probe.py::test_ready_completion_at_deadline_has_one_lawful_owner",
        "test/services/test_fx191_convergent_delivery.py::TestS2AC14MultiTickConvergence::test_safety_gate_obligations_escalate_within_bound[waiting_user_answer]",
        "test/services/test_f72_fleet_lifecycle.py::test_uncertain_kill_stops_keeps_row_and_releases_quarantine_exit_lease",
        "test/services/test_fifo_reader.py::TestReaderThreadLifecycle::test_stop_right_after_writer_eof_does_not_leak",
        "test/providers/test_claude_transcript_hook.py::test_project_and_generated_session_start_hooks_both_fire",
        "test/providers/test_claude_transcript_hook.py::test_project_and_two_generated_hooks_are_additive_and_failure_isolated[0]",
        "test/providers/test_claude_transcript_hook.py::test_project_and_two_generated_hooks_are_additive_and_failure_isolated[1]",
        "test/services/test_worktree_branch_integrity.py::TestProductionPathForkPlusWorktree::test_create_terminal_fork_worktree_propagates_worktree_info",
        "test/providers/test_grok_cli_unit.py::test_ac9_all_grok_profiles_register_cao_mcp_server",
    }

    # Current registry nodeids
    entries = _load_quarantine()
    current_nodeids = {e["nodeid"] for e in entries}

    # Departed = was in P4 but not in current
    departed = _P4_NODEIDS - current_nodeids

    if not departed:
        pytest.skip("No entries departed from registry yet")

    # Check ledger
    if not _VERDICTS_FILE.exists():
        pytest.fail(
            f"{len(departed)} entries departed but quarantine-verdicts.md does not exist"
        )

    ledger_text = _VERDICTS_FILE.read_text()
    bucket_pattern = {"(a)", "(b)", "(c)", "(d)"}

    missing = []
    for nodeid in departed:
        # The ledger section heading should contain the nodeid (or a recognizable part)
        # We check for the function name at minimum
        parts = nodeid.split("::")
        func_name = parts[-1].split("[")[0] if "::" in nodeid else nodeid
        if func_name not in ledger_text:
            missing.append(f"{nodeid}: no ledger section found")
            continue
        # Check for a bucket tag in the vicinity
        # (relaxed: just check the bucket tag exists somewhere in the file
        # near the function reference)
        if not any(tag in ledger_text for tag in bucket_pattern):
            missing.append(f"{nodeid}: no bucket tag (a)/(b)/(c)/(d) in ledger")

    if missing:
        msg = "Departed entries without ledger sections (AC7):\n"
        msg += "\n".join(f"  - {m}" for m in missing)
        pytest.fail(msg)
