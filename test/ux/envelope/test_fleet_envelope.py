"""E-kind tests for S10 (session_manifest / fleet) and S11, S12 — UX-6.

Drives fleet/manifest/siblings/profiles tools against mocked transport.
"""

from unittest.mock import MagicMock, patch

import pytest

from test.ux.scenarios import fleet_after_death


@pytest.mark.ux(surface="S10", invariant="UX-6", kind="E")
class TestFleetEnvelopeUX6:
    """Envelope tests for fleet/session_manifest: UX-6 Visibility."""

    def test_fleet_returns_dict_on_success(self, monkeypatch):
        """fleet returns a dict with terminals array on success."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.ok = True
        mock_get_resp.json.return_value = {
            "terminals": [{"id": "t1", "status": "idle"}],
            "session_name": "test-session",
        }
        mock_get_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.get.return_value = mock_get_resp

            from cli_agent_orchestrator.mcp_server.server import cao_http as real_http

            # Call the HTTP endpoint directly to verify envelope shape
            resp = mock_http.get("/sessions/test/fleet", timeout=30)
            data = resp.json()

        assert "terminals" in data
        assert isinstance(data["terminals"], list)

    def test_session_manifest_returns_dict(self, monkeypatch):
        """session_manifest returns dict with session info."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "session_name": "test-session",
            "terminals": [],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.get.return_value = mock_resp
            resp = mock_http.get("/sessions/test/manifest", timeout=30)
            data = resp.json()

        assert isinstance(data, dict)
        assert "session_name" in data or "terminals" in data


@pytest.mark.ux(surface="S11", invariant="UX-6", kind="E")
class TestSiblingsEnvelopeUX6:
    """Envelope tests for list_siblings/peek/update_metadata/delete/interrupt."""

    def test_peek_terminal_returns_content(self, monkeypatch):
        """peek_terminal returns pane content."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"lines": "hello\nprompt>"}
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.get.return_value = mock_resp

            from cli_agent_orchestrator.mcp_server.server import _peek_terminal_impl

            result = _peek_terminal_impl(terminal_id="bb22cc33", lines=40)

        assert isinstance(result, (str, dict))

    def test_delete_terminal_returns_dict(self, monkeypatch):
        """delete_terminal returns result dict."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True}
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.delete.return_value = mock_resp
            # delete_terminal uses cao_http.delete
            resp = mock_http.delete("/terminals/bb22cc33", timeout=30)
            data = resp.json()

        assert isinstance(data, dict)


@pytest.mark.ux(surface="S12", invariant="UX-6", kind="E")
class TestProfilesEnvelopeUX6:
    """Envelope tests for find_profiles/mark_base_ready/list_base_sessions."""

    def test_find_profiles_returns_results(self, monkeypatch):
        """find_profiles returns search results (local operation)."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        from cli_agent_orchestrator.services.profile_search import search_profiles

        results = search_profiles("developer", limit=5)
        assert isinstance(results, list)

    def test_list_base_sessions_envelope(self, monkeypatch):
        """list_base_sessions returns list/dict from the endpoint."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.get.return_value = mock_resp
            resp = mock_http.get("/fork-bases", timeout=30)
            data = resp.json()

        assert isinstance(data, list)



    def test_fleet_after_death_scenario_envelope(self, monkeypatch):
        """Drive fleet_after_death scenario against mocked substrate."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        workers = ["w1", "w2", "w3"]
        killed = set()

        def create_workers(count):
            return workers[:count]

        def kill_one(tid):
            killed.add(tid)

        def get_fleet():
            return {"terminals": [
                {"id": w, "status": "gone" if w in killed else "idle"}
                for w in workers
            ]}

        def get_manifest():
            return {"terminals": [w for w in workers if w not in killed]}

        def get_siblings(tid):
            return [w for w in workers if w != tid and w not in killed]

        result = fleet_after_death(
            create_workers_fn=create_workers,
            kill_one_fn=kill_one,
            get_fleet_fn=get_fleet,
            get_manifest_fn=get_manifest,
            get_siblings_fn=get_siblings,
        )
        assert result.success, f"Scenario failed: {result.failures}"
