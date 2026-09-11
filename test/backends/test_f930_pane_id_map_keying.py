"""F930 (#782): the durable pane_id map must be keyed by CAO's identifiers, not herdr's.

``HerdrBackend._refresh_pane_id_map`` built the map from each snapshot pane's
``terminal_id`` field, and ``get_pane_id`` looked it up by CAO's terminal uuid.
Those are two different identifier spaces: a snapshot pane's ``terminal_id`` is
HERDR's own terminal handle, so the map could not be hit even once.

Measured on a live herdr 0.9.0 server holding panes CAO itself created
(grok-box-006, ``herdr --session cao api snapshot``):

    pane   {"pane_id": "w3:p1", "tab_id": "w3:t1", "terminal_id": "term_65b3308e08aa73", ...}
    tab    {"tab_id": "w3:t1", "workspace_id": "w3", "label": "codex_general-919751d7"}
    wspace {"workspace_id": "w3", "label": "cao-hfxB"}

while the CAO terminal those panes belong to has id ``919751d7`` and window name
``codex_general-919751d7``. So every ``get_pane_id`` call missed the map, paid a
full ``api snapshot`` subprocess, missed again, and fell through to the legacy
label walk — the "durable map" made resolution strictly slower than no map.

CAO identity IS in the snapshot, one level up from the pane: ``create_window``
labels the tab with the CAO window name and the workspace with the CAO session
name. Joining panes -> tabs -> workspaces recovers the (session, window) pair
callers actually ask with.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

# Verbatim shape of a live `herdr api snapshot` (herdr 0.9.0, protocol 22),
# trimmed to the fields the map join uses. Two CAO sessions on one herdr server,
# and a window name deliberately REPEATED across them.
SNAPSHOT = {
    "id": "cli:api:snapshot",
    "result": {
        "workspaces": [
            {"workspace_id": "w3", "label": "cao-hfxB"},
            {"workspace_id": "w5", "label": "cao-hfxD"},
        ],
        "tabs": [
            {"tab_id": "w3:t1", "workspace_id": "w3", "label": "codex_general-919751d7"},
            {"tab_id": "w5:t1", "workspace_id": "w5", "label": "codex_general-f44ba37e"},
            # same window name, different CAO session
            {"tab_id": "w5:t2", "workspace_id": "w5", "label": "codex_general-919751d7"},
        ],
        "panes": [
            {"pane_id": "w3:p1", "tab_id": "w3:t1", "terminal_id": "term_65b3308e08aa73"},
            {"pane_id": "w5:p1", "tab_id": "w5:t1", "terminal_id": "term_65b330bfe4b2e7"},
            {"pane_id": "w5:p2", "tab_id": "w5:t2", "terminal_id": "term_65b330dbc637f8"},
        ],
    },
}


def _backend(snapshot=SNAPSHOT):
    backend = HerdrBackend.__new__(HerdrBackend)
    backend._pane_cache = {}
    backend._pane_id_map = {}
    backend._pane_id_map_ts = 0.0
    backend._workspace_cache = {}
    calls: list[list[str]] = []

    def run(args, check=False):
        calls.append(list(args))
        if args[:2] == ["api", "snapshot"]:
            return MagicMock(returncode=0, stdout=json.dumps(snapshot), stderr="")
        return MagicMock(returncode=1, stdout="", stderr="unexpected")

    backend._run_herdr = MagicMock(side_effect=run)  # type: ignore[method-assign]
    backend._resolve_pane_id_from_window = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("label walk must not be reached on a map hit")
    )
    return backend, calls


class TestTheMapResolvesByCaoIdentity:
    def test_pane_registered_under_the_herdr_id_resolves_by_the_cao_id(self):
        """The defect, stated as the ticket states it."""
        backend, _ = _backend()
        pane = backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7")
        assert pane == "w3:p1"

    def test_a_second_lookup_is_served_from_the_map_without_another_snapshot(self):
        """A hit must not re-run `api snapshot` — that cost was the whole defect."""
        backend, calls = _backend()
        backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7")
        snapshots_after_first = sum(1 for c in calls if c[:2] == ["api", "snapshot"])
        backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7")
        snapshots_after_second = sum(1 for c in calls if c[:2] == ["api", "snapshot"])
        assert snapshots_after_first == 1
        assert snapshots_after_second == 1, "second lookup rebuilt the map — still missing"

    def test_the_map_never_keys_on_herdrs_own_terminal_id(self):
        """Guard the defect directly, not only the repaired behaviour."""
        backend, _ = _backend()
        backend._refresh_pane_id_map()
        flattened = {part for key in backend._pane_id_map for part in key}
        assert not any(part.startswith("term_") for part in flattened), backend._pane_id_map

    def test_same_window_name_in_two_sessions_does_not_collide(self):
        """A snapshot spans every workspace, so the session must be part of the key."""
        backend, _ = _backend()
        assert backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7") == "w3:p1"
        assert backend.get_pane_id("919751d7", "cao-hfxD", "codex_general-919751d7") == "w5:p2"

    def test_every_cao_pane_in_the_snapshot_is_mapped(self):
        backend, _ = _backend()
        backend._refresh_pane_id_map()
        assert backend._pane_id_map == {
            ("cao-hfxB", "codex_general-919751d7"): "w3:p1",
            ("cao-hfxD", "codex_general-f44ba37e"): "w5:p1",
            ("cao-hfxD", "codex_general-919751d7"): "w5:p2",
        }


class TestRefreshStaysDefensive:
    """The existing failure contract must survive the re-key."""

    @pytest.mark.parametrize(
        "snapshot",
        [
            {"result": {}},
            {"result": {"panes": [{"pane_id": "w1:p1"}]}},  # pane with no tab_id
            {"result": {"tabs": [{"tab_id": "w1:t1", "label": "w"}], "panes": []}},
            {"result": {"panes": [{"tab_id": "w1:t1"}], "tabs": [], "workspaces": []}},
        ],
    )
    def test_a_partial_snapshot_yields_an_empty_map_not_an_exception(self, snapshot):
        backend, _ = _backend(snapshot)
        backend._refresh_pane_id_map()
        assert backend._pane_id_map == {}

    def test_a_tab_whose_workspace_is_missing_is_skipped(self):
        """Without a session label the key would be half-formed — drop the row."""
        backend, _ = _backend(
            {
                "result": {
                    "workspaces": [],
                    "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "win"}],
                    "panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1"}],
                }
            }
        )
        backend._refresh_pane_id_map()
        assert backend._pane_id_map == {}

    def test_a_failed_refresh_leaves_the_map_and_its_timestamp_untouched(self):
        backend, _ = _backend()
        backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7")
        good, stamp = dict(backend._pane_id_map), backend._pane_id_map_ts
        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=1, stdout="", stderr="socket closed")
        )
        backend._refresh_pane_id_map()
        assert backend._pane_id_map == good
        assert backend._pane_id_map_ts == stamp

    def test_a_stale_map_is_not_trusted(self):
        """Past the TTL a hit must rebuild rather than answer from the old map."""
        from cli_agent_orchestrator.backends import herdr_backend as mod

        backend, calls = _backend()
        backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7")
        backend._pane_id_map_ts = time.time() - (mod._PANE_ID_MAP_TTL + 1)
        backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7")
        assert sum(1 for c in calls if c[:2] == ["api", "snapshot"]) == 2


class TestInvalidationBeatsTheMap:
    """B1: the map became authoritative, so invalidation must reach it.

    `herdr_inbox_service`'s reconcile invalidates a pane it has just PROVEN dead
    (pane id gone, tab label still live) and then re-resolves. Before F930 it
    poked `_pane_cache` directly and that worked only because the map in front
    of the cache never hit. With the map live, a poke that misses it would let
    `get_pane_id` hand back the very id the caller proved wrong — the reconcile
    would "re-map" the terminal onto its own stale pane, count it repaired, and
    leave the routing table naming a pane that may since belong to a different
    terminal.
    """

    def test_after_invalidation_the_map_does_not_answer(self):
        backend, calls = _backend()
        assert backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7") == "w3:p1"

        # The pane is now dead and herdr says so: the snapshot no longer lists
        # it, and the label walk is the only source of the new id.
        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                returncode=0,
                stdout=json.dumps({"result": {"workspaces": [], "tabs": [], "panes": []}}),
                stderr="",
            )
        )
        backend._resolve_pane_id_from_window = MagicMock(return_value="w3:p7")  # type: ignore[method-assign]

        backend.invalidate_pane("919751d7", "cao-hfxB", "codex_general-919751d7")
        assert backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7") == "w3:p7"

    def test_invalidation_without_the_labels_still_disarms_the_map(self):
        """`_remap_terminal_identity` knows only the terminal id."""
        backend, _ = _backend()
        backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7")
        assert backend._pane_id_map, "precondition: the map is populated"

        backend.invalidate_pane("919751d7")

        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=1, stdout="", stderr="socket closed")
        )
        backend._resolve_pane_id_from_window = MagicMock(return_value="w3:p9")  # type: ignore[method-assign]
        # Refresh fails, so it leaves map and stamp untouched — the zeroed stamp
        # is what stops the stale entry being served anyway.
        assert backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7") == "w3:p9"

    def test_invalidation_also_clears_the_create_time_cache(self):
        backend, _ = _backend()
        backend._pane_cache["919751d7"] = ("w3:p1", time.time())
        backend.invalidate_pane("919751d7")
        assert "919751d7" not in backend._pane_cache

    def test_a_dead_pane_is_never_re_mapped_onto_itself(self):
        """The reconcile's own shape, end to end.

        Reproduces the regression the blocker names: proven-dead pane, live tab
        label, and the assertion is that the newly resolved id DIFFERS from the
        one just invalidated.
        """
        backend, _ = _backend()
        stale = backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7")

        # herdr has renumbered/re-created: the snapshot now names a new pane for
        # the same tab, exactly as a live rebuild would see it.
        moved = {
            "result": {
                "workspaces": [{"workspace_id": "w3", "label": "cao-hfxB"}],
                "tabs": [
                    {"tab_id": "w3:t1", "workspace_id": "w3", "label": "codex_general-919751d7"}
                ],
                "panes": [{"pane_id": "w3:p42", "tab_id": "w3:t1"}],
            }
        }
        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=0, stdout=json.dumps(moved), stderr="")
        )

        backend.invalidate_pane("919751d7", "cao-hfxB", "codex_general-919751d7")
        fresh = backend.get_pane_id("919751d7", "cao-hfxB", "codex_general-919751d7")

        assert fresh == "w3:p42"
        assert fresh != stale, "re-mapping a terminal onto its own dead pane is the defect"


class TestTheMapIsOnlyConsultedWhenItsKeyExists:
    """N7: a caller with no labels cannot be answered from a label-keyed map."""

    def test_a_keyless_lookup_does_not_pay_a_snapshot(self):
        backend, calls = _backend()
        backend._pane_cache["919751d7"] = ("w1:p1", time.time())
        assert backend.get_pane_id("919751d7") == "w1:p1"
        assert not [
            c for c in calls if c[:2] == ["api", "snapshot"]
        ], "rebuilding a map whose key the caller does not hold can only miss"


class TestMultiPaneTabsResolveLikeTheFallback:
    """N6: the two resolution paths must not disagree by snapshot order."""

    def test_the_first_pane_of_a_tab_wins(self):
        backend, _ = _backend(
            {
                "result": {
                    "workspaces": [{"workspace_id": "w1", "label": "cao-x"}],
                    "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "win-0"}],
                    "panes": [
                        {"pane_id": "w1:p1", "tab_id": "w1:t1"},
                        {"pane_id": "w1:p2", "tab_id": "w1:t1"},
                    ],
                }
            }
        )
        backend._refresh_pane_id_map()
        assert backend._pane_id_map == {("cao-x", "win-0"): "w1:p1"}
