"""D9.1 — the operator gate and the durable connector authorization.

The pre-flight found the live turn could not complete the pull plane. Two causes,
both structural:

* **Nothing waited for the human.** After printing the pairing code the runner
  spent 5-15 seconds on launch, navigation, the composer wait and the mint — on
  the very page the operator would have to navigate away from to reach Settings.
  The code lived 300 s; the window in which it could be typed was zero.
* **Every attempt started from an empty auth store.** ``bind_attempt`` was given
  ``attempts/<id>/connector``, so a refresh token ChatGPT had kept found no
  record and failed ``invalid_grant``. Pairing could not be done once and reused.

These arms drive the real gate and the real store. Offline: no browser, no
chatgpt.com, no profile.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from cli_agent_orchestrator.chatgpt_web_runner import production
from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError, RunnerErrorCode
from cli_agent_orchestrator.chatgpt_web_runner.send_intent import AttemptState, SendIntentLog
from cli_agent_orchestrator.chatgpt_web_runner.source_pull import (
    PairingExpired,
    await_pairing_consumed,
    connector_auth_dir,
)
from cli_agent_orchestrator.services.workspace_read import bind_attempt
from cli_agent_orchestrator.workspace_connector.pairing import PairingManager

pytestmark = pytest.mark.unit


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "alpha.py").write_text("canary-alpha\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("canary-beta\n", encoding="utf-8")
    return tmp_path


def _attempt(tmp_path: Path, attempt_id: str) -> SendIntentLog:
    from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import get_relay_hub
    from cli_agent_orchestrator.providers.chatgpt_web import ChatGptWebProvider

    get_relay_hub().reset_for_tests()
    ChatGptWebProvider.start_attempt(
        run_id="run-gate", attempt_id=attempt_id, prompt_sha="0" * 64, artifacts_dir=tmp_path
    )
    log = SendIntentLog(tmp_path / "attempts" / attempt_id)
    log.load()
    get_relay_hub().get(attempt_id).intent_log = log
    return log


def _drive(log: SendIntentLog, attempt_id: str, workspace: Path):
    return production._drive_composed_turn(
        task_text="review it",
        run_id="run-gate",
        attempt_id=attempt_id,
        prompt_sha="0" * 64,
        intent_log=log,
        manifest=["alpha.py", "beta.py"],
        frozen_worktree=str(workspace),
        reviewed_commit=None,
        base_commit=None,
        pull_evidence={},
    )


class _GatePage:
    """A blank page that records when it was navigated — never before the gate."""

    def __init__(self, events: list) -> None:
        self._events = events
        self.keyboard = None

    def locator(self, _selector):
        raise AssertionError("the composer was reached before the gate opened")

    def on(self, _event, _handler):
        return None

    async def route(self, _pattern, _handler):
        self._events.append(("route", time.monotonic()))

    async def goto(self, _url, **_kwargs):
        self._events.append(("goto", time.monotonic()))
        raise RuntimeError("stop the turn at the navigation boundary")


class _GateContext:
    def __init__(self, events: list) -> None:
        self.pages = [_GatePage(events)]
        self.closed = False

    def on(self, _event, _handler):
        return None

    async def new_page(self):
        return self.pages[0]

    async def close(self):
        self.closed = True


def _stub_browser(monkeypatch, events: list):
    """Launch succeeds and records when; navigation records and then stops.

    D9.2 puts the gate between them, so this is the shape that can assert the
    order: launch, then the wait, then goto — and never goto first.
    """
    import cli_agent_orchestrator.chatgpt_web_runner.runtime as runtime

    contexts: list = []

    async def _launch(_options: object) -> object:
        events.append(("launch", time.monotonic()))
        context = _GateContext(events)
        contexts.append(context)
        return context

    monkeypatch.setattr(runtime, "launch", _launch, raising=False)
    monkeypatch.setattr(runtime, "resolve_profile_dir", lambda: "/data/fake/profile", raising=False)
    monkeypatch.setattr(runtime, "pin_fingerprint_seed", lambda _p: "epoch", raising=False)
    return contexts


def _configure(monkeypatch, tmp_path: Path, port: int) -> str:
    public = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("CHATGPT_PULL_BIND_PORT", str(port))
    monkeypatch.setenv("CHATGPT_PULL_PUBLIC_BASE_URL", public)
    return public


# =====================================================================
# The gate itself, on the REAL timing path
#
# The arm this replaces passed an already-past deadline while the pairing
# session was still live. Production can never produce that combination — the
# deadline IS the session's TTL — so it exercised the deadline branch while
# leaving the liveness branch, the one that actually ran at 300 s, untested. The
# gate failed open there and the arm stayed green. Everything below drives a real
# clock against a real, short TTL.
# =====================================================================


def _short_pairing(ttl: float):
    """A real PairingManager whose code really expires, quickly."""
    pairing = PairingManager(workspace_id="w", ttl_s=ttl)
    created = pairing.create()
    return pairing, str(created["code"]), str(created["session_id"]), float(created["expires_at"])


def test_the_gate_raises_when_the_code_really_expires() -> None:
    """The pre-flight-2 failure, reproduced on the real path.

    At the true deadline the session is gone — `verify()` pops it on expiry just
    as it does on redemption — so the old liveness test said "not active" and the
    gate announced success. Here nobody pairs and the deadline genuinely passes.
    """
    pairing, _code, session_id, expires_at = _short_pairing(0.3)
    lines: list[str] = []

    async def _run() -> float:
        return await await_pairing_consumed(
            pairing,
            session_id=session_id,
            expires_at=expires_at,
            announce=lines.append,
            poll_interval=0.02,
            countdown_interval=0.05,
        )

    with pytest.raises(PairingExpired):
        asyncio.run(_run())
    # And it really did wait, rather than failing instantly for another reason.
    assert pairing.has_active_session() is False
    assert pairing.was_consumed(session_id) is False


def test_the_gate_returns_only_on_a_real_redemption() -> None:
    pairing, code, session_id, expires_at = _short_pairing(5.0)
    lines: list[str] = []

    async def _run() -> float:
        async def _pair_soon() -> None:
            await asyncio.sleep(0.05)
            assert pairing.verify(code)["ok"] is True

        asyncio.ensure_future(_pair_soon())
        return await await_pairing_consumed(
            pairing,
            session_id=session_id,
            expires_at=expires_at,
            announce=lines.append,
            poll_interval=0.02,
            countdown_interval=0.03,
        )

    waited = asyncio.run(_run())
    assert waited >= 0
    assert pairing.was_consumed(session_id) is True
    assert any(line.startswith("PULL-PAIRING-WAIT") for line in lines), lines


def test_an_invalidated_pairing_is_not_an_authorization() -> None:
    """`has_active_session()` is false here too — and that is not consent."""
    pairing, _code, session_id, expires_at = _short_pairing(0.3)
    pairing.invalidate_all()
    assert pairing.has_active_session() is False

    async def _run() -> float:
        return await await_pairing_consumed(
            pairing,
            session_id=session_id,
            expires_at=expires_at,
            announce=lambda _l: None,
            poll_interval=0.02,
        )

    with pytest.raises(PairingExpired):
        asyncio.run(_run())


def test_a_redemption_observed_after_the_deadline_still_counts() -> None:
    """Consumption is checked before expiry, on purpose.

    A code redeemed at 299 s but first observed at 301 s is an authorization the
    operator completed; refusing it would throw the turn away. Only an expired
    AND unredeemed pairing raises.
    """
    pairing, code, session_id, _expires = _short_pairing(5.0)
    assert pairing.verify(code)["ok"] is True

    async def _run() -> float:
        return await await_pairing_consumed(
            pairing,
            session_id=session_id,
            expires_at=time.time() - 1,  # already past
            announce=lambda _l: None,
            poll_interval=0.02,
        )

    assert asyncio.run(_run()) >= 0


def test_a_refresh_token_in_the_store_also_opens_the_gate(tmp_path, workspace: Path) -> None:
    """The stronger corroboration: the exchange completed and a token exists."""
    pairing, _code, session_id, expires_at = _short_pairing(5.0)
    server = bind_attempt(
        attempt_id="store-evidence",
        frozen_worktree=workspace,
        manifest=["alpha.py"],
        state_dir=connector_auth_dir(tmp_path, "https://front.example"),
        public_base_url="https://front.example",
    )
    server.store.issue_tokens(client_id="chatgpt", scopes=["workspace.read", "offline_access"])

    async def _run() -> float:
        return await await_pairing_consumed(
            pairing,
            session_id=session_id,
            expires_at=expires_at,
            announce=lambda _l: None,
            store=server.store,
            poll_interval=0.02,
        )

    assert asyncio.run(_run()) >= 0
    assert pairing.was_consumed(session_id) is False, "it opened on the STORE, not the session"


# =====================================================================
# Through the composed turn — D9.2 ordering
# =====================================================================


def test_launch_then_wait_then_goto_in_that_order(tmp_path, monkeypatch, workspace: Path) -> None:
    """D9.2: the browser is up BEFORE the code is printed, and goto waits.

    Pairing happens inside this window, in a second tab of the driven browser's
    own session — one profile instance on the burner account, which is what
    removes the proc_exited collision the operator card flagged.
    """
    import cli_agent_orchestrator.chatgpt_web_runner.source_pull as source_pull

    port = _free_port()
    _configure(monkeypatch, tmp_path, port)
    monkeypatch.setenv("CHATGPT_PULL_PAIRING_TTL_S", "5")
    events: list = []
    _stub_browser(monkeypatch, events)

    consumed_at: list[float] = []
    real_gate = source_pull.await_pairing_consumed

    async def _gate(pairing, **kwargs):
        async def _pair_soon() -> None:
            await asyncio.sleep(0.05)
            # A REAL redemption, not invalidate_all: absence is not consent.
            code = (tmp_path / "attempts" / "gate-order" / "pairing.code").read_text().strip()
            assert pairing.verify(code)["ok"] is True
            consumed_at.append(time.monotonic())

        asyncio.ensure_future(_pair_soon())
        return await real_gate(pairing, **{**kwargs, "poll_interval": 0.02})

    monkeypatch.setattr(source_pull, "await_pairing_consumed", _gate, raising=False)

    log = _attempt(tmp_path, "gate-order")
    with pytest.raises(RuntimeError, match="stop the turn at the navigation boundary"):
        asyncio.run(_drive(log, "gate-order", workspace))

    kinds = [kind for kind, _at in events]
    assert kinds[0] == "launch", f"the browser must be up before the gate: {kinds}"
    assert "goto" in kinds, "the turn never navigated"
    assert consumed_at, "the pairing was never redeemed"
    launch_at = next(at for kind, at in events if kind == "launch")
    goto_at = next(at for kind, at in events if kind == "goto")
    # ORDER, not occurrence.
    assert launch_at < consumed_at[0] < goto_at


def test_an_expired_pairing_after_launch_closes_the_browser_and_spends_nothing(
    tmp_path, monkeypatch, workspace: Path
) -> None:
    """Expiry on the REAL clock: a short TTL and nobody pairs."""
    from cli_agent_orchestrator.chatgpt_web_runner.source_pull import PAIRING_CODE_FILENAME

    port = _free_port()
    _configure(monkeypatch, tmp_path, port)
    # The real gate, the real clock, a deadline an arm can afford to wait out.
    monkeypatch.setenv("CHATGPT_PULL_PAIRING_TTL_S", "0.4")
    events: list = []
    contexts = _stub_browser(monkeypatch, events)

    log = _attempt(tmp_path, "gate-expired")
    with pytest.raises(RunnerError) as excinfo:
        asyncio.run(_drive(log, "gate-expired", workspace))

    assert excinfo.value.code is RunnerErrorCode.PAIRING_EXPIRED
    assert excinfo.value.delivery_state.value == "nothing-sent"

    kinds = [kind for kind, _at in events]
    assert "launch" in kinds, "the gate must sit after the launch (D9.2)"
    assert "goto" not in kinds, "the page navigated despite an expired pairing"

    # The browser the turn opened is closed by the teardown.
    assert contexts and contexts[0].closed is True

    record = SendIntentLog(tmp_path / "attempts" / "gate-expired").load()
    assert record is not None
    assert record.attempt_state == AttemptState.ERROR.value
    assert record.route_disposition == "pairing_expired"
    assert record.reserved_at is None and record.submits_dispatched == 0
    assert not (tmp_path / "attempts" / "gate-expired" / PAIRING_CODE_FILENAME).exists()


# =====================================================================
# The durable store
# =====================================================================


def test_the_auth_dir_is_keyed_by_connector_identity_and_is_0700(tmp_path) -> None:
    import stat

    a = connector_auth_dir(tmp_path, "https://front.example:8443")
    again = connector_auth_dir(tmp_path, "https://front.example:8443")
    other = connector_auth_dir(tmp_path, "https://elsewhere.example:8443")

    assert a == again, "the same connector must reuse one store"
    assert a != other, "different connectors must not share one"
    assert "attempts" not in str(a), "the store must not live under an attempt"
    assert stat.S_IMODE(a.stat().st_mode) == 0o700


def test_a_stored_refresh_token_means_no_pairing_is_issued(
    tmp_path, monkeypatch, workspace: Path
) -> None:
    """Second attempt, same connector: the operator is not asked again."""
    import cli_agent_orchestrator.chatgpt_web_runner.source_pull as source_pull
    from cli_agent_orchestrator.chatgpt_web_runner.source_pull import PAIRING_CODE_FILENAME

    port = _free_port()
    public = _configure(monkeypatch, tmp_path, port)

    # Seed the DURABLE store the way a completed pairing would have.
    seed = bind_attempt(
        attempt_id="earlier-attempt",
        frozen_worktree=workspace,
        manifest=["alpha.py", "beta.py"],
        state_dir=connector_auth_dir(tmp_path, public),
        public_base_url=public,
    )
    seed.store.issue_tokens(client_id="chatgpt", scopes=["workspace.read", "offline_access"])
    assert seed.store.has_reusable_authorization() is True

    gate_calls: list[object] = []

    async def _gate(pairing, **kwargs):
        gate_calls.append(pairing)
        return 0.0

    monkeypatch.setattr(source_pull, "await_pairing_consumed", _gate, raising=False)
    events: list = []
    _stub_browser(monkeypatch, events)

    log = _attempt(tmp_path, "reuse")
    with pytest.raises(RuntimeError, match="stop the turn at the navigation boundary"):
        asyncio.run(_drive(log, "reuse", workspace))

    record = SendIntentLog(tmp_path / "attempts" / "reuse").load()
    assert record is not None
    assert record.connector_auth_reused is True
    assert record.connector_pairing_code_sha256 is None, "a code was minted despite reuse"
    assert not (tmp_path / "attempts" / "reuse" / PAIRING_CODE_FILENAME).exists()
    assert gate_calls == [], "the operator was made to wait despite a reusable authorization"
    assert any(k == "launch" for k, _ in events), "the turn did not proceed to the browser"


def test_an_unusable_stored_token_issues_a_fresh_pairing(
    tmp_path, monkeypatch, workspace: Path
) -> None:
    """invalid_grant territory: an expired refresh token is not an authorization."""
    import cli_agent_orchestrator.chatgpt_web_runner.source_pull as source_pull

    port = _free_port()
    public = _configure(monkeypatch, tmp_path, port)

    seed = bind_attempt(
        attempt_id="earlier-attempt",
        frozen_worktree=workspace,
        manifest=["alpha.py", "beta.py"],
        state_dir=connector_auth_dir(tmp_path, public),
        public_base_url=public,
    )
    seed.store.issue_tokens(client_id="chatgpt", scopes=["workspace.read", "offline_access"])
    # Expire every stored token, the way a long gap between attempts would.
    for record in list(seed.store._tokens.values()):  # noqa: SLF001 - fixture surgery
        record.expires_at = time.time() - 1
    seed.store._save()  # noqa: SLF001
    assert seed.store.has_reusable_authorization() is False

    gate_calls: list[object] = []

    async def _gate(pairing, **kwargs):
        gate_calls.append(pairing)
        return 0.0

    monkeypatch.setattr(source_pull, "await_pairing_consumed", _gate, raising=False)
    _stub_browser(monkeypatch, [])

    log = _attempt(tmp_path, "stale")
    with pytest.raises(RuntimeError, match="stop the turn at the navigation boundary"):
        asyncio.run(_drive(log, "stale", workspace))

    record = SendIntentLog(tmp_path / "attempts" / "stale").load()
    assert record is not None
    assert record.connector_auth_reused is False
    assert record.connector_pairing_code_sha256, "no pairing was issued for a dead token"
    assert gate_calls, "a fresh pairing must go through the operator gate"


def test_a_refreshed_token_is_rebound_to_the_current_attempt(tmp_path, workspace: Path) -> None:
    """Why the durable store works at all.

    Refresh used to re-issue against the token's ORIGINAL attempt, so a reused
    store would hand out access tokens bound to a dead attempt and every read
    would 403 — the same wall as re-pairing, reached more slowly.
    """
    public = "https://front.example:8443"
    store_dir = connector_auth_dir(tmp_path, public)

    first = bind_attempt(
        attempt_id="attempt-one",
        frozen_worktree=workspace,
        manifest=["alpha.py"],
        state_dir=store_dir,
        public_base_url=public,
    )
    issued = first.store.issue_tokens(
        client_id="chatgpt", scopes=["workspace.read", "offline_access"]
    )
    refresh_token = str(issued["refresh_token"])

    second = bind_attempt(
        attempt_id="attempt-two",
        frozen_worktree=workspace,
        manifest=["alpha.py"],
        state_dir=store_dir,
        public_base_url=public,
    )
    assert second.store.has_reusable_authorization() is True

    ok, tokens, reason = second.store.refresh(refresh_token, "chatgpt")
    assert ok is True, reason
    assert tokens is not None

    verified_ok, record, why = second.store.verify_access_token(str(tokens["access_token"]))
    assert verified_ok is True, why
    assert record is not None
    assert record.attempt_id == "attempt-two", "the refreshed token is bound to the dead attempt"
