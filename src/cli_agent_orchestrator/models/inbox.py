"""Inbox message models."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class OrchestrationType(str, Enum):
    """Orchestration mode for a message delivery."""

    SEND_MESSAGE = "send_message"
    HANDOFF = "handoff"
    ASSIGN = "assign"
    MAILBOX_DIGEST = "mailbox_digest"


class MessageStatus(str, Enum):
    """Message status enumeration."""

    PENDING = "pending"
    HELD = "held"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"
    FAILED = "failed"
    DIGESTED = "digested"
    PARKED = "parked"
    CANCELLED = "cancelled"


class InboxMessage(BaseModel):
    """Inbox message model."""

    id: int = Field(..., description="Message ID")
    sender_id: str = Field(..., description="Sender terminal ID")
    receiver_id: str = Field(..., description="Receiver terminal ID")
    logical_receiver_id: str | None = Field(
        default=None, description="Durable logical mailbox destination, when present"
    )
    message: str = Field(..., description="Message content")
    orchestration_type: OrchestrationType = Field(
        default=OrchestrationType.SEND_MESSAGE, description="Orchestration mode"
    )
    status: MessageStatus = Field(..., description="Message status")
    park_warm: bool = Field(
        default=False,
        description="Deliver without arming a receiver watchdog episode",
    )
    failure_reason: str | None = Field(
        default=None, description="Terminal settlement reason, when available"
    )
    digested_into: int | None = Field(
        default=None, description="Digest message containing this row"
    )
    enqueue_generation: int | None = Field(
        default=None, description="Logical mailbox generation captured at enqueue"
    )
    owner_receiver_id: str | None = Field(
        default=None, description="Immutable terminal incarnation that owns parked mail"
    )
    owner_generation: int | None = Field(
        default=None, description="Immutable mailbox/lifecycle generation owning parked mail"
    )
    dead_to_successor: bool | None = Field(
        default=None, description="Whether parked mail belongs to a superseded incarnation"
    )
    barrier_id: int | None = Field(default=None, description="Callback barrier owning this row")
    barrier_member_key: str | None = Field(
        default=None, description="Callback barrier member that produced this row"
    )
    created_at: datetime = Field(..., description="Creation timestamp")
