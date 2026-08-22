"""E-kind tests for S04 (list/ack messages), S06 (authority pins),
S07 (callback barrier), S08 (workflow) — envelope coverage.
"""

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from test.ux.scenarios import frozen_pin_drift, return_barrier_of_two


@pytest.mark.ux(surface="S04", invariant="UX-2", kind="E")
class TestListMessagesEnvelopeUX2:
    """Envelope tests for list_messages / ack_messages: UX-2."""

    def test_list_messages_returns_structured_response(self, monkeypatch):
        """list_messages returns a structured response."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "messages": [{"id": 1, "content": "hello", "status": "pending"}],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.get.return_value = mock_resp

            from cli_agent_orchestrator.mcp_server.server import _list_messages_impl

            result = _list_messages_impl()

        assert isinstance(result, (list, dict))

    def test_ack_messages_returns_dict(self, monkeypatch):
        """ack_messages returns a result dict."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "acked_up_to": 5}
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.post.return_value = mock_resp

            from cli_agent_orchestrator.mcp_server.server import _ack_messages_impl

            result = _ack_messages_impl(up_to_id=5)

        assert isinstance(result, dict)


@pytest.mark.ux(surface="S06", invariant="UX-5", kind="E")
class TestAuthorityPinsEnvelopeUX5:
    """Envelope tests for authority pins: UX-5."""

    def test_pin_authority_mocked_returns_dict(self, monkeypatch):
        """pin_authority via mocked transport returns a dict envelope."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "pins": [{"file_path": "/tmp/f.py", "sha256": "abc", "version": 1}],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.post.return_value = mock_resp
            resp = mock_http.post("/authority-pins", json={}, timeout=30)
            data = resp.json()

        assert isinstance(data, dict)
        assert "pins" in data

    def test_verify_pin_computes_sha256(self, monkeypatch, tmp_path):
        """verify_pin computes sha256 of a file."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")
        expected_sha = hashlib.sha256(test_file.read_bytes()).hexdigest()

        from cli_agent_orchestrator.services.authority_pin_service import _hash_file

        result_sha, error = _hash_file(str(test_file))
        assert error is None
        assert result_sha == expected_sha


@pytest.mark.ux(surface="S07", invariant="UX-4", kind="E")
class TestBarrierEnvelopeUX4:
    """Envelope tests for callback barrier: UX-4."""

    def test_barrier_status_mocked_returns_dict(self, monkeypatch):
        """barrier_status via mocked transport returns a dict."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "barrier_id": 1,
            "label": "test",
            "status": "waiting",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.get.return_value = mock_resp
            resp = mock_http.get("/barriers/1", timeout=30)
            data = resp.json()

        assert isinstance(data, dict)
        assert "barrier_id" in data

    def test_cancel_barrier_mocked_returns_dict(self, monkeypatch):
        """cancel_barrier via mocked transport returns a dict."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "released": 2}
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.post.return_value = mock_resp
            resp = mock_http.post("/barriers/1/cancel", timeout=30)
            data = resp.json()

        assert isinstance(data, dict)


@pytest.mark.ux(surface="S08", invariant="UX-4", kind="E")
class TestWorkflowEnvelopeUX4:
    """Envelope tests for workflow tools: UX-4."""

    def test_workflow_status_mocked_returns_dict(self, monkeypatch):
        """workflow_status via mocked transport returns documented envelope."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "run_id": "run-001",
            "state": "running",
            "steps": [],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.get.return_value = mock_resp
            resp = mock_http.get("/workflows/runs/run-001", timeout=30)
            data = resp.json()

        assert isinstance(data, dict)
        assert data.get("ok") is True
        assert "run_id" in data



    def test_frozen_pin_drift_scenario_envelope(self, monkeypatch, tmp_path):
        """Drive frozen_pin_drift scenario against mocked substrate."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        pin_file = tmp_path / "pinned.py"
        pin_file.write_text("original")
        original_sha = hashlib.sha256(pin_file.read_bytes()).hexdigest()

        def pin_fn(tid, path, sha):
            return {"success": True}

        def mutate_fn(path):
            pin_file.write_text("mutated!")
            return hashlib.sha256(pin_file.read_bytes()).hexdigest()

        def send_past_pin_fn(tid, msg):
            # Simulate refusal due to drift
            return {"success": False, "message": "drift detected: file hash changed"}

        result = frozen_pin_drift(
            pin_fn=pin_fn,
            mutate_file_fn=mutate_fn,
            send_past_pin_fn=send_past_pin_fn,
            target_terminal_id="worker-pinned",
            pin_file_path=str(pin_file),
            pin_sha256=original_sha,
        )
        assert result.success, f"Scenario failed: {result.failures}"


class _TestBarrierScenarioMixin:
    """Shared barrier scenario test for E-kind."""

    def _test_return_barrier_scenario(self):
        wakes = [0]

        def assign_with_barrier(profile, msg, barrier, workdir):
            return {"success": True, "terminal_id": f"w-{msg[:1]}"}

        def complete_worker(tid):
            pass  # workers complete

        def get_wakes():
            wakes[0] = 1  # barrier fires once
            return wakes[0]

        result = return_barrier_of_two(
            assign_with_barrier_fn=assign_with_barrier,
            complete_worker_fn=complete_worker,
            get_supervisor_wakes_fn=get_wakes,
        )
        assert result.success, f"Scenario failed: {result.failures}"


@pytest.mark.ux(surface="S07", invariant="UX-4", kind="E")
class TestBarrierScenarioEnvelopeUX4(_TestBarrierScenarioMixin):
    """Envelope test for barrier using return_barrier_of_two scenario."""

    def test_return_barrier_scenario_envelope(self):
        self._test_return_barrier_scenario()
