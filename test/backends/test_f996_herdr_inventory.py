"""F996: herdr pane caches must not outlive the native pane inventory."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend


def test_get_pane_id_returns_none_for_retired_cached_id() -> None:
    backend = HerdrBackend.__new__(HerdrBackend)
    backend._pane_cache = {"retired0001": ("w1:p2", time.time())}
    backend._pane_id_map = {("cao-f996", "retired-window"): "w1:p2"}
    backend._pane_id_map_ts = time.time()
    backend._workspace_cache = {}
    backend._run_herdr = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": {
                        "panes": [
                            {"pane_id": "w1:p1", "tab_id": "w1:t1"},
                            {"pane_id": "w1:p3", "tab_id": "w1:t3"},
                        ]
                    }
                }
            ),
            stderr="",
        )
    )

    assert backend.get_pane_id("retired0001", "cao-f996", "retired-window") is None


def test_get_pane_id_retired_id_stays_none_after_failed_refresh() -> None:
    """A failed refresh must not resurrect either cache's retired pane id."""
    backend = HerdrBackend.__new__(HerdrBackend)
    backend._pane_cache = {"retired0001": ("w1:p2", time.time())}
    backend._pane_id_map = {("cao-f996", "retired-window"): "w1:p2"}
    backend._pane_id_map_ts = time.time()
    backend._workspace_cache = {}
    pane_list_calls = 0

    def run_herdr(args: list[str], check: bool = True) -> MagicMock:
        if args == ["pane", "list"]:
            nonlocal pane_list_calls
            pane_list_calls += 1
            if pane_list_calls > 1:
                return MagicMock(returncode=1, stdout="", stderr="herdr unavailable")
            # The first read proves the cached id is retired.  A no-invalidate
            # mutant sees this error on the second call and incorrectly fails
            # open with the stale id.
            return MagicMock(
                returncode=0,
                stdout=json.dumps({"result": {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1"}]}}),
                stderr="",
            )
        if args == ["api", "snapshot"]:
            # The real refresh failure leaves the map and timestamp untouched.
            return MagicMock(returncode=1, stdout="", stderr="herdr unavailable")
        if args == ["workspace", "list"]:
            return MagicMock(returncode=1, stdout="", stderr="herdr unavailable")
        raise AssertionError(f"unexpected herdr call: {args!r}")

    backend._run_herdr = MagicMock(side_effect=run_herdr)  # type: ignore[method-assign]

    assert backend.get_pane_id("retired0001", "cao-f996", "retired-window") is None
    assert backend._pane_cache == {}
    assert backend._pane_id_map == {}
    assert backend._pane_id_map_ts == 0.0

    # A second lookup must remain a refusal even though the failed snapshot
    # refresh cannot establish a replacement pane.
    assert backend.get_pane_id("retired0001", "cao-f996", "retired-window") is None
    assert any(call.args[0] == ["api", "snapshot"] for call in backend._run_herdr.call_args_list)
