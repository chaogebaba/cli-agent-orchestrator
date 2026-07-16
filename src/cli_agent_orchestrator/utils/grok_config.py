"""Helpers for Grok user-scope MCP configuration.

Grok only discovers MCP servers from config files such as
``~/.grok/config.toml``. CAO must not write project-local ``.grok`` files, so
profile MCP declarations are upserted into the user config.
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict

from cli_agent_orchestrator.utils.sandbox_guard import bind_mcp_server_identity

logger = logging.getLogger(__name__)

GROK_CONFIG_FILE = Path.home() / ".grok" / "config.toml"
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")


def ensure_grok_mcp_servers(
    mcp_servers: Dict[str, Any] | None, *, terminal_id: str | None = None
) -> None:
    """Ensure profile MCP servers exist in ``~/.grok/config.toml``.

    The write is intentionally narrow: only ``[mcp_servers.<name>]`` and its
    nested tables for names present in *mcp_servers* are replaced. Unrelated
    user config and unrelated MCP servers are left intact.
    """
    if not mcp_servers:
        return

    content = GROK_CONFIG_FILE.read_text(encoding="utf-8") if GROK_CONFIG_FILE.exists() else ""
    for name, raw_config in mcp_servers.items():
        config = dict(raw_config)
        if terminal_id is not None:
            config = bind_mcp_server_identity(config, terminal_id)
        content = _upsert_mcp_server_section(content, name, config)

    GROK_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(GROK_CONFIG_FILE, content)
    logger.info("Ensured %d Grok MCP server(s) in %s", len(mcp_servers), GROK_CONFIG_FILE)


def _upsert_mcp_server_section(content: str, name: str, config: Dict[str, Any]) -> str:
    if not _MCP_NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid Grok MCP server name {name!r}: expected [A-Za-z0-9_-]+")

    command = config.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError(f"Grok MCP server {name!r} requires a non-empty command")

    args = config.get("args") or []
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError(f"Grok MCP server {name!r} args must be a list of strings")

    env = config.get("env") or {}
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise ValueError(f"Grok MCP server {name!r} env must be a string map")

    remaining = _remove_mcp_server_section(content, name).rstrip()
    section = _render_mcp_server_section(name, command, args, env)
    return f"{remaining}\n\n{section}" if remaining else section


def _remove_mcp_server_section(content: str, name: str) -> str:
    lines = content.splitlines(keepends=True)
    output: list[str] = []
    skip = False
    target_prefix = f"mcp_servers.{name}"

    for line in lines:
        table = _table_name(line)
        if table is not None:
            skip = table == target_prefix or table.startswith(f"{target_prefix}.")
        if not skip:
            output.append(line)

    return "".join(output)


def _table_name(line: str) -> str | None:
    match = _TABLE_RE.match(line)
    if not match:
        return None
    return match.group(1).strip()


def _render_mcp_server_section(
    name: str,
    command: str,
    args: list[str],
    env: Dict[str, str],
) -> str:
    lines = [
        f"[mcp_servers.{name}]\n",
        f"command = {_toml_string(command)}\n",
    ]
    if args:
        lines.append("args = [\n")
        for arg in args:
            lines.append(f"    {_toml_string(arg)},\n")
        lines.append("]\n")
    lines.append("enabled = true\n")

    if env:
        lines.append("\n")
        lines.append(f"[mcp_servers.{name}.env]\n")
        for key in sorted(env):
            if not _MCP_NAME_RE.fullmatch(key):
                raise ValueError(
                    f"Invalid env var name {key!r} for Grok MCP server {name!r}: "
                    "expected [A-Za-z0-9_-]+"
                )
            lines.append(f"{key} = {_toml_string(env[key])}\n")

    return "".join(lines)


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text atomically by replacing from the same directory."""
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            temp_name = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
