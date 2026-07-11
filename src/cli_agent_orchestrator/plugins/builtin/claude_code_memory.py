"""Claude Code memory-injection plugin (built-in).

On ``post_create_terminal`` for a ``claude_code`` provider, writes the CAO
memory context block into ``<cwd>/.claude/CLAUDE.md``, replacing any prior
block delimited by the cao-memory markers.

Observer-only: the plugin runs *after* the terminal is created, so any
failure is logged and the terminal continues without memory context
rather than crashing ``cao-server``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.plugins import PostCreateTerminalEvent, hook
from cli_agent_orchestrator.plugins.base import CaoPlugin
from cli_agent_orchestrator.plugins.builtin.memory_file import (
    inject_memory_file,
    resolve_working_directory,
    strip_existing_block,
    validated_target_path,
    write_marker_block,
)
from cli_agent_orchestrator.services.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Delimited section so repeated runs overwrite the same block rather than
# appending forever. Readers of CLAUDE.md can also treat the delimiters as
# a well-known injection boundary.
BEGIN_MARKER = "<!-- cao-memory:begin -->"
END_MARKER = "<!-- cao-memory:end -->"
CLAUDE_FILENAME = "CLAUDE.md"
CLAUDE_DIR = ".claude"


class ClaudeCodeMemoryPlugin(CaoPlugin):
    """Inject CAO memory into the per-project CLAUDE.md on terminal creation."""

    async def setup(self) -> None:
        """Nothing to configure; plugin is stateless."""

    async def teardown(self) -> None:
        """Nothing to close; plugin holds no resources."""

    @hook("post_create_terminal")
    async def on_post_create_terminal(self, event: PostCreateTerminalEvent) -> None:
        """Write the <cao-memory> block into <cwd>/.claude/CLAUDE.md."""

        if event.provider != "claude_code":
            return
        inject_memory_file(
            event,
            "claude_code_memory",
            lambda: self._resolve_working_directory(event),
            lambda: MemoryService().get_memory_context_for_terminal(event.terminal_id),
            self._validated_target_path,
            self._write_block,
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
        """Return <cwd>/.claude/CLAUDE.md, rejecting paths that escape the cwd.

        Uses realpath for both the base and the final target so symlink
        trickery cannot redirect the write outside the working directory.
        """

        return validated_target_path(working_directory, CLAUDE_DIR, CLAUDE_FILENAME)

    def _write_block(self, target: Path, context_block: str) -> None:
        """Write or replace the delimited memory section in CLAUDE.md."""

        write_marker_block(target, context_block, BEGIN_MARKER, END_MARKER)

    @staticmethod
    def _strip_existing_block(content: str) -> str:
        """Remove any prior cao-memory block so we replace rather than append.

        Each BEGIN is paired with the END that follows it. A stray BEGIN with
        no following END (or with another BEGIN before its END) is treated as
        corruption: only the marker token is removed, never the user content
        around it. This stops a stale unclosed BEGIN from later pairing with an
        unrelated block's END and deleting everything in between.
        """

        return strip_existing_block(content, BEGIN_MARKER, END_MARKER)
