import io
import json
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.hooks.transcript_binding import main


def test_transport_inherits_identity_and_bearer(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    event = {"session_id": "effective", "transcript_path": "/trace", "cwd": "/work",
             "source": "resume"}
    response = MagicMock()
    with patch("sys.stdin", io.StringIO(json.dumps(event))), \
         patch("cli_agent_orchestrator.hooks.transcript_binding.get_local_bearer",
               return_value="secret"), \
         patch("cli_agent_orchestrator.hooks.transcript_binding.requests.post",
               return_value=response) as post:
        assert main() == 0
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}
    assert post.call_args.args[0].endswith("/terminals/abcd1234/transcript-binding")
    response.raise_for_status.assert_called_once()
