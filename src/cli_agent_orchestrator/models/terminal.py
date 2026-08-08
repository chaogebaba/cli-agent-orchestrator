from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.provider import ProviderType

# Terminal ID validation (8 character hex string)
TerminalId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{8}$")]
InboxReceiverId = Annotated[str, StringConstraints(pattern=r"^(?:[a-f0-9]{8}|mb_[a-f0-9]{8})$")]
RecoveryState = Literal[
    "rebind_starting",
    "rebind_exiting",
    "rebind_failed",
    "rebound",
    "fallback_starting",
    "fallback_ready",
]


class TerminalStatus(str, Enum):
    """Terminal status enumeration with provider-aware states."""

    UNKNOWN = "unknown"
    IDLE = "idle"
    PROCESSING = "processing"
    COMPLETED = "completed"
    WAITING_USER_ANSWER = "waiting_user_answer"
    RENDER_UNCERTAIN = "render_uncertain"
    ERROR = "error"


class Terminal(BaseModel):
    """Terminal model - represents a tmux window."""

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(..., description="Unique terminal identifier")
    name: str = Field(..., description="Terminal/window name")
    provider: ProviderType = Field(..., description="CLI tool provider")
    session_name: str = Field(..., description="Session name")
    agent_profile: Optional[str] = Field(None, description="Agent profile")
    caller_id: Optional[str] = Field(
        None, description="Terminal that created this one via handoff/assign (callback target)"
    )
    caller_mailbox_id: Optional[str] = Field(
        None, description="Durable mailbox of the recorded caller, derived by the server"
    )
    lifecycle: Literal["ephemeral", "sticky"] = "ephemeral"
    reparented_from: Optional[str] = None
    allowed_tools: Optional[List[str]] = Field(None, description="Allowed CAO tools")
    engine: Optional[KiroEngine] = Field(None, description="Resolved Kiro engine")
    shell_command: Optional[str] = Field(
        None, description="Shell process name captured before kiro launch"
    )
    group: Optional[List[str]] = Field(
        None,
        description=(
            "Ordered, general-to-specific grouping array (e.g. "
            '["tenant_1", "project_5", "folder_12"]). CAO does ordered-prefix '
            "matching only; consumers own what the levels mean. None = this "
            "terminal participates in no group-based discovery (see "
            "list_siblings)."
        ),
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None, description="Free-form, consumer-defined JSON describing what this terminal is doing"
    )
    status: Optional[TerminalStatus] = Field(
        None, description="Current terminal status (live only)"
    )
    input_gen: int = Field(
        0,
        description=(
            "Input-event generation; trust dialogs and special keys also bump it. "
            "On event-inbox terminals it may advance while status_gen stays 0."
        ),
    )
    status_gen: int = Field(
        0,
        description=(
            "Generation of the latest ready status after processing. On event-inbox "
            "terminals it stays 0; clients must never treat 0 as fresh COMPLETED."
        ),
    )
    last_active: Optional[datetime] = Field(None, description="Last active timestamp")
    provider_session_id: Optional[str] = None


class ForkContext(BaseModel):
    mode: Literal["fork", "resume"]
    session_uuid: str
    base_name: str
    provider: str
    initial_preamble: str


class AgentStepResult(BaseModel):
    """Transient result of one agent step (issue #312, C3b). Not persisted.

    ``run_agent_step`` returns this ONLY on success (status COMPLETED); all
    failure modes raise narrow exceptions instead. It lives here in the terminal
    layer (not the workflow module) because it is the generic step substrate's
    return type and is conceptually workflow-independent — keeping it out of
    ``models/workflow.py`` lets ``services/agent_step.py`` avoid importing the
    workflow module (and its jsonschema/yaml deps).
    """

    terminal_id: str
    last_message: str
    status: TerminalStatus
