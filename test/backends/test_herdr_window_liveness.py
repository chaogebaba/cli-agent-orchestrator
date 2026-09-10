"""herdr must answer ``window_liveness`` itself, not inherit the fail-closed default.

Same family as F893 (#745) and F900 (#752): a port method herdr never
implemented, so it fell through to ``base.py``'s ``return "error"``. Callers read
``"error"`` as "cannot rule out that it is alive" — and
``terminal_service._acquire_resume_leases`` refuses the resume with
``owner_conflict`` when a prior owner of the resume uuid reads ``live`` OR
``error``. Under herdr that made EVERY resume of a hibernated terminal fail
``500 owner_conflict`` (F880 r3 live round on grok-box-006: the hibernate
succeeded, `hibernate_ok=True`, and the resume that should have followed it
returned 500).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

WS = {"workspaces": [{"label": "cao-live", "workspace_id": "w1"}]}
TABS = {"tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "window-0"}]}


def _backend(workspace_body, tab_body, *, ws_rc=0, tab_rc=0):
    backend = HerdrBackend.__new__(HerdrBackend)
    backend._workspace_cache = {}

    def run(args, check=False):
        if args[:2] == ["workspace", "list"]:
            return MagicMock(returncode=ws_rc, stdout=json.dumps(workspace_body), stderr="")
        if args[:2] == ["tab", "list"]:
            return MagicMock(returncode=tab_rc, stdout=json.dumps(tab_body), stderr="")
        raise AssertionError(f"unexpected herdr call: {args}")

    backend._run_herdr = MagicMock(side_effect=run)  # type: ignore[method-assign]
    return backend


class TestHerdrWindowLiveness:
    def test_it_is_actually_overridden(self):
        assert HerdrBackend.window_liveness is not TerminalBackend.window_liveness

    def test_present_tab_is_live(self):
        assert _backend(WS, TABS).window_liveness("cao-live", "window-0") == "live"

    def test_closed_tab_in_a_live_workspace_is_gone(self):
        """The hibernate case: siblings remain, this window's tab is closed."""
        tabs = {"tabs": [{"tab_id": "w1:t2", "workspace_id": "w1", "label": "other"}]}
        assert _backend(WS, tabs).window_liveness("cao-live", "window-0") == "gone"

    def test_missing_workspace_is_gone(self):
        assert _backend({"workspaces": []}, TABS).window_liveness("cao-live", "window-0") == "gone"

    def test_a_tab_in_another_workspace_does_not_count(self):
        tabs = {"tabs": [{"tab_id": "w9:t1", "workspace_id": "w9", "label": "window-0"}]}
        assert _backend(WS, tabs).window_liveness("cao-live", "window-0") == "gone"

    def test_workspace_list_failure_is_error_not_gone(self):
        """Fail-closed the other way: a broken herdr must never read as 'gone'."""
        assert _backend(WS, TABS, ws_rc=3).window_liveness("cao-live", "window-0") == "error"

    def test_tab_list_failure_is_error_not_gone(self):
        assert _backend(WS, TABS, tab_rc=3).window_liveness("cao-live", "window-0") == "error"

    def test_unparseable_output_is_error(self):
        backend = HerdrBackend.__new__(HerdrBackend)
        backend._workspace_cache = {}
        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=0, stdout="not json", stderr="")
        )
        assert backend.window_liveness("cao-live", "window-0") == "error"
