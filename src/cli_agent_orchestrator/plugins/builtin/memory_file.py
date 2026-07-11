"""Shared filesystem operations for built-in memory injection plugins."""

import logging
import os
from pathlib import Path
from typing import Callable

from cli_agent_orchestrator.plugins import PostCreateTerminalEvent


def inject_memory_file(
    event: PostCreateTerminalEvent,
    plugin_name: str,
    resolve_cwd: Callable[[], str | None],
    get_context: Callable[[], str],
    target_for: Callable[[str], Path],
    write: Callable[[Path, str], None],
    logger: logging.Logger,
) -> None:
    """Run the shared observer-only resolve/fetch/validate/write pipeline."""
    try:
        working_directory = resolve_cwd()
    except Exception as exc:
        logger.warning(
            "%s: could not resolve working dir for %s: %s",
            plugin_name,
            event.terminal_id,
            exc,
        )
        return
    if not working_directory:
        logger.debug(
            "%s: no working directory for %s; skipping",
            plugin_name,
            event.terminal_id,
        )
        return

    try:
        context_block = get_context()
    except Exception as exc:
        logger.warning(
            "%s: memory fetch failed for %s: %s",
            plugin_name,
            event.terminal_id,
            exc,
        )
        return
    if not context_block:
        logger.debug(
            "%s: no memory context for %s; skipping write",
            plugin_name,
            event.terminal_id,
        )
        return

    try:
        target = target_for(working_directory)
    except ValueError as exc:
        logger.warning(
            "%s: path validation rejected %s: %s",
            plugin_name,
            working_directory,
            exc,
        )
        return
    try:
        write(target, context_block)
    except Exception as exc:
        logger.warning("%s: write failed for %s: %s", plugin_name, target, exc)


def resolve_working_directory(
    event: PostCreateTerminalEvent, metadata_getter, pane_cwd_getter
) -> str | None:
    """Look up the terminal pane's working directory."""
    metadata = metadata_getter(event.terminal_id)
    if metadata is None:
        return None

    session_name = metadata.get("tmux_session") or event.session_id
    window_name = metadata.get("tmux_window")
    if not session_name or not window_name:
        return None
    return pane_cwd_getter(session_name, window_name)


def validated_target_path(working_directory: str, *relative_parts: str) -> Path:
    """Resolve a target below an existing cwd, rejecting escape attempts."""
    if "\x00" in working_directory:
        raise ValueError("working directory contains null bytes")
    try:
        base = Path(working_directory).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"working directory {working_directory!r} is not resolvable: {exc}")
    target = base.joinpath(*relative_parts).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError(f"target {target} escapes working directory {base}")
    return target


def atomic_write_text(target: Path, content: str) -> None:
    """Atomically replace a UTF-8 text file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_suffix(target.suffix + ".tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_marker_block(
    target: Path,
    context_block: str,
    begin_marker: str,
    end_marker: str,
) -> None:
    """Write or replace a delimited block while preserving surrounding text."""
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    stripped = strip_existing_block(existing, begin_marker, end_marker)
    separator = "" if not stripped or stripped.endswith("\n") else "\n"
    atomic_write_text(
        target,
        f"{stripped}{separator}{begin_marker}\n{context_block}\n{end_marker}\n",
    )


def strip_existing_block(content: str, begin_marker: str, end_marker: str) -> str:
    """Remove prior delimited blocks without dropping stray-marker content."""
    while True:
        begin = content.find(begin_marker)
        if begin == -1:
            break
        end = content.find(end_marker, begin + len(begin_marker))
        next_begin = content.find(begin_marker, begin + len(begin_marker))
        if end == -1 or (next_begin != -1 and next_begin < end):
            content = content[:begin] + content[begin + len(begin_marker) :]
            continue
        before = content[:begin].rstrip("\n")
        after = content[end + len(end_marker) :].lstrip("\n")
        content = f"{before}\n{after}" if before and after else before or after
    return content
