"""Workspace and agent-store helpers the skill CLI is allowed to use.

Three needs, all pre-existing: find a file by walking a workspace's parents
(``cao-orchestrator ledger check``), name CAO's own agent store and the
``routing.toml`` inside it, and copy a file into that store without a reader
ever seeing a half-written buffer (``cao-orchestrator sync-routing``).

The atomic copy is published here rather than re-exported: before F1004 (#852)
``sync_routing`` imported ``cli.commands.redeploy._atomic_copy``, which pinned a
private symbol of another *edge* command module across the seam.  ``redeploy``
now imports it from here, so there is one implementation and it is the public
one.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from cli_agent_orchestrator.constants import local_agent_store_dir, routing_toml_path
from cli_agent_orchestrator.services.verification_service import find_workspace_file

__all__ = [
    "atomic_copy",
    "find_workspace_file",
    "local_agent_store_dir",
    "package_root",
    "routing_toml_path",
]


def package_root() -> Path:
    """The directory of the installed ``cli_agent_orchestrator`` package.

    A skill command that lints or cites the runtime needs the build that is
    actually installed, not a checkout that happens to nest the fork. Published
    here so the seam has a named way to ask, instead of each command importing
    the base package for its ``__file__``.
    """
    import cli_agent_orchestrator

    return Path(cli_agent_orchestrator.__file__).resolve().parent


def atomic_copy(src: Path, dst: Path) -> None:
    """Copy ``src`` over ``dst`` via a same-directory temp file + rename.

    F838 (#695): a plain copy truncates then streams, so a concurrent resolver
    (an assign resolving a composition stub) can read a half-written fragment.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
