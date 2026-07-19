"""Agent profile models."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

PermissionMode = Literal["default", "acceptEdits", "plan", "auto", "bypassPermissions"]


class McpServer(BaseModel):
    """MCP server configuration."""

    type: Optional[str] = None
    command: str
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    timeout: Optional[int] = None


class ContainerPathMap(BaseModel):
    """Single host->guest path mapping for container environments."""

    host: str
    guest: str


class ContainerConfig(BaseModel):
    """Container environment configuration."""

    path_maps: Optional[List[ContainerPathMap]] = None


class AgentProfile(BaseModel):
    """Agent profile configuration with Q CLI agent fields."""

    name: str
    description: str
    provider: Optional[str] = None  # Provider override (e.g. "claude_code", "kiro_cli")
    system_prompt: Optional[str] = None  # The markdown content
    role: Optional[str] = None  # "supervisor", "developer", "reviewer"
    protected: Optional[bool] = None  # Refuse MCP deletion unless force=true

    # CAO-native. Per-agent skill-catalog scope: when set, only skills whose name
    # matches one of these patterns (exact name or fnmatch glob, e.g. "ads-*") are
    # injected into this agent's "## Available Skills" catalog at launch. None =
    # the full catalog (backward-compatible); [] = no skills advertised. Consumed
    # by CAO when composing the prompt, not passed through to provider JSON.
    skills: Optional[List[str]] = None
    sessionBrief: Optional[Literal["required", "optional"]] = None

    # CAO-native. Host->guest path maps for container-backed agents. Consumed by
    # the provider layer to translate host paths (e.g. temp prompt/MCP files)
    # into the guest paths the containerized CLI sees; not passed to provider JSON.
    container: Optional[ContainerConfig] = None

    # CAO-native. Per-profile override for provider initialization timeout (seconds).
    # When set, this value is used as the hard outer cap for CLI agent initialization
    # instead of the server default (60s from settings_service). Allows containerized
    # profiles to declare longer init times (e.g., 180s) without changing global config.
    provider_init_timeout: Optional[int] = None

    # Q CLI agent fields (all optional, will be passed through to JSON)
    prompt: Optional[str] = None
    mcpServers: Optional[Dict[str, Any]] = None
    # claude_code-only. When true, omit --strict-mcp-config so Claude Code
    # merges native user/project MCP config with CAO-injected servers.
    inheritUserMcpServers: Optional[bool] = None
    tools: Optional[List[str]] = Field(default=None)
    toolAliases: Optional[Dict[str, str]] = None
    allowedTools: Optional[List[str]] = None
    toolsSettings: Optional[Dict[str, Any]] = None
    resources: Optional[List[str]] = None
    hooks: Optional[Dict[str, Any]] = None
    useLegacyMcpJson: Optional[bool] = None
    model: Optional[str] = None
    # Generic model-effort hint; consumed by grok_cli and claude_code.
    reasoningEffort: Optional[str] = Field(default=None, min_length=1)
    permissionMode: Optional[PermissionMode] = None
    native_agent: Optional[str] = None  # Claude Code native agent name (thin-wrapper mode)
    messageContract: Optional[str] = Field(default=None, min_length=1)

    # Codex-only. Names a [profiles.<name>] block in ~/.codex/config.toml.
    # Used as --profile <name> when yolo mode is not active; unrestricted
    # allowed tools still force --yolo. min_length=1 prevents an explicit
    # empty string from silently degrading to --yolo, since this is a
    # permission-floor knob.
    codexProfile: Optional[str] = Field(default=None, min_length=1)

    # Codex-only. Inline Codex config overrides passed as `-c key=value` at
    # launch (e.g. {"model_reasoning_effort": "xhigh", "service_tier": "fast",
    # "features.fast_mode": True}). Keys may be dotted paths into Codex's
    # config.toml schema; values are serialized to TOML scalars (strings are
    # quoted, bools/numbers emitted bare). Applied in both the default --yolo
    # path and the --profile <codexProfile> path, so per-agent knobs like
    # reasoning effort or fast mode need no global ~/.codex/config.toml edits
    # or named profile files. Composes with codexProfile; because Codex applies
    # CLI overrides last, these win on key conflicts.
    codexConfig: Optional[Dict[str, Any]] = None

    # Hermes-only. Optionally names a Hermes profile wrapper command (for
    # example one created by `hermes profile alias <profile>`). When omitted,
    # the Hermes provider launches the default `hermes` command.
    hermesProfile: Optional[str] = Field(default=None, min_length=1)
