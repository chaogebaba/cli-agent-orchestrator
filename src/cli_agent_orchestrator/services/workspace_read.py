"""Workspace read service — F862 Amendment D connector entry point (D5/D7).

This module is the service-layer façade the CAO runner composes to serve one
attempt's read-only pull plane.  The behaviour is the ported upstream
contract (``XiaoDuoYa/codex-with-chatgpt`` at commit
``8fdd97c188c7678d0d9c43b3769b426940de568a``, MIT; modules under
``workspace_connector/``); this service binds an attempt:

* a frozen read-only worktree checked out at the reviewed source commit,
* the attempt's allowlisted source manifest (enforced by the connector, not
  only stated in the prompt),
* the attempt-scoped audit projection and pull budgets,
* the loopback-bound Streamable-HTTP connector server reached through the
  operator's HTTPS tunnel (the tunnel is not an auth boundary).

There is no write, shell, patch, commit, terminal-input or arbitrary-command
tool to authorize (D5 kill list).
"""

from __future__ import annotations

from pathlib import Path

from cli_agent_orchestrator.workspace_connector import budgets as budgets_mod
from cli_agent_orchestrator.workspace_connector.audit import AttemptAudit
from cli_agent_orchestrator.workspace_connector.auth_store import AuthStore
from cli_agent_orchestrator.workspace_connector.http_server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ConnectorServer,
)
from cli_agent_orchestrator.workspace_connector.oauth import OAuthService
from cli_agent_orchestrator.workspace_connector.pairing import PairingManager
from cli_agent_orchestrator.workspace_connector.tools import WorkspaceTools
from cli_agent_orchestrator.workspace_connector.workspace_manager import Workspace


def bind_attempt(
    *,
    attempt_id: str,
    frozen_worktree: str | Path,
    manifest: list[str],
    reviewed_commit: str | None = None,
    base_commit: str | None = None,
    state_dir: str | Path | None = None,
    public_base_url: str | None = None,
) -> ConnectorServer:
    """Bind one attempt's read-only pull plane.

    ``frozen_worktree`` is the read-only worktree checked out at the reviewed
    source commit; ``manifest`` is the allowlisted source manifest the access
    token is bound to.  Reads outside it are refused ``PATH_NOT_IN_MANIFEST``.
    """
    workspace = Workspace(frozen_worktree, manifest=manifest)
    audit = AttemptAudit(attempt_id)
    budget = budgets_mod.PullBudget()
    tools = WorkspaceTools(
        workspace,
        audit=audit,
        budget=budget,
        reviewed_commit=reviewed_commit,
        base_commit=base_commit,
    )
    if state_dir is not None:
        store = AuthStore(
            workspace.id,
            attempt_id=attempt_id,
            manifest_digest=workspace.manifest_digest,
            file=Path(state_dir) / "auth" / f"{workspace.id}.json",
        )
    else:
        store = AuthStore(
            workspace.id,
            attempt_id=attempt_id,
            manifest_digest=workspace.manifest_digest,
        )
    pairing = PairingManager(workspace_id=workspace.id)
    oauth = OAuthService(store=store, pairing=pairing, workspace_name=workspace.name)
    return ConnectorServer(
        tools=tools,
        store=store,
        pairing=pairing,
        oauth=oauth,
        workspace_id=workspace.id,
        attempt_id=attempt_id,
        public_base_url=public_base_url,
    )


def default_host() -> str:
    return DEFAULT_HOST


def default_port() -> int:
    return DEFAULT_PORT
