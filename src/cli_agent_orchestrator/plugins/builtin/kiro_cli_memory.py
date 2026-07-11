"""Kiro CLI memory-injection plugin (built-in).

On ``post_create_terminal`` for a ``kiro_cli`` provider, writes the CAO
memory context to ``<cwd>/.kiro/steering/cao-memory.md``. Kiro CLI natively
loads every ``*.md`` file under ``.kiro/steering/``, so this file is picked
up automatically. The plugin owns this file end-to-end and overwrites it
whole on each run (no in-file markers).

Observer-only: runs after terminal creation, logs-and-skips on every
error path rather than crashing ``cao-server``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.plugins import PostCreateTerminalEvent, hook
from cli_agent_orchestrator.plugins.base import CaoPlugin
from cli_agent_orchestrator.plugins.builtin.memory_file import (
    atomic_write_text,
    inject_memory_file,
    resolve_working_directory,
    validated_target_path,
)
from cli_agent_orchestrator.services.memory_service import MemoryService

logger = logging.getLogger(__name__)

STEERING_SUBDIR = ".kiro/steering"
MEMORY_FILENAME = "cao-memory.md"


class KiroCliMemoryPlugin(CaoPlugin):
    """Inject CAO memory into the per-project Kiro steering directory."""

    async def setup(self) -> None:
        """Stateless; nothing to configure."""

    async def teardown(self) -> None:
        """Stateless; nothing to close."""

    @hook("post_create_terminal")
    async def on_post_create_terminal(self, event: PostCreateTerminalEvent) -> None:
        """Write <cwd>/.kiro/steering/cao-memory.md with the memory context."""

        if event.provider != "kiro_cli":
            return
        inject_memory_file(
            event,
            "kiro_cli_memory",
            lambda: self._resolve_working_directory(event),
            lambda: MemoryService().get_memory_context_for_terminal(event.terminal_id),
            self._validated_target_path,
            lambda target, context: atomic_write_text(target, context + "\n"),
            logger,
        )

    # ------------------------------------------------------------------
    # helpers

    def _resolve_working_directory(self, event: PostCreateTerminalEvent) -> str | None:
        """Look up the tmux pane's working directory for the terminal."""

        return resolve_working_directory(
            event, get_terminal_metadata, tmux_client.get_pane_working_directory
        )

    def _validated_target_path(self, working_directory: str) -> Path:
        """Return <cwd>/.kiro/steering/cao-memory.md, rejecting escape attempts.

        Uses realpath for both the base and the target so symlink trickery
        cannot redirect the write outside the working directory.
        """

        return validated_target_path(working_directory, STEERING_SUBDIR, MEMORY_FILENAME)
