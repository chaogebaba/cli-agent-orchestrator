"""Bundle framing, manifest, size bounds, and attachment identity (D8, D3).

Pure logic: builds the pinned manifest, enforces the tested attachment envelope
(AC-10), computes and verifies the attachment identity tuple (AC-9), and owns the
same-origin read-containment predicates (AC-11b). No browser import.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)

#: The ONE tested attachment envelope (D8/AC-10). NOT a proven maximum — a larger
#: pack is refused with a typed error; never split, truncated or summarized.
MAX_BUNDLE_BYTES = 199_627
MAX_BUNDLE_LINES = 4_335

#: The only origin the runner may read from (D3/AC-11b).
ALLOWED_ORIGIN = "chatgpt.com"

#: The two calibrated attachment-readiness signals (D8, upload-probe.md). The
#: chip's filename and its "Document" label, stable from ~2s. The composer
#: attachment testid / spinner / percentage signals are NOT permitted until a
#: fresh probe calibrates them (they timed out and sent nothing, exit 8).
READINESS_SIGNAL_FILENAME = "filename_chip"
READINESS_SIGNAL_DOCUMENT_LABEL = "document_label"
CALIBRATED_READINESS_SIGNALS: frozenset[str] = frozenset(
    {READINESS_SIGNAL_FILENAME, READINESS_SIGNAL_DOCUMENT_LABEL}
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AttachmentIdentity:
    """The D8 attachment identity tuple, recorded in the manifest before upload.

    A run is accepted only when this recorded tuple matches the manifest AND
    exactly one attachment is present on the submitted turn. The model's ability
    to quote file content is a transport diagnostic and never establishes
    identity (D8).
    """

    file_sha256: str
    byte_length: int
    line_count: int
    submitted_filename: str
    composer_attachment_ref: Optional[str] = None

    def matches_manifest(self, manifest: "AttachmentIdentity") -> bool:
        """Byte-level identity match against the pre-upload MANIFEST (D8/AC-9).

        The manifest is recorded BEFORE upload and carries no composer-side
        reference, so this compares only the four byte-level fields. The
        composer-side reference is verified separately by
        :func:`verify_attachment_on_turn` (non-empty on the submitted turn, and
        equal to the attach-time reference when one is supplied).
        """
        return (
            self.file_sha256 == manifest.file_sha256
            and self.byte_length == manifest.byte_length
            and self.line_count == manifest.line_count
            and self.submitted_filename == manifest.submitted_filename
        )


def build_attachment_identity(data: bytes, submitted_filename: str) -> AttachmentIdentity:
    """Compute the pre-upload identity of a bundle's bytes (D8)."""
    text = data.decode("utf-8")  # raises on non-UTF-8: bundles are UTF-8 (D8)
    line_count = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    return AttachmentIdentity(
        file_sha256=sha256_bytes(data),
        byte_length=len(data),
        line_count=line_count,
        submitted_filename=submitted_filename,
    )


def enforce_bundle_bounds(data: bytes) -> None:
    """Refuse an over-limit bundle with a typed error; no split/truncate (AC-10)."""
    if len(data) > MAX_BUNDLE_BYTES:
        raise RunnerError(
            RunnerErrorCode.CONTEXT_TOO_LARGE,
            f"bundle {len(data)} bytes exceeds tested envelope {MAX_BUNDLE_BYTES}",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunnerError(
            RunnerErrorCode.CONTEXT_TOO_LARGE,
            f"bundle is not valid UTF-8: {exc.reason}",
            delivery_state=DeliveryState.NOTHING_SENT,
        ) from exc
    lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    if lines > MAX_BUNDLE_LINES:
        raise RunnerError(
            RunnerErrorCode.CONTEXT_TOO_LARGE,
            f"bundle {lines} lines exceeds tested envelope {MAX_BUNDLE_LINES}",
            delivery_state=DeliveryState.NOTHING_SENT,
        )


def verify_attachment_on_turn(
    manifest: AttachmentIdentity,
    observed: AttachmentIdentity,
    attachment_count: int,
    *,
    expected_ref: "str | None" = None,
) -> None:
    """AC-9: reject a submitted turn whose attachment identity does not match the
    manifest, carries no composer-side reference, or carries more/fewer than one
    attachment.

    ``manifest`` is the pre-upload byte record. ``observed`` is the identity read
    from the submitted turn — it MUST carry a non-empty composer-side reference.
    ``expected_ref`` (when given) is the reference recorded at attach time; the
    observed reference must equal it (the r2 adversarial ref-A vs ref-B fixture).
    """
    if attachment_count != 1:
        raise RunnerError(
            RunnerErrorCode.ATTACHMENT_IDENTITY,
            f"expected exactly one attachment, saw {attachment_count}",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    if not observed.composer_attachment_ref:
        raise RunnerError(
            RunnerErrorCode.ATTACHMENT_IDENTITY,
            "submitted turn carries no composer-side attachment reference",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    if expected_ref is not None and observed.composer_attachment_ref != expected_ref:
        raise RunnerError(
            RunnerErrorCode.ATTACHMENT_IDENTITY,
            "submitted composer-side reference differs from the attach-time reference",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    if not observed.matches_manifest(manifest):
        raise RunnerError(
            RunnerErrorCode.ATTACHMENT_IDENTITY,
            "submitted attachment identity does not match the manifest (byte fields)",
            delivery_state=DeliveryState.NOTHING_SENT,
        )


def readiness_reached(present_signals: "frozenset[str] | set[str]") -> bool:
    """AC-9: an attachment is ready only when BOTH calibrated signals are seen.

    A decorative spinner or a percentage is NOT a permitted signal — if the two
    calibrated signals are absent the caller returns ``upload_unconfirmed``
    BEFORE pressing Enter (never after a spinner deadline elapsed)."""
    return CALIBRATED_READINESS_SIGNALS.issubset(set(present_signals))


# --- Same-origin read containment (D3 / AC-11b) ---------------------------------


#: A backend conversation id is a bare uuid-shaped token (F862 r2: the ``WEB:``
#: route prefix is a FRONT-END id and 404s/429s on the backend path).
_CONVERSATION_ID_SHAPE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def assert_conversation_id_shape(conversation_id: str) -> None:
    """Refuse an id that is not a bare uuid, BEFORE it is put in a URL (AC-11b).

    The path check alone is not sufficient containment: neither ``urlparse``
    nor an in-page ``fetch('/backend-api/conversation/' + id)`` normalizes
    ``..`` the same way, so an id like ``../conversations`` compares equal to
    the permitted path here while the browser resolves it to the ENUMERATION
    endpoint the read exception exists to keep unreachable. Both transports
    route through this, so the guard cannot be added to one and forgotten in
    the other.
    """
    if not isinstance(conversation_id, str) or not _CONVERSATION_ID_SHAPE.match(conversation_id):
        raise RunnerError(
            RunnerErrorCode.READ_FORBIDDEN,
            "conversation id is not a bare uuid — refusing to build a read URL",
            delivery_state=DeliveryState.DELIVERED,
        )


def is_same_origin_read_allowed(url: str, owned_conversation_id: str) -> bool:
    """AC-11b: the read exception is bound to the conversation THIS run created
    and to the chatgpt.com origin. A GET for any other conversation id, an
    off-origin fetch, or an enumeration (conversations-list) endpoint is refused.
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host != ALLOWED_ORIGIN and not host.endswith("." + ALLOWED_ORIGIN):
        return False
    path = parsed.path or ""
    # The single permitted structured read: GET /backend-api/conversation/<owned>
    expected = f"/backend-api/conversation/{owned_conversation_id}"
    if path == expected:
        return True
    # The auth-session read solely to authorize the conversation GET (D3).
    if path == "/api/auth/session":
        return True
    return False


def is_enumeration_endpoint(url: str) -> bool:
    """AC-11b: the conversation-LISTING endpoint must be unreachable from the
    runner (no cookie export, no conversation enumeration, D3)."""
    parsed = urlparse(url or "")
    path = parsed.path or ""
    return path.rstrip("/").endswith("/backend-api/conversations")


def enforce_read_allowed(url: str, owned_conversation_id: str) -> None:
    """Raise ``read_forbidden`` unless ``url`` is the bounded permitted read."""
    assert_conversation_id_shape(owned_conversation_id)
    if is_enumeration_endpoint(url) or not is_same_origin_read_allowed(url, owned_conversation_id):
        raise RunnerError(
            RunnerErrorCode.READ_FORBIDDEN,
            "read outside the bounded same-origin conversation exception",
            delivery_state=DeliveryState.DELIVERED,
        )


def enforce_no_api_egress(url: str) -> None:
    """AC-11: refuse any egress to the OpenAI API host (dynamically constructed).

    Enforced at request time, not as a startup string scan (D3) — a scan cannot
    see a dynamically built host.
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host == "api.openai.com" or host.endswith(".api.openai.com"):
        raise RunnerError(
            RunnerErrorCode.EGRESS_FORBIDDEN,
            "egress to the OpenAI API is forbidden — web session only",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
