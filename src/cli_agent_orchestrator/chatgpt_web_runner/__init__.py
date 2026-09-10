"""F862 (#718) — deterministic ChatGPT-web findings runner.

This package is the DATA PLANE for the ``chatgpt_web`` provider (D2/D13): it
frames a dispatch, hashes its bytes, drives the user's real chatgpt.com Plus web
session through a headful cloakbrowser + Playwright, reads the answer back from
the same-origin conversation GET, validates it, and publishes a
``Status: FINDINGS-READY`` artifact. The provider (``providers/chatgpt_web.py``)
is lifecycle and status only; everything that touches the browser, the bytes or
the report lives here.

Design authority: ``orchestrator/blueprints/f862-chatgpt-web-findings-lane.md``
(DESIGN-GATE-YES r3). Ground truth for endpoints/locators/completion signals:
``/data/cao-scratch/chatgpt-web/findings/chatgpt-api-behavior.md`` and
``upload-probe.md``.

Hard invariants enforced across the package:
- NEVER the OpenAI API (no ``api.openai.com``, no API key) — the DOM composer is
  the only send path; the conversation GET is the only structured read (D3).
- NEVER reimplement the sentinel / proof-of-work / Turnstile machinery — the real
  page computes those (D3).
- NEVER return, log or persist a cookie, bearer or sentinel VALUE (D3/AC-11): the
  bearer stays inside in-page ``fetch`` execution.
- Completion is decided by the conversation GET, never by DOM heuristics (D6).
- The artifact is FINDINGS, never a gate ruling (D10).

Modules are import-safe WITHOUT Playwright/cloakbrowser installed: the browser
libraries are imported lazily inside the runtime/transport modules so the pure
logic (errors, output, publication, submit-id framing, poll-gate evaluation,
upload identity) is unit-testable offline with recorded fixtures.
"""

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)
from cli_agent_orchestrator.chatgpt_web_runner.output import RunnerOutcome

__all__ = [
    "DeliveryState",
    "RunnerError",
    "RunnerErrorCode",
    "RunnerOutcome",
]
