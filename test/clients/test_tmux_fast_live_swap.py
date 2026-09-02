"""Live-tmux parity for PaneLocator after ``swap-pane`` (gate r2 blocker 3).

Runs a throwaway tmux server on a private ``cao-sbx-*`` socket (never the
operator's default socket), builds a two-pane window whose panes run
distinguishable commands, warms the fast-path cache, swaps the panes, and
asserts the fast path reads the same first pane libtmux's ``window.panes[0]``
does. Also covers ``rotate-window``, which reorders indexes the same way.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from typing import Iterator, List

import pytest

pytestmark = pytest.mark.requires_tmux

_HAS_TMUX = shutil.which("tmux") is not None


@pytest.fixture
def sock(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    if not _HAS_TMUX:
        pytest.skip("tmux not on PATH")
    name = "cao-sbx-swp" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("CAO_TMUX_SOCKET", name)
    monkeypatch.delenv("CAO_TMUX_FAST_LOOKUP", raising=False)
    # Panes run finite sleeps so the private server dies on its own even if
    # teardown never runs.
    subprocess.run(
        [
            "tmux",
            "-L",
            name,
            "new-session",
            "-d",
            "-s",
            "S",
            "-n",
            "w",
            "-x",
            "80",
            "-y",
            "20",
            "sh -c 'exec sleep 120'",
        ],
        check=True,
    )
    subprocess.run(
        ["tmux", "-L", name, "split-window", "-t", "S:w", "sh -c 'exec sleep 121'"], check=True
    )
    yield name
    subprocess.run(["tmux", "-L", name, "kill-server"], check=False, capture_output=True)


def _tm(sock: str, *args: str) -> List[str]:
    out = subprocess.run(["tmux", "-L", sock, *args], check=True, capture_output=True, text=True)
    return out.stdout.split()


def _layout(sock: str) -> List[str]:
    return _tm(sock, "list-panes", "-t", "S:w", "-F", "#{pane_index}=#{pane_id}")


def _wait_layout(sock: str, expect_first_pane: str) -> None:
    for _ in range(50):
        if _layout(sock)[0].endswith(expect_first_pane):
            return
        time.sleep(0.05)
    raise AssertionError(f"layout never settled: {_layout(sock)}")


def _fast_first_pane_id(sock: str) -> str:
    from cli_agent_orchestrator.clients.tmux_fast import PaneLocator

    locator = PaneLocator()
    out = locator.run_pane_command("S", "w", "display-message", "-p", "#{pane_id}", pane="first")
    assert out is not None, "fast path fell back; expected a fast answer on a live private socket"
    return out[0].strip()


def test_fast_first_pane_follows_swap_pane(sock: str) -> None:
    from cli_agent_orchestrator.clients.tmux import TmuxClient
    from cli_agent_orchestrator.clients.tmux_fast import PaneLocator

    before = _layout(sock)
    assert len(before) == 2
    ids = [entry.split("=")[1] for entry in before]

    locator = PaneLocator()
    # Warm the cache on the pre-swap layout.
    warm = locator.run_pane_command("S", "w", "display-message", "-p", "#{pane_id}", pane="first")
    assert warm == [ids[0]]
    assert locator.resolve("S", "w").first_pane == ids[0]

    subprocess.run(["tmux", "-L", sock, "swap-pane", "-t", "S:w.0", "-s", "S:w.1"], check=True)
    _wait_layout(sock, ids[1])

    # libtmux's notion of the first pane (the legacy path) ...
    client = TmuxClient()
    session = client.server.sessions.get(session_name="S")
    window = session.windows.get(window_name="w")
    legacy_first = window.panes[0].pane_id
    assert legacy_first == ids[1]

    # ... and the fast path with a WARM (now stale) cache must agree in the same call.
    fast_first = locator.run_pane_command(
        "S", "w", "display-message", "-p", "#{pane_id}", pane="first"
    )
    assert fast_first == [legacy_first]
    assert locator.resolve("S", "w").first_pane == ids[1]

    # End-to-end through TmuxClient: fast on, fast off, identical.
    os.environ["CAO_TMUX_FAST_LOOKUP"] = "1"
    fast_size = client.get_pane_size("S", "w")
    os.environ["CAO_TMUX_FAST_LOOKUP"] = "0"
    legacy_size = client.get_pane_size("S", "w")
    assert fast_size == legacy_size
    os.environ.pop("CAO_TMUX_FAST_LOOKUP", None)


def test_fast_first_pane_follows_rotate_window(sock: str) -> None:
    before = _layout(sock)
    ids = [entry.split("=")[1] for entry in before]
    assert _fast_first_pane_id(sock) == ids[0]

    from cli_agent_orchestrator.clients.tmux_fast import PaneLocator

    locator = PaneLocator()
    assert locator.run_pane_command(
        "S", "w", "display-message", "-p", "#{pane_id}", pane="first"
    ) == [ids[0]]
    subprocess.run(["tmux", "-L", sock, "rotate-window", "-t", "S:w"], check=True)
    _wait_layout(sock, ids[1])
    assert locator.run_pane_command(
        "S", "w", "display-message", "-p", "#{pane_id}", pane="first"
    ) == [ids[1]]
