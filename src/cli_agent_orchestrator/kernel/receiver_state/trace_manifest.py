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
        "classify_idle_reason",
        "emit_screen_signals",
        "publish_fresh_observation",
        "prove_terminal_identity",
    }
)
CONSUMER_MODULES = (
    "src/cli_agent_orchestrator/services/agent_step.py",
    "src/cli_agent_orchestrator/services/auto_responder.py",
    "src/cli_agent_orchestrator/services/inbox_service.py",
    "src/cli_agent_orchestrator/services/stalled_callback_watchdog.py",
)
TRACE_MANIFEST_PATH = Path("src/cli_agent_orchestrator/kernel/receiver_state/trace_manifest.txt")


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


def regenerate_manifest(repo_root: Path | None = None) -> tuple[int, int, bool]:
    """Regenerate the trace manifest and return hits, touched files, and change state."""

    root = Path(__file__).parents[4] if repo_root is None else repo_root
    manifest_path = root / TRACE_MANIFEST_PATH
    generated = generate_manifest(root)
    committed = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""

    def rows_by_path(manifest: str) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for row in manifest.splitlines():
            if not row:
                continue
            path, _, _ = row.rsplit(":", 2)
            grouped.setdefault(path, []).append(row)
        return grouped

    committed_by_path = rows_by_path(committed)
    generated_by_path = rows_by_path(generated)
    touched_files = sum(
        committed_by_path.get(path) != generated_by_path.get(path)
        for path in committed_by_path.keys() | generated_by_path.keys()
    )
    changed = committed != generated
    if changed:
        manifest_path.write_text(generated, encoding="utf-8")

    hit_count = len([row for row in generated.splitlines() if row])
    return hit_count, touched_files, changed


if __name__ == "__main__":
    print(generate_manifest(), end="")
