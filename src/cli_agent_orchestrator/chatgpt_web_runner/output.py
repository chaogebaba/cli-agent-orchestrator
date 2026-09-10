"""Structured output envelope (D7, r1 §5).

The runner returns a :class:`RunnerOutcome` on EVERY path — success or typed
failure. AC-7 and AC-8 assert against this fixed shape, not against log text.
Exit codes (when the runner is invoked as a subprocess) COMPLEMENT the envelope
and never replace it.

The envelope carries only allowlisted, non-secret fields (D3 "only allowlisted
IDs, model/effort, HTTP status and completion metadata may be retained"):
error code, hint, conversation URL, a submitted flag (true/false/unknown), the
delivery state, the partial-source field, elapsed time, observed model and
effort, and the bundle digest. It NEVER carries a cookie/bearer/sentinel value,
raw headers, or a HAR trace (AC-11/AC-11b).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerErrorCode,
)


class Submitted(str, Enum):
    """Tri-state submitted flag (D7). ``unknown`` is a first-class value: an
    ``ack-unknown`` delivery leaves submission genuinely undetermined and must
    never be coerced to true or false."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class PartialSource(str, Enum):
    """Where a partial (non-accepted) answer body came from, when any (D6).

    ``conversation-get`` — a partial read from the authoritative GET.
    ``dom``              — DOM salvage only; diagnostic, never an accepted read.
    """

    CONVERSATION_GET = "conversation-get"
    DOM = "dom"


@dataclass(frozen=True)
class RunnerOutcome:
    """The runner's single return shape (D7).

    ``ok`` is True only for an accepted, validated, published findings run. On
    any typed failure ``ok`` is False, ``error_code`` is set, and ``report_path``
    is None. ``answer`` is populated ONLY on an accepted run (never a partial —
    a partial rides ``partial_source`` with ``answer=None``).
    """

    ok: bool
    # --- failure classification (None on success) ---------------------------
    error_code: Optional[RunnerErrorCode] = None
    hint: str = ""
    delivery_state: DeliveryState = DeliveryState.NOTHING_SENT
    submitted: Submitted = Submitted.UNKNOWN
    partial_source: Optional[PartialSource] = None
    # --- run facts (allowlisted, non-secret) --------------------------------
    conversation_url: Optional[str] = None
    elapsed_ms: int = 0
    model_slug: Optional[str] = None
    thinking_effort: Optional[str] = None
    bundle_sha256: Optional[str] = None
    # --- accepted run only --------------------------------------------------
    answer: Optional[str] = None
    report_path: Optional[str] = None
    report_body_sha256: Optional[str] = None
    # --- attachment identity tuple (D8), when an attachment was sent --------
    attachment_identity: Optional[dict[str, Any]] = field(default=None)

    def to_envelope(self) -> dict[str, Any]:
        """Serialize to a plain JSON-safe dict with enum values flattened.

        This is what the provider surfaces and what the subprocess prints on
        stdout. Guaranteed to contain no secret field by construction.
        """
        raw = asdict(self)
        out: dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, Enum):
                out[key] = value.value
            else:
                out[key] = value
        # error_code / delivery_state / submitted / partial_source may be enums
        # nested one level; asdict already recursed dataclasses but leaves the
        # top-level enum members as members -> normalize explicitly.
        if isinstance(self.error_code, Enum):
            out["error_code"] = self.error_code.value
        if isinstance(self.delivery_state, Enum):
            out["delivery_state"] = self.delivery_state.value
        if isinstance(self.submitted, Enum):
            out["submitted"] = self.submitted.value
        if isinstance(self.partial_source, Enum):
            out["partial_source"] = self.partial_source.value
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_envelope(), sort_keys=True)
