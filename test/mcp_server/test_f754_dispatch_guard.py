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
from cli_agent_orchestrator.utils import routing_guard as rg
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

SHARED_MARKERS = {
    "terminal_id_scan.py": (
        "# --- BEGIN SHARED SCANNER (F754) ---",
        "# --- END SHARED SCANNER (F754) ---",
    ),
    "routing_guard.py": (
        "# --- BEGIN SHARED ROUTING GUARD (F754) ---",
        "# --- END SHARED ROUTING GUARD (F754) ---",
    ),
}


def _shared_block(path, begin, end):
    text = path.read_text()
    return text[text.index(begin) : text.index(end) + len(end)]


@pytest.mark.parametrize("module_file", sorted(SHARED_MARKERS))
def test_shared_blocks_are_identical(module_file):
    """One rule set per module, two copies. Regenerate; never hand-edit either."""
    from pathlib import Path

    fork_copy = Path(tis.__file__).parent / module_file
    root_lib = os.environ.get("F754_ROOT_LIB")
    if root_lib:
        root_copy = Path(root_lib) / module_file
    else:
        # In a normal checkout the fork is nested inside the root repo:
        # <root>/cli-agent-orchestrator/src/cli_agent_orchestrator/utils/<this file>
        root_copy = fork_copy.parents[4] / ".claude" / "hooks" / "lib" / module_file
    if not root_copy.is_file():
        pytest.skip(f"root-repo twin not present at {root_copy}")
    begin, end = SHARED_MARKERS[module_file]
    assert _shared_block(root_copy, begin, end) == _shared_block(fork_copy, begin, end)


# ---------------------------------------------------------------------------
# Routing violation (#611 scope add)
# ---------------------------------------------------------------------------
# routing.toml is the sole authority on which lane fills which position. A
# legacy provider-named profile implies its own provider and can contradict it —
# the M34-class mistake in routing.toml's own status log (2026-09-04: three
# kiro_dev lanes plus kiro_oracle dispatched against `dev = in_harness`).

DEV_IN_HARNESS = [
    {"position": "dev", "provider": None, "kind": "in_harness", "model": "opus"},
    {"position": "empirical_reviewer", "provider": "codex", "kind": "cao", "model": None},
    {"position": "oracle", "provider": "cline_cli", "kind": "cao", "model": None},
    {"position": "tester", "provider": "grok_cli", "kind": "cao", "model": None},
    {"position": "general", "provider": "codex", "kind": "cao", "model": None},
    {"position": "general", "provider": "grok_cli", "kind": "cao", "model": None},
]


class TestRoutingRule:
    """The rule itself, against a fixed binding table."""

    @pytest.mark.parametrize(
        "profile,provider,position",
        [
            ("codex_dev", "codex", "dev"),  # position is in-harness: no CAO cell at all
            ("kiro_dev", "kiro_cli", "dev"),
            ("cline_dev", "cline_cli", "dev"),
            ("kiro_oracle", "kiro_cli", "oracle"),  # oracle is bound to cline_cli
            ("grok_oracle", "grok_cli", "oracle"),
            ("kiro_general", "kiro_cli", "general"),  # general has no kiro cell
        ],
    )
    def test_contradicting_profiles_are_refused(self, profile, provider, position):
        refusal = rg.routing_violation(profile, provider, position, DEV_IN_HARNESS, "assign")
        assert refusal is not None
        assert refusal.startswith(rg.ROUTING_ERROR_CODE)
        assert f"profile {profile} implies provider {provider} for position {position}" in refusal
        assert f"routing.toml binds {position} ->" in refusal

    def test_in_harness_binding_is_named_in_the_error(self):
        refusal = rg.routing_violation("codex_dev", "codex", "dev", DEV_IN_HARNESS, "assign")
        assert "routing.toml binds dev -> in_harness (model=opus)" in refusal
        assert "No assign was sent" in refusal

    @pytest.mark.parametrize(
        "profile,provider,position",
        [
            ("codex_empirical_reviewer", "codex", "empirical_reviewer"),  # exact cell
            ("grok_tester", "grok_cli", "tester"),
            ("codex_general", "codex", "general"),  # one of several general cells
            ("grok_general", "grok_cli", "general"),
        ],
    )
    def test_matching_profiles_pass(self, profile, provider, position):
        assert rg.routing_violation(profile, provider, position, DEV_IN_HARNESS, "assign") is None

    def test_unbound_position_is_never_refused(self):
        """MUTANT GUARD: refusing an unbound position would block legitimate work."""
        assert rg.routing_violation("codex_base", "codex", "base", DEV_IN_HARNESS) is None

    def test_unmappable_profile_is_never_refused(self):
        assert rg.routing_violation("grok_reviewer", "grok_cli", None, DEV_IN_HARNESS) is None

    def test_profile_without_a_provider_is_never_refused(self):
        assert rg.routing_violation("developer-opus", None, "dev", DEV_IN_HARNESS) is None

    def test_empty_binding_table_is_never_refused(self):
        assert rg.routing_violation("codex_dev", "codex", "dev", []) is None


class TestProfileToPosition:
    def test_extends_frontmatter_wins(self):
        meta = rg.parse_profile_frontmatter(
            "---\nname: kiro_oracle\nprovider: kiro_cli\nrole: developer\nextends: oracle\n---\n",
            "kiro_oracle",
        )
        assert meta == rg.ProfileMeta("kiro_oracle", "kiro_cli", "oracle")
        assert rg.position_for_profile(meta) == "oracle"

    def test_explicit_table_covers_profiles_without_extends(self):
        """codex_dev — the headline case — carries no `extends`."""
        meta = rg.parse_profile_frontmatter(
            "---\nname: codex_dev\nprovider: codex\nrole: developer\n---\n", "codex_dev"
        )
        assert meta.extends is None
        assert rg.position_for_profile(meta) == "dev"

    def test_role_is_not_used_as_a_position(self):
        """Every worker profile says `role: developer`, gates and oracle included."""
        meta = rg.parse_profile_frontmatter(
            "---\nname: grok_reviewer\nprovider: grok_cli\nrole: developer\n---\n", "grok_reviewer"
        )
        assert rg.position_for_profile(meta) is None

    def test_extends_gated_on_known_positions(self):
        meta = rg.parse_profile_frontmatter(
            "---\nname: x\nprovider: codex\nextends: some_profile\n---\n", "x"
        )
        assert rg.position_for_profile(meta, ["dev", "oracle"]) is None
        assert rg.position_for_profile(meta, None) == "some_profile"

    def test_nested_frontmatter_keys_are_not_read_as_top_level(self):
        text = (
            "---\n"
            "name: codex_dev\n"
            "provider: codex\n"
            "mcpServers:\n"
            "  cao-mcp-server:\n"
            "    provider: NOT_THIS\n"
            "    command: cao-mcp-server\n"
            "---\n"
        )
        assert rg.parse_profile_frontmatter(text, "codex_dev").provider == "codex"

    def test_tab_indented_nested_keys_are_also_ignored(self):
        text = "---\nname: codex_dev\nprovider: codex\nmcpServers:\n\tprovider: NOT_THIS\n---\n"
        assert rg.parse_profile_frontmatter(text, "codex_dev").provider == "codex"

    def test_a_nested_key_never_supplies_a_missing_top_level_one(self):
        """The reader must not invent a provider out of a nested block."""
        text = "---\nname: mystery\nmcpServers:\n  inner:\n    provider: NOT_THIS\n---\n"
        assert rg.parse_profile_frontmatter(text, "mystery").provider is None

    def test_no_frontmatter_yields_nothing(self):
        meta = rg.parse_profile_frontmatter("# just a body\n", "mystery")
        assert meta == rg.ProfileMeta("mystery", None, None)

    def test_every_table_entry_names_a_real_repo_profile(self):
        """The explicit table must not drift from profiles/ (F754 scope add)."""
        from pathlib import Path

        override = os.environ.get("F754_ROOT_PROFILES")
        profiles_dir = Path(override) if override else Path(tis.__file__).parents[4] / "profiles"
        if not profiles_dir.is_dir():
            pytest.skip(f"root-repo profiles/ not present at {profiles_dir}")
        for profile in rg.PROFILE_POSITION_TABLE:
            assert (profiles_dir / f"{profile}.md").is_file(), profile
        for profile in rg.UNMAPPED_BY_DESIGN:
            assert (profiles_dir / f"{profile}.md").is_file(), profile


class TestRoutingBindingsLoader:
    def test_reads_and_flattens_the_store(self, tmp_path, monkeypatch):
        toml = tmp_path / "routing.toml"
        toml.write_text(
            '[[binding]]\nposition = "dev"\nkind = "in_harness"\nmodel = "opus"\n\n'
            '[[binding]]\nposition = "empirical_reviewer"\nprovider = "codex"\nkind = "cao"\n'
        )
        monkeypatch.setenv("CAO_ROUTING_TOML", str(toml))
        server._routing_bindings_cache = (0.0, None)
        rows = server._routing_bindings(force_refresh=True)
        assert {"position": "dev", "provider": None, "kind": "in_harness", "model": "opus"} in rows
        assert {
            "position": "empirical_reviewer",
            "provider": "codex",
            "kind": "cao",
            "model": None,
        } in rows

    def test_missing_store_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CAO_ROUTING_TOML", str(tmp_path / "nope.toml"))
        server._routing_bindings_cache = (0.0, None)
        assert server._routing_bindings(force_refresh=True) is None

    def test_is_cached(self, tmp_path, monkeypatch):
        toml = tmp_path / "routing.toml"
        toml.write_text('[[binding]]\nposition = "dev"\nkind = "in_harness"\n')
        monkeypatch.setenv("CAO_ROUTING_TOML", str(toml))
        server._routing_bindings_cache = (0.0, None)
        first = server._routing_bindings(force_refresh=True)
        toml.unlink()
        assert server._routing_bindings() == first


@pytest.fixture
def routed_store(tmp_path, monkeypatch):
    """A CAO home whose store holds codex_dev + a dev=in_harness routing.toml."""
    store = tmp_path / "agent-store"
    (store / "positions").mkdir(parents=True)
    (store / "codex_dev.md").write_text(
        "---\nname: codex_dev\nprovider: codex\nrole: developer\n---\n\n# CODEX DEV\n"
    )
    (store / "codex_empirical_reviewer.md").write_text(
        "---\nname: codex_empirical_reviewer\nprovider: codex\nextends: empirical_reviewer\n---\n"
    )
    # `secretary` and `grok_doc_keeper` are the two names in this repo that are
    # BOTH a position and a same-named profile (verified against profiles/).
    (store / "secretary.md").write_text(
        "---\nname: secretary\nprovider: cline_cli\nextends: secretary\n---\n"
    )
    for position in ("dev", "empirical_reviewer", "general", "secretary"):
        (store / "positions" / f"{position}.md").write_text(f"# {position}\n")
    (store / "routing.toml").write_text(
        '[[binding]]\nposition = "dev"\nkind = "in_harness"\nmodel = "opus"\n\n'
        '[[binding]]\nposition = "empirical_reviewer"\nprovider = "codex"\nkind = "cao"\n\n'
        # secretary bound AWAY from the same-named profile's provider
        '[[binding]]\nposition = "secretary"\nprovider = "codex"\nkind = "cao"\n'
    )
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("CAO_ROUTING_TOML", str(store / "routing.toml"))
    server._routing_bindings_cache = (0.0, None)
    yield store
    server._routing_bindings_cache = (0.0, None)


class TestRoutingGuardHelper:
    def test_legacy_profile_against_in_harness_is_refused(self, routed_store):
        refusal = server._routing_guard("codex_dev", "assign")
        assert refusal is not None
        assert refusal.startswith(rg.ROUTING_ERROR_CODE)
        assert "routing.toml binds dev -> in_harness (model=opus)" in refusal

    def test_matching_cell_passes(self, routed_store):
        assert server._routing_guard("codex_empirical_reviewer", "assign") is None

    def test_position_name_is_never_checked(self, routed_store):
        """A bare position takes the routed path — its provider comes FROM routing."""
        assert server._routing_guard("dev", "assign") is None

    def test_a_name_that_is_both_a_position_and_a_profile_takes_the_routed_path(self, routed_store):
        """MUTANT GUARD: the position check must come FIRST, not fall out by luck.

        `secretary` and `grok_doc_keeper` are both a position AND a same-named
        profile. Here the store binds position `secretary` to codex while the
        profile `secretary.md` declares cline_cli. Assigning the POSITION is the
        correct routed usage and must pass; reading the same-named profile
        instead would refuse it — a false refusal of the one dispatch shape the
        routing store is asking for.
        """
        assert (routed_store / "secretary.md").is_file()
        assert (routed_store / "positions" / "secretary.md").is_file()
        assert server._routing_guard("secretary", "assign") is None

    def test_unknown_profile_is_never_checked(self, routed_store):
        assert server._routing_guard("not_a_profile", "assign") is None

    def test_none_profile_passes(self, routed_store):
        assert server._routing_guard(None, "assign") is None

    def test_guard_never_raises(self, routed_store):
        with patch.object(server, "_routing_bindings", side_effect=RuntimeError("boom")):
            assert server._routing_guard("codex_dev", "assign") is None


class TestRoutingWiring:
    def test_assign_of_codex_dev_against_dev_in_harness_is_refused(self, routed_store):
        """MUTANT GUARD: the scope add's named case. Guard removed -> this fails."""
        with patch.object(server, "_create_terminal") as create:
            result = server._assign_impl("codex_dev", "implement the thing")
        assert result["success"] is False
        assert result["terminal_id"] is None
        assert result["message"].startswith(rg.ROUTING_ERROR_CODE)
        assert "No assign was sent" in result["message"]
        create.assert_not_called()

    def test_handoff_of_kiro_dev_is_refused(self, routed_store, monkeypatch):
        (routed_store / "kiro_dev.md").write_text(
            "---\nname: kiro_dev\nprovider: kiro_cli\nrole: developer\n---\n"
        )
        with patch.object(server, "strict_supervisor_cwd") as cwd:
            result = asyncio.run(server._handoff_impl("kiro_dev", "review this"))
        assert result.success is False
        assert result.message.startswith(rg.ROUTING_ERROR_CODE)
        assert "No handoff was sent" in result.message
        cwd.assert_not_called()

    def test_a_routed_position_still_gets_past_the_guard(self, routed_store, monkeypatch):
        from cli_agent_orchestrator.utils import agent_profiles

        sentinel = agent_profiles.AssignmentResolutionError("E-SENTINEL", "past-the-guard")

        def boom(profile, provider):
            raise sentinel

        monkeypatch.setattr(agent_profiles, "resolve_assignment_target", boom)
        result = server._assign_impl("dev", "implement the thing")
        assert "past-the-guard" in result["message"]
