"""Tests for KiroAgentConfig model (F113 AC1)."""

import json

from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig


class TestKiroAgentConfigPermissions:
    """AC1: permissions round-trip through model_dump_json."""

    def test_permissions_serialized_when_set(self) -> None:
        """permissions field appears in JSON when explicitly provided."""
        perms = {
            "rules": [
                {"capability": "shell", "effect": "allow"},
                {"capability": "web_fetch", "effect": "allow"},
                {
                    "capability": "mcp",
                    "match": ["builtin/*", "cao-mcp-server/*"],
                    "effect": "allow",
                },
            ]
        }
        config = KiroAgentConfig(
            name="test-agent",
            description="Test",
            permissions=perms,
        )
        dumped = json.loads(config.model_dump_json(exclude_none=True))
        assert dumped["permissions"] == perms

    def test_permissions_absent_when_none(self) -> None:
        """permissions key is absent from JSON when not set (exclude_none)."""
        config = KiroAgentConfig(
            name="test-agent",
            description="Test",
        )
        dumped = json.loads(config.model_dump_json(exclude_none=True))
        assert "permissions" not in dumped

    def test_empty_permissions_serialized_verbatim(self) -> None:
        """An explicit empty dict permissions serializes as {}."""
        config = KiroAgentConfig(
            name="test-agent",
            description="Test",
            permissions={},
        )
        dumped = json.loads(config.model_dump_json(exclude_none=True))
        assert dumped["permissions"] == {}
