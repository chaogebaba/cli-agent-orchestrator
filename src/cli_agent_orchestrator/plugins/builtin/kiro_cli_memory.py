"""Kiro CLI memory-injection plugin (built-in).

On ``post_create_terminal`` for a ``kiro_cli`` provider, writes the CAO
memory context to ``<cwd>/.kiro/steering/cao-memory.md``. Kiro CLI natively
loads every ``*.md`` file under ``.kiro/steering/``, so this file is picked
up automatically. The plugin owns this file end-to-end and overwrites it
whole on each run (no in-file markers).

Unlike the Claude Code / Codex plugins, the whole-file overwrite means the
lost-update race (defect B) does not apply here — there is no
read-modify-write cycle to serialize, only a full replace. But the target
path is fixed *per working directory* (``<cwd>/.kiro/steering/cao-memory.md``),
so two terminals sharing a cwd still write the same file, and the old
fixed ``.tmp`` idiom is still exposed to defect A (one writer's
``finally``-unlink deleting the other's live temp file → ``FileNotFoundError``,
or a half-written temp being published). ``locked_atomic_rewrite`` closes
that hole too: it uses a unique per-call temp file and an inter-process
lock, so concurrent overwrites are safe even though the content itself does
not depend on the prior file state.

Observer-only: runs after terminal creation, logs-and-skips on every
error path rather than crashing ``cao-server``.
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
    validated_target_path,
)
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.utils.atomic_file import locked_atomic_rewrite

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
        # locked_atomic_rewrite polls with time.sleep up to the lock
        # timeout; run the whole resolve/fetch/write pipeline off the event
        # loop so a contended lock cannot stall cao-server for other terminals.
        await asyncio.to_thread(
            inject_memory_file,
            event,
            "kiro_cli_memory",
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
        """Return <cwd>/.kiro/steering/cao-memory.md, rejecting escape attempts.

        Uses realpath for both the base and the target so symlink trickery
        cannot redirect the write outside the working directory.
        """

        return validated_target_path(working_directory, STEERING_SUBDIR, MEMORY_FILENAME)

    def _write_block(self, target: Path, context_block: str) -> None:
        """Whole-file overwrite of cao-memory.md via locked atomic rewrite.

        Content does not depend on the prior file (plugin owns the path
        end-to-end), but concurrent writers sharing a cwd still need the
        inter-process lock + unique temp that ``locked_atomic_rewrite``
        provides (defect A). The compute callback ignores existing content
        by design.
        """

        locked_atomic_rewrite(target, lambda _existing: context_block + "\n")
