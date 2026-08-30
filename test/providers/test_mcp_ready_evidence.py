"""F537 D20 — MCP readiness evidence type + classification (Do-NOT 23).

The classifier core: an undeclared provider (evidence None) is EXEMPT
(mcp_unverified, never ERROR); a declared-and-connected artifact is mcp_ready;
a declared-but-not-connected artifact is E-MCP-UNAVAILABLE. Readiness is never
read from pane text — MCPEvidence.source names a durable artifact.

NOTE (build report): the kiro evidence PARSER (the `Prepared N MCP servers`
worker-log line + ~/.kiro/logs/*/mcp.log `mcp.connect.ok`) and the assign-result
wiring are deferred with the cline leg — the exact durable artifact format is
cited to HANDOFF.md:237, which is not present in this tree, so implementing the
parser here would be inventing the artifact shape. The classifier core below is
the verifiable, provider-agnostic half and is landed now.
"""

from cli_agent_orchestrator.providers.base import (
    MCP_READY,
    MCP_UNAVAILABLE,
    MCP_UNVERIFIED,
    BaseProvider,
    MCPEvidence,
    classify_mcp_readiness,
)


def test_undeclared_provider_is_exempt_unverified():
    assert classify_mcp_readiness(None) == MCP_UNVERIFIED


def test_declared_not_connected_is_unavailable():
    ev = MCPEvidence(declared=True, connected=False, source="~/.kiro/logs/x/mcp.log")
    assert classify_mcp_readiness(ev) == MCP_UNAVAILABLE


def test_declared_connected_is_ready():
    ev = MCPEvidence(declared=True, connected=True, source="Prepared 3 MCP servers")
    assert classify_mcp_readiness(ev) == MCP_READY


def test_declared_false_evidence_is_exempt():
    """An MCPEvidence with declared=False is treated as undeclared → exempt."""
    ev = MCPEvidence(declared=False, connected=False, source="")
    assert classify_mcp_readiness(ev) == MCP_UNVERIFIED


def test_base_provider_default_evidence_is_none_exempt():
    """Every non-overriding provider returns None → mcp_unverified, never ERROR
    (Do-NOT 23: an undeclared provider is exempt, spawn unaffected)."""
    assert BaseProvider.mcp_ready_evidence(None) is None  # type: ignore[arg-type]
    assert classify_mcp_readiness(BaseProvider.mcp_ready_evidence(None)) == MCP_UNVERIFIED  # type: ignore[arg-type]


def test_evidence_source_is_never_pane_text_by_contract():
    """The source field names a durable artifact; the classifier never inspects
    pane text — it decides only on declared/connected."""
    ev = MCPEvidence(declared=True, connected=True, source="worker.log:Prepared N MCP servers")
    assert classify_mcp_readiness(ev) == MCP_READY
    # connected verdict, not source string, drives the classification.
    ev2 = MCPEvidence(declared=True, connected=False, source="worker.log:Prepared N MCP servers")
    assert classify_mcp_readiness(ev2) == MCP_UNAVAILABLE
