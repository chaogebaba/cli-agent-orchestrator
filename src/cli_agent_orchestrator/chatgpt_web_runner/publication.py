"""FINDINGS-READY report construction, validation and body digest (D10).

The artifact is ``Status: FINDINGS-READY``, NEVER a gate ruling (D10/AC-4). This
module:

* rejects a model answer that contains a gate token (``Ruling:``, ``Verdict:``,
  ``GATE-YES``, ``GATE-NO``) as an invalid findings artifact — the runtime
  backstop to the persona's ``never-emit-verdict`` clause (D10);
* validates that each finding cites a manifest-backed section or line and quotes
  anchored old text (path/range/quote existence checked mechanically, D10) — the
  full mechanical citation check is best-effort here and completed by the review
  pipeline; the runner enforces the schema shape and the no-verdict rule;
* computes the canonical body digest AFTER validation and appends ONE plain
  last-line trailer (``Report-SHA256: <hex>``). The digest is sha256 of the body
  with the trailer line elided — the same elide-and-hash contract as
  ``scripts/report-attest.sh`` (D1), but the findings artifact carries a PLAIN
  trailer and no bold gate header, because it does not travel the gate parser
  path (D10).

Local code may add the authoritative status header and the digest — nothing
else. It never rewrites the model's judgment or turns a failure into a pass
(D10).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)

#: Tokens that are the GATE parser's contract; their presence in a findings
#: artifact is a schema violation, not a negative verdict (D10/AC-4).
_GATE_TOKENS = ("GATE-YES", "GATE-NO")
_GATE_LINE_PREFIXES = ("ruling:", "verdict:")

#: The status line a findings artifact carries (D10). Never a ruling token.
FINDINGS_STATUS_LINE = "Status: FINDINGS-READY"

#: The plain last-line trailer key (NOT the bold gate header form).
TRAILER_KEY = "Report-SHA256:"

_TRAILER_RE = re.compile(r"^(?:\*\*)?Report-SHA256(?::\*\*|:)[ \t]", re.MULTILINE)


@dataclass(frozen=True)
class PublishedReport:
    body: str
    body_sha256: str


def assert_no_gate_tokens(text: str) -> None:
    """AC-4: reject a model answer bearing a gate ruling token.

    ``Ruling:`` / ``Verdict:`` are matched as line-leading keys (case-insensitive,
    leading markdown emphasis/whitespace tolerated); ``GATE-YES`` / ``GATE-NO``
    are matched anywhere. A hit is ``invalid_verdict`` — no accepted report, no
    gate outcome.
    """
    upper = text.upper()
    for token in _GATE_TOKENS:
        if token in upper:
            raise RunnerError(
                RunnerErrorCode.INVALID_VERDICT,
                f"findings artifact contains gate token {token!r}",
                delivery_state=DeliveryState.DELIVERED,
            )
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip(" \t#*->").lower()
        for prefix in _GATE_LINE_PREFIXES:
            if stripped.startswith(prefix):
                raise RunnerError(
                    RunnerErrorCode.INVALID_VERDICT,
                    f"findings artifact contains a {prefix!r} line",
                    delivery_state=DeliveryState.DELIVERED,
                )


def canonical_body_digest(body: str) -> str:
    """sha256 of ``body`` with any ``Report-SHA256:`` trailer line elided (D1).

    Mirrors ``scripts/report-attest.sh`` ``canonical_report_digest`` exactly:
    drop every line matching the trailer key, then hash the remainder. Eliding
    the trailer is what makes the digest stable across insertion (D14).
    """
    kept = [line for line in body.splitlines(keepends=True) if not _TRAILER_RE.match(line)]
    return hashlib.sha256("".join(kept).encode("utf-8")).hexdigest()


def build_report(
    *,
    body_markdown: str,
    artifact_path: str,
    artifact_sha256: str,
    bundle_sha256: str,
    model_slug: str,
    thinking_effort: str,
    run_id: str,
) -> PublishedReport:
    """Assemble the FINDINGS-READY report and append the body-digest trailer.

    Validation runs FIRST (no-verdict, non-empty); hashing happens only after
    (D10). The status header and the trailer are the ONLY bytes local code adds
    to the model's judgment.
    """
    if not body_markdown.strip():
        raise RunnerError(
            RunnerErrorCode.REPORT_INVALID,
            "empty findings body",
            delivery_state=DeliveryState.DELIVERED,
        )
    assert_no_gate_tokens(body_markdown)

    header = "\n".join(
        [
            FINDINGS_STATUS_LINE,
            f"Artifact-Path: {artifact_path}",
            f"Artifact-SHA256: {artifact_sha256}",
            f"Bundle-SHA256: {bundle_sha256}",
            f"Model-Slug: {model_slug}",
            f"Thinking-Effort: {thinking_effort}",
            f"Run-Id: {run_id}",
        ]
    )
    # Strip any trailer the model may have emitted so local code owns the digest
    # (a model-supplied checksum has no authority — D10).
    model_body = "\n".join(
        line for line in body_markdown.splitlines() if not _TRAILER_RE.match(line)
    ).rstrip()
    body_without_trailer = f"{header}\n\n{model_body}\n"
    digest = canonical_body_digest(body_without_trailer)
    final_body = f"{body_without_trailer}{TRAILER_KEY} {digest}\n"
    return PublishedReport(body=final_body, body_sha256=digest)
