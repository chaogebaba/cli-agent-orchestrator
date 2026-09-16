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
        reached["digest"] = record.connector_pairing_code_sha256
        reached["issued"] = record.connector_pairing_issued_at
        reached["expires"] = record.connector_pairing_expires_at
        raise RuntimeError("stop the turn here")

    monkeypatch.setattr(runtime, "launch", _launch, raising=False)
    monkeypatch.setattr(runtime, "resolve_profile_dir", lambda: "/data/fake/profile", raising=False)
    monkeypatch.setattr(runtime, "pin_fingerprint_seed", lambda _p: "epoch", raising=False)

    log = _attempt(tmp_path, "reach-ok-order")
    with pytest.raises(RuntimeError, match="stop the turn here"):
        asyncio.run(_drive(log, "reach-ok-order", workspace))

    # CONNECTOR_READY is written before any composer step, with the public URL
    # and the pairing AUDIT fields — never the raw code (see the ruling arms).
    assert reached["state"] == AttemptState.CONNECTOR_READY.value
    assert reached["url"] == f"http://127.0.0.1:{port}"
    assert reached["digest"], "the ledger needs a digest to identify the code later"
    assert float(reached["issued"]) > 0
    assert float(reached["expires"]) > float(reached["issued"])


def test_production_passes_the_public_url_through_to_bind_attempt(
    tmp_path, monkeypatch, workspace: Path
) -> None:
    """The coverage gap a surviving mutant exposed.

    The external-client arm calls ``bind_attempt`` itself, so it proves the
    CONNECTOR honours ``public_base_url`` — not that PRODUCTION supplies it.
    Setting it to None in production left that arm green. This one captures the
    real call.
    """
    import cli_agent_orchestrator.chatgpt_web_runner.runtime as runtime
    import cli_agent_orchestrator.services.workspace_read as workspace_read

    port = _free_port()
    public = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("CHATGPT_PULL_BIND_PORT", str(port))
    monkeypatch.setenv("CHATGPT_PULL_PUBLIC_BASE_URL", public)

    seen: dict[str, object] = {}
    real_bind = workspace_read.bind_attempt

    def _spy(**kwargs: object) -> object:
        seen.update(kwargs)
        return real_bind(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(workspace_read, "bind_attempt", _spy, raising=False)

    async def _launch(_options: object) -> object:
        raise RuntimeError("stop after the plane is up")

    monkeypatch.setattr(runtime, "launch", _launch, raising=False)
    monkeypatch.setattr(runtime, "resolve_profile_dir", lambda: "/data/fake/profile", raising=False)
    monkeypatch.setattr(runtime, "pin_fingerprint_seed", lambda _p: "epoch", raising=False)

    log = _attempt(tmp_path, "reach-passthrough")
    with pytest.raises(RuntimeError, match="stop after the plane is up"):
        asyncio.run(_drive(log, "reach-passthrough", workspace))

    assert seen.get("public_base_url") == public, (
        "production must pass the configured public URL, or the connector "
        "advertises a loopback issuer ChatGPT cannot reach"
    )


# =====================================================================
# The pairing code stays OUT of the durable ledger (supervisor ruling)
# =====================================================================


def test_the_raw_pairing_code_is_absent_from_the_ledger_and_present_in_the_file(
    tmp_path, monkeypatch, workspace: Path
) -> None:
    """The ruling, asserted on the bytes rather than on the field names.

    The durable record refuses raw secrets — `create_locked_attempt` will not
    even accept the relay token — so a single-use code is no exception just
    because it is short-lived. What the record keeps is a digest and two
    timestamps, enough to prove later WHICH code was used without holding it.
    The operator's copy lives in one 0600 file next to the attempt.
    """
    import hashlib
    import stat

    import cli_agent_orchestrator.chatgpt_web_runner.runtime as runtime
    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import SendIntentLog
    from cli_agent_orchestrator.chatgpt_web_runner.source_pull import PAIRING_CODE_FILENAME

    port = _free_port()
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("CHATGPT_PULL_BIND_PORT", str(port))
    monkeypatch.setenv("CHATGPT_PULL_PUBLIC_BASE_URL", f"http://127.0.0.1:{port}")

    captured: dict[str, object] = {}

    async def _launch(_options: object) -> object:
        attempt_dir = tmp_path / "attempts" / "pairing-ruling"
        captured["raw_json"] = (attempt_dir / "send_intent.json").read_text(encoding="utf-8")
        code_path = attempt_dir / PAIRING_CODE_FILENAME
        captured["code"] = code_path.read_text(encoding="utf-8").strip()
        captured["mode"] = stat.S_IMODE(code_path.stat().st_mode)
        raise RuntimeError("stop after the plane is up")

    monkeypatch.setattr(runtime, "launch", _launch, raising=False)
    monkeypatch.setattr(runtime, "resolve_profile_dir", lambda: "/data/fake/profile", raising=False)
    monkeypatch.setattr(runtime, "pin_fingerprint_seed", lambda _p: "epoch", raising=False)

    log = _attempt(tmp_path, "pairing-ruling")
    with pytest.raises(RuntimeError, match="stop after the plane is up"):
        asyncio.run(_drive(log, "pairing-ruling", workspace))

    code = str(captured["code"])
    assert code, "the operator's copy must exist while the code is live"
    # Owner-only, at creation, with no wider window (O_CREAT with mode 0600).
    assert captured["mode"] == 0o600, oct(int(captured["mode"]))

    # The RAW code appears nowhere in the durable record's bytes.
    raw_json = str(captured["raw_json"])
    assert code not in raw_json, "the raw pairing code leaked into send_intent.json"
    # Not even with the formatting stripped, in case a future format changes it.
    assert code.replace("-", "") not in raw_json.replace("-", "")

    # What the record DOES carry proves which code it was.
    record = SendIntentLog(tmp_path / "attempts" / "pairing-ruling").load()
    assert record is not None
    assert record.connector_pairing_code_sha256 == hashlib.sha256(code.encode()).hexdigest()
    assert record.connector_pairing_issued_at is not None
    assert record.connector_pairing_expires_at is not None


def test_the_code_file_is_removed_once_the_pairing_is_consumed(tmp_path, workspace: Path) -> None:
    """A consumed code left on disk is a credential nobody is watching."""
    from cli_agent_orchestrator.chatgpt_web_runner.source_pull import PairingCodeFile

    server = bind_attempt(
        attempt_id="pairing-consume",
        frozen_worktree=workspace,
        manifest=["alpha.py"],
        state_dir=workspace / ".s-consume",
        public_base_url="http://127.0.0.1:1",
    )
    created = server.pairing.create()
    code = str(created["code"])
    handle = PairingCodeFile(tmp_path / "attempt", server.pairing)
    digest = handle.write(code)
    assert handle.path.exists() and digest

    async def _run() -> bool:
        handle.start_watch(interval=0.02)
        await asyncio.sleep(0.05)
        assert handle.path.exists(), "still live, so the operator can still read it"
        # A client pairs: the session is consumed.
        assert server.pairing.verify(code)["ok"] is True
        for _ in range(100):
            if not handle.path.exists():
                return True
            await asyncio.sleep(0.02)
        return False

    assert asyncio.run(_run()) is True, "the code file outlived its pairing session"


def test_the_code_file_is_removed_once_the_pairing_expires(tmp_path, workspace: Path) -> None:
    """Expiry is the other way a code stops being useful."""
    from cli_agent_orchestrator.chatgpt_web_runner.source_pull import PairingCodeFile
    from cli_agent_orchestrator.workspace_connector.pairing import PairingManager

    # The real manager, with the TTL its constructor already exposes.
    pairing = PairingManager(workspace_id="expiry", ttl_s=0.05)
    code = str(pairing.create()["code"])
    handle = PairingCodeFile(tmp_path / "attempt", pairing)
    handle.write(code)
    assert handle.path.exists()

    async def _run() -> bool:
        handle.start_watch(interval=0.02)
        for _ in range(100):
            if not handle.path.exists():
                return True
            await asyncio.sleep(0.02)
        return False

    assert asyncio.run(_run()) is True, "an expired code file was left behind"


def test_revoke_is_idempotent_and_safe_in_teardown(tmp_path, workspace: Path) -> None:
    from cli_agent_orchestrator.chatgpt_web_runner.source_pull import PairingCodeFile

    server = bind_attempt(
        attempt_id="pairing-revoke",
        frozen_worktree=workspace,
        manifest=["alpha.py"],
        state_dir=workspace / ".s-revoke",
        public_base_url="http://127.0.0.1:1",
    )
    handle = PairingCodeFile(tmp_path / "attempt", server.pairing)
    handle.write("ABCD-EFGH")
    handle.revoke()
    handle.revoke()  # must not raise
    assert not handle.path.exists()
