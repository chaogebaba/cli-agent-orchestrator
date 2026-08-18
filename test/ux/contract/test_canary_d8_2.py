"""Canary test: proves _assign_impl request reaches the cao_server subprocess.

D8.2 validation: the MCP tool's `_assign_impl` builds a real HTTP request
that arrives at the live `cao_server` subprocess. Proven by a ROW in the
subprocess's SQLite database (never the tool's return value).

If this test cannot pass, D8.2 is wrong and Phase 5 needs a design amendment.
"""

import sqlite3
import uuid

import pytest
import requests

from test.fixtures.cao_server import CaoServer


@pytest.mark.ux(surface="S01", invariant="UX-1", kind="C")
class TestCanaryD8_2:
    """Validate that _assign_impl's request reaches the cao_server subprocess DB."""

    def test_assign_creates_terminal_row_in_subprocess_db(
        self, cao_server: CaoServer, monkeypatch, tmp_path
    ):
        """Call _assign_impl with CAO_ENDPOINT pointed at the live server.

        Proof: a terminal row appears in the subprocess's SQLite database
        that was NOT there before the call.
        """
        # Step 1: Create a supervisor terminal on the server (needed for
        # _assign_impl's GET /terminals/{id} lookup).
        session_name = f"canary-{uuid.uuid4().hex[:8]}"
        resp = requests.post(
            f"{cao_server.url}/sessions",
            params={
                "provider": "mock_cli",
                "agent_profile": "developer",
                "session_name": session_name,
            },
            timeout=30,
        )
        resp.raise_for_status()
        supervisor_terminal = resp.json()
        supervisor_id = supervisor_terminal["id"]

        # Step 2: Count terminal rows BEFORE the assign call.
        conn = sqlite3.connect(str(cao_server.db_path))
        pre_count = conn.execute("SELECT COUNT(*) FROM terminals").fetchone()[0]

        # Step 3: Point the MCP layer at the live server and call _assign_impl.
        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", supervisor_id)

        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        result = _assign_impl(
            agent_profile="developer",
            message="canary task for D8.2 validation",
            working_directory=str(tmp_path),
        )

        # Step 4: Verify success at the API level (secondary evidence only).
        assert result["success"] is True, f"_assign_impl failed: {result}"
        worker_id = result["terminal_id"]
        assert worker_id is not None

        # Step 5: PRIMARY EVIDENCE — query the subprocess's database.
        # The terminal row must exist and match the assigned worker.
        post_count = conn.execute("SELECT COUNT(*) FROM terminals").fetchone()[0]
        assert post_count > pre_count, (
            f"No new terminal row in subprocess DB! pre={pre_count} post={post_count}"
        )

        row = conn.execute(
            "SELECT id, agent_profile, provider FROM terminals WHERE id = ?",
            (worker_id,),
        ).fetchone()

        # Dump evidence for the build report.
        all_terminals = conn.execute(
            "SELECT id, agent_profile, provider FROM terminals"
        ).fetchall()
        conn.close()

        print(f"\n=== CANARY DB EVIDENCE ===")
        print(f"Subprocess DB path: {cao_server.db_path}")
        print(f"Pre-assign terminal count: {pre_count}")
        print(f"Post-assign terminal count: {post_count}")
        print(f"Worker terminal ID: {worker_id}")
        print(f"DB row found: id={row[0]} profile={row[1]} provider={row[2]}")
        print(f"All terminals in subprocess DB:")
        for t in all_terminals:
            print(f"  id={t[0]} profile={t[1]} provider={t[2]}")
        print(f"=== END CANARY DB EVIDENCE ===\n")

        assert row is not None, (
            f"Worker terminal {worker_id} NOT FOUND in subprocess DB at "
            f"{cao_server.db_path} — D8.2 injection did not work."
        )
        assert row[0] == worker_id
        assert row[1] == "developer"
        assert row[2] == "mock_cli"
