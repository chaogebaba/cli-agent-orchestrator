"""F254 Phase 5 — Contract-tier (C kind) tests.

These tests drive MCP tool functions against a live cao_server subprocess
with zero patching of server.requests / server.cao_http. The wire contract
between the MCP layer and the HTTP API is the thing being tested.
"""
