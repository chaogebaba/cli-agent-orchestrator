"""Principal-bound in-process callback-barrier operations for MCP."""

import logging
from typing import Any

from cli_agent_orchestrator.clients.database import (
    callback_barrier_dispatch_allowed as _dispatch_allowed,
)
from cli_agent_orchestrator.clients.database import (
    callback_barrier_status,
    cancel_callback_barrier,
    create_inbox_message,
)
from cli_agent_orchestrator.services.inbox_service import inbox_service
from cli_agent_orchestrator.services.terminal_guard_service import require_input_allowed

logger = logging.getLogger(__name__)


def dispatch_allowed(sender_id: str, receiver_id: str) -> bool:
    """Return whether the process-bound sender owns the receiver callback route."""
    return _dispatch_allowed(sender_id, receiver_id)


def dispatch(
    *,
    sender_id: str,
    receiver_id: str,
    message: str,
    refresh_ingest: bool,
    barrier: str,
    barrier_timeout_seconds: int | None,
    barrier_member_key: str | None,
) -> dict[str, Any]:
    """Create and best-effort deliver one supervisor-owned barrier dispatch."""
    if not dispatch_allowed(sender_id, receiver_id):
        raise ValueError("callback barriers require supervisor ownership of the receiver")
    dispatch_barrier = {
        "label": barrier,
        "timeout_seconds": barrier_timeout_seconds,
        "member_key": barrier_member_key,
    }
    if receiver_id.startswith("mb_"):
        from cli_agent_orchestrator.services.mailbox_service import create_logical_inbox_message

        inbox_msg = create_logical_inbox_message(
            sender_id=sender_id,
            mailbox_id=receiver_id,
            message=message,
            refresh_ingest=refresh_ingest,
            dispatch_barrier=dispatch_barrier,
        )
    else:
        require_input_allowed(receiver_id, refresh_ingest=refresh_ingest)
        inbox_msg = create_inbox_message(
            sender_id,
            receiver_id,
            message,
            dispatch_barrier=dispatch_barrier,
        )
    try:
        inbox_service.deliver_pending(inbox_msg.receiver_id)
    except Exception as exc:
        logger.warning("Immediate delivery attempt failed for %s: %s", receiver_id, exc)
    return {
        "success": True,
        "message_id": inbox_msg.id,
        "sender_id": inbox_msg.sender_id,
        "receiver_id": inbox_msg.receiver_id,
        "created_at": inbox_msg.created_at.isoformat(),
    }


def status(
    *,
    barrier_id: int | None = None,
    barrier_label: str | None = None,
    owner_id: str,
) -> dict[str, Any]:
    """Inspect one barrier scoped to the process-bound owner identity."""
    return callback_barrier_status(
        barrier_id=barrier_id,
        barrier_label=barrier_label,
        owner_id=owner_id,
    )


def cancel(
    *,
    barrier_id: int | None = None,
    barrier_label: str | None = None,
    owner_id: str,
) -> dict[str, Any]:
    """Cancel one owner-scoped barrier and wake each released receiver."""
    result = cancel_callback_barrier(
        barrier_id=barrier_id,
        barrier_label=barrier_label,
        owner_id=owner_id,
    )
    for receiver_id in result.get("receiver_ids", []):
        inbox_service.deliver_pending(receiver_id)
    return result
