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
