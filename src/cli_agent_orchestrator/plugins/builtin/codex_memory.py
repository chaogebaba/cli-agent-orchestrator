"""Codex CLI memory-injection plugin (built-in).

On ``post_create_terminal`` for a ``codex`` provider, writes the CAO memory
context block into ``<cwd>/AGENTS.md``, replacing any prior block delimited by
the cao-memory markers. Codex CLI reads ``AGENTS.md`` from the working
directory as project instructions, so the injected block is picked up
automatically on startup.

``AGENTS.md`` is a user-authored, repo-root file (the "README for agents"), so
this plugin owns only the delimited section and preserves all surrounding
hand-written content — the same replace-in-place approach as the Claude Code
plugin, *not* the whole-file ownership used for Kiro steering files.

Observer-only: the plugin runs *after* the terminal is created, so any
failure is logged and the terminal continues without memory context
rather than crashing ``cao-server``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import get_terminal_metadata
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
from cli_agent_orchestrator.utils.atomic_file import locked_atomic_rewrite

logger = logging.getLogger(__name__)

# Delimited section so repeated runs overwrite the same block rather than
# appending forever. Readers of AGENTS.md can also treat the delimiters as
# a well-known injection boundary.
BEGIN_MARKER = "<!-- cao-memory:begin -->"
END_MARKER = "<!-- cao-memory:end -->"
AGENTS_FILENAME = "AGENTS.md"


class CodexMemoryPlugin(CaoPlugin):
    """Inject CAO memory into the per-project AGENTS.md on terminal creation."""

    async def setup(self) -> None:
        """Nothing to configure; plugin is stateless."""

    async def teardown(self) -> None:
        """Nothing to close; plugin holds no resources."""

    @hook("post_create_terminal")
    async def on_post_create_terminal(self, event: PostCreateTerminalEvent) -> None:
        """Write the <cao-memory> block into <cwd>/AGENTS.md."""

        if event.provider != "codex":
            return
        from cli_agent_orchestrator.utils.persona_context import has_persona_plan

        if has_persona_plan(event.terminal_id):
            return
        # locked_atomic_rewrite polls with time.sleep up to the lock
        # timeout; run the pipeline off the event loop so a contended lock
        # cannot stall cao-server for other terminals.
        await asyncio.to_thread(
            inject_memory_file,
            event,
            "codex_memory",
            lambda: self._resolve_working_directory(event),
            lambda: MemoryService().get_memory_context_for_terminal(event.terminal_id),
            self._validated_target_path,
            self._write_block,
            logger,
        )

    # ------------------------------------------------------------------
    # helpers

    def _resolve_working_directory(self, event: PostCreateTerminalEvent) -> str | None:
        """Look up the pane's working directory for the terminal via backend."""

        return resolve_working_directory(
            event, get_terminal_metadata, get_backend().get_pane_working_directory
        )

    def _validated_target_path(self, working_directory: str) -> Path:
        """Return <cwd>/AGENTS.md, rejecting paths that escape the cwd.

        Uses realpath for both the base and the final target so symlink
        trickery (including a symlinked AGENTS.md itself) cannot redirect the
        write outside the working directory.
        """

        return validated_target_path(working_directory, AGENTS_FILENAME)

    def _write_block(self, target: Path, context_block: str) -> None:
        """Write or replace the delimited memory section in AGENTS.md.

        AGENTS.md is user-authored, so an interrupted write must never
        truncate it, and concurrent writers (multiple terminals, or the CLI
        and cao-server touching the same working directory) must never lose
        each other's memory block. ``locked_atomic_rewrite`` holds an
        inter-process lock for the whole read-strip-append-replace cycle so
        ``compute_new_content`` always sees the latest published content.
        """

        def compute_new_content(existing: str) -> str:
            stripped = self._strip_existing_block(existing)
            separator = "" if not stripped or stripped.endswith("\n") else "\n"
            return f"{stripped}{separator}{BEGIN_MARKER}\n{context_block}\n{END_MARKER}\n"

        locked_atomic_rewrite(target, compute_new_content)

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
