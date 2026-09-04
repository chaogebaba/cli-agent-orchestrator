"""F754 (#611): the stale-terminal-id dispatch guard.

Incident being mechanized (2026-09-04 ~22:50Z): after a compaction the summary
carried the pre-relaunch seat id ``5561a7d1`` while the live seat was
``34a7b2c1``. Seven lanes were dispatched telling workers to call back the dead
id; the codex lanes would have failed their callback silently.

The rule set under test, in full:
  own id -> ok, live foreign id -> ok, anything else -> refuse,
  no id at all -> never refuse, live set unavailable -> never refuse.
"""

import asyncio
import os
import re
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.utils import terminal_id_scan as tis

OWN = "34a7b2c1"
LIVE_FOREIGN = "ab12cd34"
DEAD = "5561a7d1"
LIVE = {OWN, LIVE_FOREIGN}


@pytest.fixture(autouse=True)
def _clear_roster_cache():
    server._live_terminals_cache = (0.0, None)
    yield
    server._live_terminals_cache = (0.0, None)


# ---------------------------------------------------------------------------
# The scanner rule set (AC: own id, live foreign id, dead id, brief file, none)
# ---------------------------------------------------------------------------


class TestRuleSet:
    def test_own_id_passes(self):
        """MUTANT GUARD: flipping ``own`` to a refusal must fail here."""
        msg = f"When you finish, report to terminal {OWN}."
        assert tis.guard(msg, OWN, LIVE, reader=lambda p: None) is None

    def test_own_id_passes_even_when_the_roster_omits_it(self):
        """MUTANT GUARD: this is what the own-id branch is FOR.

        With the seat's own row in the roster, dropping the own-id check
        changes nothing. It matters exactly when the roster does not list the
        caller — a partial read, or the seat's row briefly missing — and the
        supervisor must still be able to dispatch a brief naming itself.
        """
        msg = f"When you finish, report to terminal {OWN}."
        assert tis.guard(msg, OWN, {LIVE_FOREIGN}, reader=lambda p: None) is None

    def test_live_foreign_id_passes(self):
        msg = f"Coordinate with terminal {LIVE_FOREIGN} before you commit."
        assert tis.guard(msg, OWN, LIVE, reader=lambda p: None) is None

    def test_dead_id_is_refused(self):
        """MUTANT GUARD: a scanner that accepts a dead id must fail here."""
        msg = f"Send your report back to terminal {DEAD} when done."
        refusal = tis.guard(msg, OWN, LIVE, reader=lambda p: None, action="assign")
        assert refusal is not None
        assert refusal.startswith(tis.ERROR_CODE)
        # The caller's REAL id has to be in the text — the fix is one edit away.
        assert OWN in refusal
        assert DEAD in refusal
        assert LIVE_FOREIGN in refusal  # live roster is listed

    def test_no_id_at_all_is_never_refused(self):
        msg = "Build the thing, run the suite on a box, and report when green."
        assert tis.guard(msg, OWN, LIVE, reader=lambda p: None) is None

    def test_unavailable_roster_never_refuses(self):
        msg = f"Report to terminal {DEAD}."
        assert tis.guard(msg, OWN, None, reader=lambda p: None) is None

    def test_git_sha_is_not_a_citation(self):
        msg = "Branch side/f754 at 9b93dd24, fork head 60555340ab12cd34ef, base c96d27ed."
        assert tis.find_citations(msg) == []
        assert tis.guard(msg, OWN, LIVE, reader=lambda p: None) is None

    def test_bare_hex_token_is_not_a_citation(self):
        assert tis.find_citations(f"the value {DEAD} appears here") == []

    @pytest.mark.parametrize(
        "text,rule",
        [
            (f"call back terminal {DEAD}", "terminal"),
            (f"to terminal {DEAD}", "terminal"),
            (f"CAO terminal {DEAD} is the seat", "terminal"),
            (f"terminal id: {DEAD}", "terminal"),
            (f"receiver_id={DEAD}", "id-kwarg"),
            (f'receiver_id="{DEAD}"', "id-kwarg"),
            (f"CAO_TERMINAL_ID={DEAD}", "id-kwarg"),
            (f"callback to {DEAD}", "callback"),
            # "terminal" is a stronger, earlier rule than "callback" — when both
            # match the same id on the same line the first rule keeps it.
            (f"report to the terminal {DEAD}", "terminal"),
            (f"report to {DEAD}", "callback"),
            (f"seat {DEAD}", "seat"),
            (f"GET /terminals/{DEAD}", "terminal"),
        ],
    )
    def test_citation_forms(self, text, rule):
        found = tis.find_citations(text)
        assert [c.terminal_id for c in found] == [DEAD], text
        assert found[0].rule == rule, text

    def test_citation_records_line_number(self):
        text = f"line one\nline two\ncall back to terminal {DEAD}\n"
        (citation,) = tis.find_citations(text)
        assert citation.line == 3
        assert citation.source == "message"


# ---------------------------------------------------------------------------
# Brief files (#611 part 2)
# ---------------------------------------------------------------------------


class TestBriefFiles:
    def test_finds_brief_paths(self):
        msg = "Read /data/cao-scratch/briefs/lane-f754.md first, then start."
        assert tis.find_brief_paths(msg) == ["/data/cao-scratch/briefs/lane-f754.md"]

    def test_dead_id_inside_a_brief_is_refused_naming_file_and_line(self):
        brief = "/data/cao-scratch/briefs/lane-f754.md"
        body = f"# Lane\n\nWhen done, report to terminal {DEAD}.\n"
        refusal = tis.guard(
            f"Read {brief} and follow it exactly.",
            OWN,
            LIVE,
            reader=lambda p: body if p == brief else None,
            action="assign",
        )
        assert refusal is not None
        assert f"{brief}:3 cites {DEAD}" in refusal

    def test_live_id_inside_a_brief_passes(self):
        brief = "/data/cao-scratch/briefs/lane-f754.md"
        body = f"Report to terminal {LIVE_FOREIGN}.\n"
        assert (
            tis.guard(f"Read {brief}.", OWN, LIVE, reader=lambda p: body if p == brief else None)
            is None
        )

    def test_missing_brief_is_skipped_not_refused(self):
        msg = "Read /data/cao-scratch/briefs/does-not-exist.md."
        assert tis.guard(msg, OWN, LIVE) is None

    def test_default_reader_refuses_paths_outside_the_brief_dir(self, tmp_path):
        outside = tmp_path / "notes.md"
        outside.write_text("anything")
        assert tis._default_reader(str(outside)) is None

    def test_default_reader_reads_a_real_brief(self, tmp_path, monkeypatch):
        """End-to-end through the real file reader, with the brief dir relocated."""
        brief_dir = tmp_path / "briefs"
        brief_dir.mkdir()
        brief = brief_dir / "lane-f754.md"
        brief.write_text(f"Report to terminal {DEAD}.\n")
        monkeypatch.setattr(tis, "BRIEF_DIR", str(brief_dir) + "/")
        monkeypatch.setattr(
            tis, "BRIEF_PATH_RE", re.compile(re.escape(str(brief_dir)) + r"/[A-Za-z0-9._+-]+\.md")
        )
        refusal = tis.guard(f"Read {brief} first.", OWN, LIVE)
        assert refusal is not None
        assert f"{brief}:1 cites {DEAD}" in refusal

    def test_oversized_brief_is_skipped(self, tmp_path, monkeypatch):
        brief_dir = tmp_path / "briefs"
        brief_dir.mkdir()
        brief = brief_dir / "big.md"
        brief.write_text("x" * 64)
        monkeypatch.setattr(tis, "BRIEF_DIR", str(brief_dir) + "/")
        monkeypatch.setattr(tis, "MAX_BRIEF_BYTES", 8)
        assert tis._default_reader(str(brief)) is None


# ---------------------------------------------------------------------------
# Live roster: one GET /terminals per call, cached 5 s
# ---------------------------------------------------------------------------


def _roster_response(rows, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = rows
    response.raise_for_status.return_value = None
    return response


class TestLiveRoster:
    def test_one_get_per_call_cached_for_five_seconds(self):
        rows = [{"id": OWN}, {"id": LIVE_FOREIGN}]
        with patch.object(server.cao_http, "get", return_value=_roster_response(rows)) as get:
            first = server._live_terminal_ids()
            second = server._live_terminal_ids()
        assert first == LIVE
        assert second == LIVE
        assert get.call_count == 1
        assert get.call_args.args[0] == "/terminals"

    def test_cache_expires(self):
        rows = [{"id": OWN}]
        with patch.object(server.cao_http, "get", return_value=_roster_response(rows)) as get:
            server._live_terminal_ids()
            fetched_at, cached = server._live_terminals_cache
            server._live_terminals_cache = (
                fetched_at - server._LIVE_TERMINALS_TTL_S - 1.0,
                cached,
            )
            server._live_terminal_ids()
        assert get.call_count == 2

    def test_force_refresh_bypasses_the_cache(self):
        rows = [{"id": OWN}]
        with patch.object(server.cao_http, "get", return_value=_roster_response(rows)) as get:
            server._live_terminal_ids()
            server._live_terminal_ids(force_refresh=True)
        assert get.call_count == 2

    def test_returns_none_when_the_route_does_not_exist(self):
        with patch.object(
            server.cao_http, "get", return_value=_roster_response(None, status_code=404)
        ):
            assert server._live_terminal_ids() is None

    def test_returns_none_when_the_server_is_unreachable(self):
        with patch.object(server.cao_http, "get", side_effect=requests.ConnectionError("refused")):
            assert server._live_terminal_ids() is None

    def test_ignores_malformed_rows(self):
        rows = [{"id": OWN}, {"id": "NOTHEX"}, "junk", {"nope": 1}]
        with patch.object(server.cao_http, "get", return_value=_roster_response(rows)):
            assert server._live_terminal_ids() == {OWN}


class TestOldServerProbeFallback:
    """A cao-server predating GET /terminals is probed one cited id at a time."""

    def test_probe_separates_live_from_dead(self):
        def fake_get(path, **kwargs):
            tid = path.rsplit("/", 1)[-1]
            return _roster_response({}, status_code=200 if tid in LIVE else 404)

        with patch.object(server.cao_http, "get", side_effect=fake_get):
            assert server._probe_live_ids({LIVE_FOREIGN, DEAD}) == {LIVE_FOREIGN}

    def test_probe_returns_none_when_unreachable(self):
        with patch.object(server.cao_http, "get", side_effect=requests.ConnectionError("refused")):
            assert server._probe_live_ids({DEAD}) is None

    def test_probe_returns_none_on_a_server_error(self):
        with patch.object(
            server.cao_http, "get", return_value=_roster_response({}, status_code=500)
        ):
            assert server._probe_live_ids({DEAD}) is None

    def test_guard_refuses_via_the_probe_when_the_roster_route_is_missing(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", OWN)
        with (
            patch.object(server, "_live_terminal_ids", return_value=None),
            patch.object(server, "_probe_live_ids", return_value=set()) as probe,
        ):
            refusal = server._dispatch_guard(f"report to terminal {DEAD}", "assign")
        assert refusal is not None and DEAD in refusal
        probe.assert_called_once_with({DEAD})

    def test_guard_allows_when_neither_roster_nor_probe_works(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", OWN)
        with (
            patch.object(server, "_live_terminal_ids", return_value=None),
            patch.object(server, "_probe_live_ids", return_value=None),
        ):
            assert server._dispatch_guard(f"report to terminal {DEAD}", "assign") is None


class TestDispatchGuardHelper:
    def test_no_citation_means_no_roster_fetch(self):
        with patch.object(server, "_live_terminal_ids") as roster:
            assert server._dispatch_guard("just do the work", "assign") is None
        roster.assert_not_called()

    def test_guard_never_raises(self):
        with patch.object(server, "_live_terminal_ids", side_effect=RuntimeError("boom")):
            assert server._dispatch_guard(f"terminal {DEAD}", "assign") is None

    def test_empty_message_passes(self):
        assert server._dispatch_guard(None, "assign") is None
        assert server._dispatch_guard("", "assign") is None


# ---------------------------------------------------------------------------
# Tool wiring: assign / handoff / send_message refuse before doing anything
# ---------------------------------------------------------------------------


class TestAssignWiring:
    def test_dead_id_refused_without_creating_a_terminal(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", OWN)
        with (
            patch.object(server, "_live_terminal_ids", return_value=LIVE),
            patch.object(server, "_create_terminal") as create,
        ):
            result = server._assign_impl("dev", f"Report to terminal {DEAD} when done.")
        assert result["success"] is False
        assert result["terminal_id"] is None
        assert result["message"].startswith(tis.ERROR_CODE)
        assert "No assign was sent" in result["message"]
        create.assert_not_called()

    def test_own_id_gets_past_the_guard(self, monkeypatch):
        """The guard must not be a blanket refusal: an own-id brief proceeds."""
        from cli_agent_orchestrator.utils import agent_profiles

        monkeypatch.setenv("CAO_TERMINAL_ID", OWN)
        sentinel = agent_profiles.AssignmentResolutionError("E-SENTINEL", "past-the-guard")

        def boom(profile, provider):
            raise sentinel

        monkeypatch.setattr(agent_profiles, "_position_exists", lambda name: False)
        monkeypatch.setattr(agent_profiles, "resolve_assignment_target", boom)
        with patch.object(server, "_live_terminal_ids", return_value=LIVE):
            result = server._assign_impl("dev", f"Report to terminal {OWN} when done.")
        assert result["success"] is False
        assert "past-the-guard" in result["message"]


class TestSendMessageWiring:
    def test_dead_id_refused_without_touching_the_inbox(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", OWN)
        with (
            patch.object(server, "_live_terminal_ids", return_value=LIVE),
            patch.object(server, "_send_to_inbox") as inbox,
        ):
            result = server._send_message_impl(LIVE_FOREIGN, f"ping terminal {DEAD}")
        assert result["success"] is False
        assert result["error"].startswith(tis.ERROR_CODE)
        assert "No send_message was sent" in result["error"]
        inbox.assert_not_called()

    def test_live_worker_named_in_the_body_passes(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", OWN)
        with (
            patch.object(server, "_live_terminal_ids", return_value=LIVE),
            patch.object(server, "_send_to_inbox", return_value={"success": True}) as inbox,
        ):
            result = server._send_message_impl(
                LIVE_FOREIGN, f"hand off to terminal {LIVE_FOREIGN} and report to {OWN}"
            )
        assert result["success"] is True
        inbox.assert_called_once()


class TestHandoffWiring:
    def test_dead_id_refused(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", OWN)
        with (
            patch.object(server, "_live_terminal_ids", return_value=LIVE),
            patch.object(server, "strict_supervisor_cwd") as cwd,
        ):
            result = asyncio.run(
                server._handoff_impl("reviewer", f"review and reply to terminal {DEAD}")
            )
        assert result.success is False
        assert result.terminal_id is None
        assert result.message.startswith(tis.ERROR_CODE)
        assert "No handoff was sent" in result.message
        cwd.assert_not_called()


# ---------------------------------------------------------------------------
# Drift: the root repo's hook twin must carry a byte-identical rule set
# ---------------------------------------------------------------------------

_BEGIN = "# --- BEGIN SHARED SCANNER (F754) ---"
_END = "# --- END SHARED SCANNER (F754) ---"


def _shared_block(path):
    text = path.read_text()
    return text[text.index(_BEGIN) : text.index(_END) + len(_END)]


def test_shared_scanner_block_is_identical_to_the_root_twin():
    """One rule set, two copies. Regenerate rather than hand-edit either."""
    from pathlib import Path

    fork_copy = Path(tis.__file__)
    override = os.environ.get("F754_ROOT_SCANNER")
    if override:
        root_copy = Path(override)
    else:
        # In a normal checkout the fork is nested inside the root repo:
        # <root>/cli-agent-orchestrator/src/cli_agent_orchestrator/utils/<this file>
        root_copy = fork_copy.parents[4] / ".claude" / "hooks" / "lib" / "terminal_id_scan.py"
    if not root_copy.is_file():
        pytest.skip(f"root-repo twin not present at {root_copy}")
    assert _shared_block(root_copy) == _shared_block(fork_copy)
