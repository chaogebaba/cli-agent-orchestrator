"""F497 AC2 — live resolver-support probe for the install guard.

Migration step 1 of F497 (position/provider decoupling) lands the profile
resolver seam and refuses to let a composition-bearing profile
(``extends:``/``position:``) be installed against a cao-server that cannot yet
compose it. The stale component is the RUNNING server, not the CLI: within one
``./install.sh``, ``uv tool install --force`` runs before the profile loop, so
the CLI executing ``cao install`` is always the new build and a CLI self-check
proves nothing. Composition happens server-side at spawn, so the guard queries
the LIVE server's ``/health`` capability advertisement and fails CLOSED:

  * server reports ``capabilities.profile_resolver == True``  -> supported
  * server reports it false / missing / malformed               -> NOT supported
  * server unreachable, cold, or otherwise unanswerable         -> NOT supported

An unanswerable query counting as "no support" is deliberate (mirrors
``/orchestrator`` fail-closed behaviour): a composition-bearing profile written
to a store that a resolver-less server later spawns from would boot a worker
with an empty persona and no error. The ``CAO_SKIP_RESOLVER_PROBE=1`` escape
covers environments with no server BY DESIGN (offload boxes have no systemd),
following install.sh's existing escape-hatch pattern.
"""

from __future__ import annotations

import logging
import os

from cli_agent_orchestrator.utils.http import cao_http

logger = logging.getLogger(__name__)

# Env escape hatch: when set to a truthy value, the probe is skipped and the
# guard treats resolver support as present. For environments with no server by
# design (cold bring-up, offload boxes). Mirrors install.sh's CAO_SKIP_* knobs.
_SKIP_ENV = "CAO_SKIP_RESOLVER_PROBE"

# Health probe must be quick; a slow/wedged server should read as "no support"
# and refuse, not hang the install. (connect, read) seconds.
_PROBE_TIMEOUT = (2.0, 3.0)


def resolver_probe_skipped() -> bool:
    """True when ``CAO_SKIP_RESOLVER_PROBE`` opts out of the live probe."""
    return os.environ.get(_SKIP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def server_supports_resolver() -> bool:
    """Return True only when the RUNNING cao-server advertises resolver support.

    Fails CLOSED on every uncertain path: an unreachable server, a non-200
    response, a body that is not the expected shape, or a missing/false
    ``capabilities.profile_resolver`` flag all return False. Never raises — the
    caller turns a False into a refusal, and a probe error must not itself crash
    the install.
    """
    if resolver_probe_skipped():
        # Caller checks resolver_probe_skipped() first for messaging, but guard
        # here too so a direct caller of this function is also fail-open only
        # under the explicit escape.
        return True
    try:
        resp = cao_http.get("/health", timeout=_PROBE_TIMEOUT)
        if resp.status_code != 200:
            logger.debug("resolver probe: /health returned %s", resp.status_code)
            return False
        data = resp.json()
    except Exception as exc:  # unreachable, timeout, bad JSON, config error
        logger.debug("resolver probe: /health unreachable/unusable: %s", exc)
        return False
    if not isinstance(data, dict):
        return False
    capabilities = data.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    return capabilities.get("profile_resolver") is True
