"""The model must be able to REACH the attempt's connector (live-turn blocker 1).

The live-turn prep lane found the pull plane unreachable in production: the
listener bound an OS-assigned port, `bind_attempt` was called without
`public_base_url` so the OAuth metadata advertised a loopback issuer, the
operator's tunnel pointed at a fixed port where nothing listened, and no pairing
code was ever surfaced. A turn in that state spends a mint, opens the logged-in
profile, and then fails `source_correlation` with an empty audit — the most
expensive way to discover a configuration problem.

These arms are offline. A real loopback listener stands in for the tunnel: the
"public" base URL is a real URL that a separate client really fetches.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path

import pytest

from cli_agent_orchestrator.chatgpt_web_runner import production
from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError, RunnerErrorCode
from cli_agent_orchestrator.chatgpt_web_runner.source_pull import ConnectorListener, call_tool
from cli_agent_orchestrator.services.workspace_read import bind_attempt

pytestmark = pytest.mark.unit


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "alpha.py").write_text("canary-alpha\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("canary-beta\n", encoding="utf-8")
    return tmp_path


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# =====================================================================
# The configured port and public URL
# =====================================================================


def test_the_bind_port_is_fixed_by_default(monkeypatch) -> None:
    """Not OS-assigned: the tunnel points at ONE port."""
    monkeypatch.delenv("CHATGPT_PULL_BIND_PORT", raising=False)
    assert production.pull_bind_port() == 18795


def test_the_bind_port_is_overridable(monkeypatch) -> None:
    monkeypatch.setenv("CHATGPT_PULL_BIND_PORT", "24680")
    assert production.pull_bind_port() == 24680


@pytest.mark.parametrize("value", ["not-a-port", "0", "70000", "-1"])
def test_a_bad_bind_port_is_a_typed_refusal(monkeypatch, value) -> None:
    monkeypatch.setenv("CHATGPT_PULL_BIND_PORT", value)
    with pytest.raises(RunnerError) as excinfo:
        production.pull_bind_port()
    assert excinfo.value.code is RunnerErrorCode.ACCESS_DENIED


def test_an_unset_public_url_refuses_before_anything_opens(monkeypatch) -> None:
    """The refusal names the consequence, not just the missing variable."""
    monkeypatch.delenv("CHATGPT_PULL_PUBLIC_BASE_URL", raising=False)
    with pytest.raises(RunnerError) as excinfo:
        production.pull_public_base_url()
    assert excinfo.value.code is RunnerErrorCode.ACCESS_DENIED
    assert "before the browser opens" in str(excinfo.value.hint)


@pytest.mark.parametrize("value", ["fedora.tail.ts.net:8443", "ftp://x/y", "   "])
def test_a_non_http_public_url_is_refused(monkeypatch, value) -> None:
    monkeypatch.setenv("CHATGPT_PULL_PUBLIC_BASE_URL", value)
    with pytest.raises(RunnerError):
        production.pull_public_base_url()


def test_a_trailing_slash_is_normalised(monkeypatch) -> None:
    monkeypatch.setenv("CHATGPT_PULL_PUBLIC_BASE_URL", "https://front.example/")
    assert production.pull_public_base_url() == "https://front.example"


# =====================================================================
# The reachability self-check
# =====================================================================


def test_the_self_check_passes_against_this_attempts_listener(workspace: Path) -> None:
    server = bind_attempt(
        attempt_id="reach-ok",
        frozen_worktree=workspace,
        manifest=["alpha.py"],
        state_dir=workspace / ".s-ok",
        public_base_url="http://127.0.0.1:0",
    )

    async def _run() -> None:
        async with ConnectorListener(server, port=_free_port()) as listener:
            await asyncio.to_thread(
                production.check_pull_plane_reachable, listener.base_url, "reach-ok"
            )

    asyncio.run(_run())  # must not raise


def test_the_self_check_refuses_a_listener_for_a_DIFFERENT_attempt(workspace: Path) -> None:
    """The exact funnel failure: the front door leads to the wrong listener.

    A bare 200 would have passed here. The health payload carries the attempt id
    so a stale tunnel target is caught before the profile opens.
    """
    server = bind_attempt(
        attempt_id="somebody-else",
        frozen_worktree=workspace,
        manifest=["alpha.py"],
        state_dir=workspace / ".s-other",
    )

    async def _run() -> None:
        async with ConnectorListener(server, port=_free_port()) as listener:
            await asyncio.to_thread(
                production.check_pull_plane_reachable, listener.base_url, "the-real-attempt"
            )

    with pytest.raises(RunnerError) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.code is RunnerErrorCode.ACCESS_DENIED
    assert "fronted by a DIFFERENT attempt" in str(excinfo.value.hint)


def test_the_self_check_refuses_a_dead_url() -> None:
    """Nothing listening — the state the funnel was actually in."""
    dead = f"http://127.0.0.1:{_free_port()}"
    with pytest.raises(RunnerError) as excinfo:
        production.check_pull_plane_reachable(dead, "any", timeout=2.0)
    assert excinfo.value.code is RunnerErrorCode.ACCESS_DENIED
    assert "not reachable" in str(excinfo.value.hint)


# =====================================================================
# An external client reaches the connector through the public URL
# =====================================================================


def test_an_external_client_reads_through_the_configured_public_url(workspace: Path) -> None:
    """The end-to-end shape the live turn needs, minus the tunnel.

    The listener binds the configured port, advertises the public base URL in its
    OAuth metadata, and a separate client — holding only that URL and a token —
    reads two manifest files through it.
    """
    port = _free_port()
    public = f"http://127.0.0.1:{port}"
    server = bind_attempt(
        attempt_id="reach-external",
        frozen_worktree=workspace,
        manifest=["alpha.py", "beta.py"],
        state_dir=workspace / ".s-ext",
        public_base_url=public,
    )

    def _metadata(url: str) -> dict:
        with urllib.request.urlopen(url, timeout=10) as response:
            return dict(json.loads(response.read().decode()))

    async def _run() -> tuple:
        async with ConnectorListener(server, port=port) as listener:
            assert listener.base_url == public
            meta = await asyncio.to_thread(
                _metadata, f"{public}/.well-known/oauth-protected-resource"
            )
            token = server.store.issue_tokens(client_id="model", scopes=["workspace.read"])[
                "access_token"
            ]
            reads = []
            for index, name in enumerate(("alpha.py", "beta.py")):
                reads.append(
                    await asyncio.to_thread(
                        call_tool,
                        public,
                        token,
                        "workspace_read_file",
                        {"path": name},
                        request_id=index + 1,
                    )
                )
            return meta, reads

    meta, reads = asyncio.run(_run())

    # The advertised issuer/resource is the PUBLIC url, not loopback-by-accident.
    assert public in json.dumps(meta), meta
    assert all(r["result"]["isError"] is False for r in reads), reads

    from cli_agent_orchestrator.api.routes_chatgpt_web_connector import connector_audit_projection
    from cli_agent_orchestrator.chatgpt_web_runner.source_pull import collect_pull_evidence

    evidence = collect_pull_evidence(connector_audit_projection(server))
    assert len(evidence.sources) == 2
    assert len(evidence.token_fingerprints) == 1


def test_a_pairing_code_is_minted_and_is_single_use(workspace: Path) -> None:
    """The operator needs one code; a second use of it must fail."""
    server = bind_attempt(
        attempt_id="pairing",
        frozen_worktree=workspace,
        manifest=["alpha.py"],
        state_dir=workspace / ".s-pair",
        public_base_url="http://127.0.0.1:1",
    )
    created = server.pairing.create()
    code = str(created["code"])
    assert code and float(created["expires_at"]) > 0

    assert server.pairing.verify(code)["ok"] is True
    # Consumed: the same code cannot pair a second client.
    assert server.pairing.verify(code)["ok"] is False


# =====================================================================
# The ordering: nothing opens the profile before the plane is proved
# =====================================================================


def _drive(log, attempt_id: str, workspace: Path, **kwargs):
    return production._drive_composed_turn(
        task_text="review it",
        run_id="run-reach",
        attempt_id=attempt_id,
        prompt_sha="0" * 64,
        intent_log=log,
        manifest=["alpha.py", "beta.py"],
        frozen_worktree=str(workspace),
        reviewed_commit=None,
        base_commit=None,
        pull_evidence={},
        **kwargs,
    )


def _attempt(tmp_path: Path, attempt_id: str):
    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import SendIntentLog
    from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import get_relay_hub
    from cli_agent_orchestrator.providers.chatgpt_web import ChatGptWebProvider

    get_relay_hub().reset_for_tests()
    ChatGptWebProvider.start_attempt(
        run_id="run-reach", attempt_id=attempt_id, prompt_sha="0" * 64, artifacts_dir=tmp_path
    )
    log = SendIntentLog(tmp_path / "attempts" / attempt_id)
    log.load()
    get_relay_hub().get(attempt_id).intent_log = log
    return log


@pytest.mark.parametrize(
    ("public_url", "expected"),
    [
        (None, "is unset"),
        ("http://127.0.0.1:1", "not reachable"),
    ],
)
def test_an_unusable_pull_plane_refuses_BEFORE_the_browser_launches(
    tmp_path, monkeypatch, workspace: Path, public_url, expected
) -> None:
    """The reorder, asserted by counting profile launches.

    Before this round the profile opened first, so a turn whose model could
    never reach the connector still cost a browser launch and a spent mint
    before failing `source_correlation` with an empty audit.
    """
    import cli_agent_orchestrator.chatgpt_web_runner.runtime as runtime

    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("CHATGPT_PULL_BIND_PORT", str(_free_port()))
    if public_url is None:
        monkeypatch.delenv("CHATGPT_PULL_PUBLIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("CHATGPT_PULL_PUBLIC_BASE_URL", public_url)

    launches: list[object] = []

    async def _launch(options: object) -> object:
        launches.append(options)
        raise AssertionError("the browser must not launch before the pull plane is proved")

    monkeypatch.setattr(runtime, "launch", _launch, raising=False)
    monkeypatch.setattr(runtime, "resolve_profile_dir", lambda: "/data/fake/profile", raising=False)
    monkeypatch.setattr(runtime, "pin_fingerprint_seed", lambda _p: "epoch", raising=False)

    log = _attempt(tmp_path, f"reach-order-{'unset' if public_url is None else 'dead'}")
    with pytest.raises(RunnerError) as excinfo:
        asyncio.run(_drive(log, log.record.attempt_id, workspace))

    assert excinfo.value.code is RunnerErrorCode.ACCESS_DENIED
    assert expected in str(excinfo.value.hint)
    assert launches == [], "resolve_profile_dir/launch was reached"


def test_a_reachable_plane_records_the_url_and_pairing_code(
    tmp_path, monkeypatch, workspace: Path
) -> None:
    """The two values the operator needs, in the ledger before the composer."""
    import cli_agent_orchestrator.chatgpt_web_runner.runtime as runtime
    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import AttemptState, SendIntentLog

    port = _free_port()
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("CHATGPT_PULL_BIND_PORT", str(port))
    monkeypatch.setenv("CHATGPT_PULL_PUBLIC_BASE_URL", f"http://127.0.0.1:{port}")

    reached: dict[str, object] = {}

    async def _launch(_options: object) -> object:
        # The plane is up by now; capture the ledger and stop the turn here.
        record = SendIntentLog(tmp_path / "attempts" / "reach-ok-order").load()
        reached["state"] = record.attempt_state
        reached["url"] = record.connector_public_base_url
        reached["code"] = record.connector_pairing_code
        reached["expires"] = record.connector_pairing_expires_at
        raise RuntimeError("stop the turn here")

    monkeypatch.setattr(runtime, "launch", _launch, raising=False)
    monkeypatch.setattr(runtime, "resolve_profile_dir", lambda: "/data/fake/profile", raising=False)
    monkeypatch.setattr(runtime, "pin_fingerprint_seed", lambda _p: "epoch", raising=False)

    log = _attempt(tmp_path, "reach-ok-order")
    with pytest.raises(RuntimeError, match="stop the turn here"):
        asyncio.run(_drive(log, "reach-ok-order", workspace))

    # CONNECTOR_READY is written with all three values, before any composer step.
    assert reached["state"] == AttemptState.CONNECTOR_READY.value
    assert reached["url"] == f"http://127.0.0.1:{port}"
    assert reached["code"], "the operator needs the pairing code"
    assert float(reached["expires"]) > 0
