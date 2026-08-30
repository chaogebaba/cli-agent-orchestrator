"""AC12 (Do-NOT conformance) grep-shaped tests for the D16-D23 slice-A rows.

These assert the Do-NOTs that are statically checkable on the shipped source:
  * Do-NOT 25 — no D17/D23 write path touches `recovery_state` or a free-form
    top-level metadata key; the children ledger lives ONLY under the reserved
    `cao` namespace via merge_terminal_system_metadata.
  * Do-NOT 20 — the D22 supervisor drain/ack hooks are composed as
    `python -m <module>`, never written into `~/.claude` / a `.claude/` path.
  * Do-NOT 21 — D23 does not change default send_message semantics: the new
    fields are optional (default None) on the model and the insert path.
  * Do-NOT 23 — an undeclared provider is `mcp_unverified`, never ERROR.
"""

from pathlib import Path

_SRC = Path("src/cli_agent_orchestrator")


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


# ---- Do-NOT 25: no recovery_state / free-form top-level write (D17/D23) ----


def test_children_ledger_writers_never_touch_recovery_state():
    db = _read("clients/database.py")
    # Isolate the two ledger mutators + the publish reconcile.
    start = db.index("def register_terminal_child")
    end = db.index("def set_terminal_worktree_info")
    ledger_region = db[start:end]
    assert "recovery_state" not in ledger_region


def test_children_ledger_writes_only_via_system_namespace():
    """D17: register/release/reconcile write through merge_terminal_system_metadata
    (the cao-namespace RMW), never the raw free-form json.dumps(meta) rewrite."""
    db = _read("clients/database.py")
    start = db.index("def register_terminal_child")
    end = db.index("def set_terminal_worktree_info")
    region = db[start:end]
    assert region.count("merge_terminal_system_metadata") >= 3  # register, release, reconcile
    # The old free-form full-dict rewrite is gone from these mutators.
    assert "terminal.metadata_json = _json.dumps(meta)" not in region


def test_d23_insert_path_never_writes_recovery_state():
    db = _read("clients/database.py")
    start = db.index("def _insert_routed_inbox_row")
    end = db.index("def attach_terminal_dispatch_barrier")
    assert "recovery_state" not in db[start:end]


# ---- Do-NOT 20: D22 hooks are `-m module`, never a ~/.claude path ----------


def test_d22_hooks_composed_as_module_not_claude_path():
    cc = _read("providers/claude_code.py")
    start = cc.index("drain_command = shlex.join")
    end = cc.index("settings = {", start)
    region = cc[start:end]
    assert "cli_agent_orchestrator.hooks.supervisor_drain" in region
    assert "cli_agent_orchestrator.hooks.supervisor_ack" in region
    assert ".claude" not in region


# ---- Do-NOT 21: D23 fields are opt-in (default None) -----------------------


def test_d23_fields_default_none_on_model():
    inbox = Path("src/cli_agent_orchestrator/models/inbox.py").read_text(encoding="utf-8")
    assert "expire_after_s: int | None = Field(" in inbox
    assert "supersede_key: str | None = Field(" in inbox


def test_d23_insert_defaults_are_none():
    db = _read("clients/database.py")
    sig_start = db.index("def _insert_routed_inbox_row")
    sig = db[sig_start : sig_start + 600]
    assert "expire_after_s: int | None = None" in sig
    assert "supersede_key: str | None = None" in sig


# ---- Do-NOT 23: undeclared provider is mcp_unverified, never ERROR ---------


def test_d20_undeclared_is_unverified_not_error():
    from cli_agent_orchestrator.providers.base import (
        MCP_UNVERIFIED,
        classify_mcp_readiness,
    )

    assert classify_mcp_readiness(None) == MCP_UNVERIFIED
    assert "ERROR" not in MCP_UNVERIFIED
