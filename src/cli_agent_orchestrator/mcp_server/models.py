"""MCP server models."""

from typing import Optional

from pydantic import BaseModel, Field


class HandoffResult(BaseModel):
    """Result of a handoff operation."""

    success: bool = Field(description="Whether the handoff was successful")
    message: str = Field(description="A message describing the result of the handoff")
    output: Optional[str] = Field(None, description="The output from the target agent")
    terminal_id: Optional[str] = Field(None, description="The terminal ID used for the handoff")
    display_name: Optional[str] = Field(None, description="The display name (profile-id) for the handoff terminal")
    window_name: Optional[str] = Field(None, description="The tmux window name for the handoff terminal")
