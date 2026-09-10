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


#: D10 findings schema (r3). Each candidate finding MUST carry, on the live path,
#: a manifest-backed citation, the anchored OLD text quoted verbatim, an exact
#: REPLACEMENT text, and an evidence link. These markers make the shape
#: mechanically checkable; the design_findings persona emits them. A body whose
#: findings do not satisfy the schema is FINDINGS-INVALID, never promoted (r2
#: gate: the live harness promoted a schema-invalid body).
_FINDING_HEAD_RE = re.compile(r"^\s*(?:#+\s*)?Finding\s+\d+\b", re.IGNORECASE | re.MULTILINE)
_CITE_RE = re.compile(r"(?:\bCite\s*:|\bfile:line\b|[A-Za-z0-9_./-]+:\d+|\b(?:D\d+|AC-?\d+)\b)")
_OLD_RE = re.compile(r"\bOLD\s*:", re.IGNORECASE)
_REPLACE_RE = re.compile(r"\bREPLACE\s*:", re.IGNORECASE)
_EVIDENCE_RE = re.compile(r"\bEvidence\s*:", re.IGNORECASE)


def _split_findings(body: str) -> list[str]:
    """Split a findings body into per-finding blocks on the ``Finding N`` head."""
    heads = list(_FINDING_HEAD_RE.finditer(body))
    if not heads:
        return []
    blocks: list[str] = []
    for i, m in enumerate(heads):
        start = m.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        blocks.append(body[start:end])
    return blocks


def validate_findings_schema(body: str) -> None:
    """D10: enforce the findings schema on the live path (r3).

    Raises ``report_invalid`` (→ FINDINGS-INVALID) when the body has no
    ``Finding N`` blocks, or ANY block is missing its citation, its verbatim OLD
    quote, its exact REPLACE text, or its evidence link. A syntactically numbered
    finding that omits the exact old-quote or replacement is NEVER promoted (the
    r2 gate defect).
    """
    blocks = _split_findings(body)
    if not blocks:
        raise RunnerError(
            RunnerErrorCode.REPORT_INVALID,
            "findings body contains no 'Finding N' block",
            delivery_state=DeliveryState.DELIVERED,
        )
    for idx, block in enumerate(blocks, 1):
        missing = []
        if not _CITE_RE.search(block):
            missing.append("citation")
        if not _OLD_RE.search(block):
            missing.append("OLD: verbatim quote")
        if not _REPLACE_RE.search(block):
            missing.append("REPLACE: exact replacement")
        if not _EVIDENCE_RE.search(block):
            missing.append("Evidence: link")
        if missing:
            raise RunnerError(
                RunnerErrorCode.REPORT_INVALID,
                f"finding #{idx} missing required element(s): {', '.join(missing)}",
                delivery_state=DeliveryState.DELIVERED,
            )


def verify_citations_against_manifest(body: str, manifest_text: str) -> None:
    """D10: verify each finding's OLD quote actually exists in the manifest bytes.

    Positive/negative fixtures exercise this: an OLD quote that is not a verbatim
    substring of the pinned bundle is a dangling citation → ``report_invalid``.
    """
    for idx, block in enumerate(_split_findings(body), 1):
        m = _OLD_RE.search(block)
        if not m:
            continue
        # The OLD quote is the backticked or quoted span after ``OLD:`` up to the
        # next marker line.
        tail = block[m.end() :]
        quote = _extract_quoted(tail)
        if quote and quote not in manifest_text:
            raise RunnerError(
                RunnerErrorCode.REPORT_INVALID,
                f"finding #{idx} OLD quote is not a verbatim substring of the pinned bundle",
                delivery_state=DeliveryState.DELIVERED,
            )


def _extract_quoted(text: str) -> str:
    """Extract the first backticked span, else the first quoted line, from ``text``."""
    tick = re.search(r"`([^`]+)`", text)
    if tick:
        return tick.group(1).strip()
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line.strip().strip('"').strip()


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
    validate_schema: bool = True,
    manifest_text: "str | None" = None,
) -> PublishedReport:
    """Assemble the FINDINGS-READY report and append the body-digest trailer.

    Validation runs FIRST (no-verdict, non-empty, D10 findings schema); hashing
    happens only after (D10). A body that fails the schema raises ``report_invalid``
    (→ the run ends FINDINGS-INVALID, never FINDINGS-READY). The status header and
    the trailer are the ONLY bytes local code adds to the model's judgment.

    ``validate_schema`` defaults True on the live path; a caller with a
    non-findings body (e.g. the plain smoke run) may pass False. ``manifest_text``,
    when given, additionally verifies each OLD quote is a verbatim substring of
    the pinned bundle (dangling-citation check).
    """
    if not body_markdown.strip():
        raise RunnerError(
            RunnerErrorCode.REPORT_INVALID,
            "empty findings body",
            delivery_state=DeliveryState.DELIVERED,
        )
    assert_no_gate_tokens(body_markdown)
    if validate_schema:
        validate_findings_schema(body_markdown)
        if manifest_text is not None:
            verify_citations_against_manifest(body_markdown, manifest_text)

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
