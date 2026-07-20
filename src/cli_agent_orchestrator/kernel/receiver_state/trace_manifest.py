"""AST generator for the closed Stage-0a consumer trace manifest."""

from __future__ import annotations

import ast
from pathlib import Path

TRACE_SYMBOLS = frozenset(
    {
        "get_status",
        "probe_screen_status",
        "classify_screen",
        "get_status_from_screen",
        "force_status",
        "snapshot_view",
        "get_boundary_observation",
    }
)
CONSUMER_MODULES = (
    "src/cli_agent_orchestrator/services/agent_step.py",
    "src/cli_agent_orchestrator/services/auto_responder.py",
    "src/cli_agent_orchestrator/services/inbox_service.py",
    "src/cli_agent_orchestrator/services/stalled_callback_watchdog.py",
)


def generate_manifest(repo_root: Path | None = None) -> str:
    """Return canonical ``path:line:symbol`` rows for all closed trace calls."""

    root = Path(__file__).parents[4] if repo_root is None else repo_root
    rows: list[str] = []
    for relative_path in CONSUMER_MODULES:
        source_path = root / relative_path
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=relative_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                symbol = node.func.attr
            elif isinstance(node.func, ast.Name):
                symbol = node.func.id
            else:
                continue
            if symbol in TRACE_SYMBOLS:
                rows.append(f"{relative_path}:{node.lineno}:{symbol}")
    return "\n".join(sorted(rows)) + "\n"


if __name__ == "__main__":
    print(generate_manifest(), end="")
