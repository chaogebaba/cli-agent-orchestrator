"""F702 (#557) D2: the sequential fleet fetcher loop with bounded backoff.

One long-lived loop: fetch, post, sleep. Because nothing runs concurrently
with a fetch, a fetch is never cancelled — a 5 s timeout completes and is
recorded as ``last_error`` (staleness, not an error row — #441), and the loop
continues. The blocking ``urlopen`` runs on a thread so Textual's event loop
is never frozen.

``run_fetch_loop`` is a plain async function so it is testable without
Textual; J2 wraps it in ``@work(exit_on_error=False)``.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from typing import Any, Awaitable, Callable

from cli_agent_orchestrator.tui.fleet_state import FleetState

__all__ = ["FETCH_INTERVAL", "MAX_BACKOFF", "fetch_json", "run_fetch_loop"]

#: Normal poll interval, seconds. The backoff ladder starts here (BL2).
FETCH_INTERVAL = 2.0
#: Backoff cap, seconds: 2 -> 4 -> 8 -> 10 (B11).
MAX_BACKOFF = 10.0


def fetch_json(url: str, timeout: float = 5.0) -> Any:
    """Blocking GET returning decoded JSON (mirrors ``fleet-tui.py:88-91``).

    Called through ``asyncio.to_thread`` so the 5 s timeout never blocks the
    event loop. Raises on transport failure or undecodable body; the caller's
    handler records it.
    """
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


async def run_fetch_loop(
    url: str,
    post: Callable[[FleetState], Any],
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    fetch: Callable[..., Any] = fetch_json,
    now: Callable[[], float] = time.time,
    poll_interval: float = FETCH_INTERVAL,
) -> None:
    """Poll ``url`` forever, posting a `FleetState` after every iteration.

    ``state`` and ``interval`` are bound before the loop (BLK-B) so the
    exception branch is total on iteration 1. ``post`` is called outside the
    ``try``/``else`` split, so a failed fetch still yields a snapshot.

    ``poll_interval`` is the healthy cadence and the first rung of the backoff
    ladder — F702 parity restores the script's ``--interval`` flag
    (``fleet-tui.py:563``). The cap is never below the requested cadence, so a
    ``--interval 30`` run backs off to 30 s rather than tightening to 10 s.
    """
    state = FleetState.empty()
    interval = poll_interval
    ceiling = max(MAX_BACKOFF, poll_interval)
    consecutive_failures = 0
    while True:
        try:
            raw = await asyncio.to_thread(fetch, url, 5.0)
        except Exception as exc:  # timeout, connection refused, bad JSON …
            state = state.with_failure(str(exc), now=now())
            # Ladder 2 -> 4 -> 8 -> 10 (AC1): the first failure waits the
            # normal interval, each further one doubles up to the cap.
            interval = min(interval * 2, ceiling) if consecutive_failures else poll_interval
            consecutive_failures += 1
        else:
            state = FleetState.from_dict(raw, fetched_at=now())
            interval = poll_interval
            consecutive_failures = 0
        post(state)
        await sleep(interval)
