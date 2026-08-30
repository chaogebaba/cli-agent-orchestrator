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



# ---------------------------------------------------------------------------
# F582 slice B — D20 cline leg (#393): cline stays mcp_unverified (no artifact)
# ---------------------------------------------------------------------------
#
# FINDING (#393, "no durable artifact"): cline's MCP config is MATERIALIZED at
# spawn — ClineCliProvider._materialize_mcp_settings (cline_cli.py:449-503)
# writes cline_mcp_settings.json with a per-server ``timeout`` (F537 b8e56eeb)
# so cline does not skip cao-mcp-server on its 3 s init default. But cline emits
# NO durable connect-ok line / readiness artifact on a SUCCESSFUL attach — the
# handshake happens inside cline's own process with no observable log CAO can
# read. Per Do-NOT 23 and D20, a provider with no declared artifact is EXEMPT
# (mcp_unverified), NEVER ERROR — inventing a connect-log parser here would be
# fabricating an artifact cline does not write. So cline inherits the
# BaseProvider default (mcp_ready_evidence -> None) and classifies mcp_unverified.
# The kiro evidence parser + assign-result wiring remain DEFERRED (HANDOFF.md:237
# artifact format not present in tree; corpus INDEX has no kiro MCP readiness
# artifact).


def test_cline_provider_is_exempt_unverified_no_durable_artifact():
    """#393: cline materializes MCP config at spawn but writes no connect-ok
    artifact, so it stays mcp_unverified (exempt), never ERROR."""
    from cli_agent_orchestrator.providers.cline_cli import ClineCliProvider

    # cline does NOT override mcp_ready_evidence — it inherits the exempt default.
    assert "mcp_ready_evidence" not in vars(ClineCliProvider)
    assert ClineCliProvider.mcp_ready_evidence(None) is None  # type: ignore[arg-type]
    assert (
        classify_mcp_readiness(ClineCliProvider.mcp_ready_evidence(None))  # type: ignore[arg-type]
        == MCP_UNVERIFIED
    )


def test_no_wp_provider_overrides_mcp_evidence_yet():
    """Regression guard for the D20 shipped state: every provider is exempt this
    WP (kiro parser + cline artifact both deferred). If a provider later declares
    an artifact, this guard flags that the assign-result wiring must land too."""
    from cli_agent_orchestrator.providers.cline_cli import ClineCliProvider
    from cli_agent_orchestrator.providers.codex import CodexProvider
    from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

    for cls in (ClineCliProvider, CodexProvider, KiroCliProvider):
        assert "mcp_ready_evidence" not in vars(cls), (
            f"{cls.__name__} declared an MCP artifact — assign-result wiring "
            "(mcp_unverified vs E-MCP-UNAVAILABLE) must land with it (D20)"
        )
