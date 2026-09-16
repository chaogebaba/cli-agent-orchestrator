"""The runner's worker identity and callback target (live-turn blocker 2).

The live-turn prep lane had a seat and a worker and no way to connect them: it
could not mint `identity.env` without creating a `chatgpt_web` terminal, whose
`initialize()` opens the logged-in profile — barred by its rules — and the
runner's callback read only `CAO_CALLBACK_TERMINAL_ID`, which is injected ONLY
on the cross-node path. A locally created worker therefore had a perfectly good
`caller_id` on its terminal row and still refused to call back.

These arms prove the product path offline. **Nothing here touches the ChatGPT
profile**: the identity is a property of the terminal row and its environment,
not of the provider, so a mock provider is the honest way to test it.
"""

from __future__ import annotations

from typing import Any

import pytest

from cli_agent_orchestrator.chatgpt_web_runner import production

pytestmark = pytest.mark.unit

ENDPOINT = "http://127.0.0.1:9889"
WORKER = "worker-abc"
SEAT = "seat-xyz"


class _Response:
    def __init__(self, ok: bool, payload: Any = None) -> None:
        self.ok = ok
        self._payload = payload if payload is not None else {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError("http error")


class _FakeHttp:
    """Stands in for the CAO REST API. Records what the runner asked for."""

    def __init__(self, *, callback_target: Any = None, terminal_row: Any = None) -> None:
        self.callback_target = callback_target
        self.terminal_row = terminal_row
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, url: str, **_kwargs: Any) -> _Response:
        self.gets.append(url)
        if url.endswith("/callback-target"):
            if self.callback_target is None:
                return _Response(False)
            return _Response(True, self.callback_target)
        if self.terminal_row is None:
            return _Response(False)
        return _Response(True, self.terminal_row)

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append((url, kwargs))
        return _Response(True)


@pytest.fixture()
def clean_env(monkeypatch):
    for name in (
        "CAO_CALLBACK_TERMINAL_ID",
        "CAO_CALLER_ID",
        "CAO_TERMINAL_ID",
        "CAO_TERMINAL_TOKEN",
        "CAO_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# =====================================================================
# Resolving the recorded caller
# =====================================================================


def test_the_cross_node_env_var_still_wins(clean_env, monkeypatch) -> None:
    """Unchanged behaviour for a remote worker: no lookup at all."""
    monkeypatch.setenv("CAO_CALLBACK_TERMINAL_ID", "remote-seat")
    http = _FakeHttp()
    monkeypatch.setattr(production, "requests", http, raising=False)
    assert production.resolve_callback_target(ENDPOINT, WORKER, {}) == "remote-seat"


def test_a_local_worker_resolves_its_caller_from_the_callback_target(
    clean_env, monkeypatch
) -> None:
    """The gap: no env var, a real caller_id, and it must be found.

    This is what `assign` records and what the REST create records when
    `caller_id` is passed — see the AC-15 smoke recipe.
    """
    http = _FakeHttp(callback_target={"receiver_id": SEAT})
    monkeypatch.setitem(__import__("sys").modules, "requests", http)
    monkeypatch.setattr(production, "requests", http, raising=False)
    import cli_agent_orchestrator.chatgpt_web_runner.production as prod

    monkeypatch.setattr(prod, "requests", http, raising=False)
    received = _resolve_with(http, prod, WORKER)
    assert received == SEAT
    assert any(url.endswith(f"/terminals/{WORKER}/callback-target") for url in http.gets)


def test_it_falls_back_to_the_terminal_rows_caller_id(clean_env, monkeypatch) -> None:
    """Older rows, and nodes without the callback-target route."""
    http = _FakeHttp(callback_target=None, terminal_row={"caller_id": SEAT})
    import cli_agent_orchestrator.chatgpt_web_runner.production as prod

    assert _resolve_with(http, prod, WORKER) == SEAT


def test_the_conversation_root_owner_wins_over_the_row(clean_env, monkeypatch) -> None:
    """F829: a RESUMED worker replies to the ORIGINAL caller."""
    http = _FakeHttp(
        callback_target={"receiver_id": "original-seat"},
        terminal_row={"caller_id": "recovering-seat"},
    )
    import cli_agent_orchestrator.chatgpt_web_runner.production as prod

    assert _resolve_with(http, prod, WORKER) == "original-seat"


def test_no_recorded_caller_anywhere_is_an_empty_result(clean_env) -> None:
    http = _FakeHttp(callback_target=None, terminal_row=None)
    import cli_agent_orchestrator.chatgpt_web_runner.production as prod

    assert _resolve_with(http, prod, WORKER) == ""


def test_an_unset_terminal_id_short_circuits(clean_env) -> None:
    """No identity at all: do not call the API with an empty id."""
    http = _FakeHttp(callback_target={"receiver_id": SEAT})
    import cli_agent_orchestrator.chatgpt_web_runner.production as prod

    assert _resolve_with(http, prod, "") == ""
    assert http.gets == []


def _resolve_with(http: _FakeHttp, prod: Any, terminal_id: str) -> str:
    """Run the resolver with `requests` swapped for the fake, then restore."""
    import sys

    real = sys.modules.get("requests")
    sys.modules["requests"] = http  # type: ignore[assignment]
    try:
        return prod.resolve_callback_target(ENDPOINT, terminal_id, {})
    finally:
        if real is not None:
            sys.modules["requests"] = real


# =====================================================================
# The callback the runner actually sends
# =====================================================================


def test_the_callback_posts_to_the_resolved_caller_as_the_worker(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("CAO_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("CAO_TERMINAL_ID", WORKER)
    monkeypatch.setenv("CAO_TERMINAL_TOKEN", "tok-123")
    http = _FakeHttp(callback_target={"receiver_id": SEAT})

    import sys

    real = sys.modules.get("requests")
    sys.modules["requests"] = http  # type: ignore[assignment]
    try:
        production._callback_as_worker("F862 FINDINGS-READY /p.md sha256=abc")
    finally:
        if real is not None:
            sys.modules["requests"] = real

    assert len(http.posts) == 1
    url, kwargs = http.posts[0]
    assert url == f"{ENDPOINT}/terminals/{SEAT}/inbox/messages"
    # The worker is the SENDER and authenticates as itself (D2 identity).
    assert kwargs["params"]["sender_id"] == WORKER
    assert kwargs["headers"]["X-CAO-Terminal-Token"] == "tok-123"


def test_the_callback_refusal_names_what_it_looked_for(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("CAO_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("CAO_TERMINAL_ID", WORKER)
    http = _FakeHttp(callback_target=None, terminal_row=None)

    import sys

    from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError

    real = sys.modules.get("requests")
    sys.modules["requests"] = http  # type: ignore[assignment]
    try:
        with pytest.raises(RunnerError) as excinfo:
            production._callback_as_worker("anything")
    finally:
        if real is not None:
            sys.modules["requests"] = real
    hint = str(excinfo.value.hint)
    assert WORKER in hint
    assert "CAO_CALLBACK_TERMINAL_ID" in hint and "caller_id" in hint


# =====================================================================
# The terminal-creation recipe, with a MOCK provider
# =====================================================================


def test_a_mock_provider_terminal_carries_the_worker_identity(monkeypatch) -> None:
    """The env the runner reads is injected by the BACKEND at create time.

    CAO_TERMINAL_ID / CAO_SESSION_NAME / CAO_TERMINAL_TOKEN are assigned last so
    operator-forwarded --env cannot override them. This asserts that contract
    against the real env-building code path with a mock provider, so no
    chatgpt_web initialize() runs and the ChatGPT profile is never opened.
    """
    from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

    build = getattr(HerdrBackend, "_build_env_args", None)
    if build is None:  # pragma: no cover - keeps the arm honest if it is renamed
        pytest.skip("HerdrBackend._build_env_args not present under that name")

    args = build(
        HerdrBackend.__new__(HerdrBackend),
        WORKER,
        "f862-live",
        {"CAO_TERMINAL_ID": "spoofed", "SOME_OTHER": "kept"},
        terminal_token="tok-abc",
    )
    flat = dict(pair.split("=", 1) for pair in args[1::2])
    assert flat["CAO_TERMINAL_ID"] == WORKER, "identity must win over forwarded env"
    assert flat["CAO_SESSION_NAME"] == "f862-live"
    assert flat["CAO_TERMINAL_TOKEN"] == "tok-abc"
