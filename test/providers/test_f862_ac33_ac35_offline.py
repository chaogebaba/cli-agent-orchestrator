"""AC-33 / AC-34 / AC-35 offline arms for the composed Amendment D path (B3).

AC-33 (source correlation and the one-generation invariant) is exercised against
a REAL loopback connector: a real listener, real Streamable-HTTP MCP calls over
real access tokens, and the connector's own audit projection as the verifier
input. Nothing about the correlation logic is stubbed, so each blocking mode is
proved to block rather than asserted to.

AC-34 (one context path) and AC-35 (no dual sender, no model endpoint) are
reachability arms: they fail if a deleted surface comes back. A kill list that is
only written down is a comment; these are what make it a property.

Offline by construction — no browser, no chatgpt.com, no logged-in profile.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.chatgpt_web_runner import production, source_pull
from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError, RunnerErrorCode
from cli_agent_orchestrator.chatgpt_web_runner.source_pull import (
    MINIMUM_PULLED_SOURCES,
    ConnectorListener,
    PulledSource,
    SourcePullEvidence,
    call_tool,
    collect_pull_evidence,
    verify_source_correlation,
)
from cli_agent_orchestrator.services.workspace_read import bind_attempt

pytestmark = pytest.mark.unit

BRANCH = "sha256:branch-digest"


# =====================================================================
# AC-33 — against a real loopback connector
# =====================================================================


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "alpha.py").write_text("canary-alpha\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("canary-beta\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("not in the manifest\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=nope", encoding="utf-8")
    return tmp_path


def _pull(workspace: Path, paths: list[str], *, attempt_id: str = "attempt-33") -> tuple:
    """Read ``paths`` through a real loopback connector on ONE access token."""
    server = bind_attempt(
        attempt_id=attempt_id,
        frozen_worktree=workspace,
        manifest=["alpha.py", "beta.py"],
        state_dir=workspace / f".state-{attempt_id}",
    )

    async def _run() -> list[dict]:
        async with ConnectorListener(server) as listener:
            token = server.store.issue_tokens(client_id="model", scopes=["workspace.read"])[
                "access_token"
            ]
            results = []
            for index, path in enumerate(paths):
                results.append(
                    await asyncio.to_thread(
                        call_tool,
                        listener.base_url,
                        token,
                        "workspace_read_file",
                        {"path": path},
                        request_id=index + 1,
                    )
                )
            return results

    responses = asyncio.run(_run())
    from cli_agent_orchestrator.api.routes_chatgpt_web_connector import connector_audit_projection

    return responses, connector_audit_projection(server), server


def test_two_canary_sources_on_one_token_correlate_and_publish(workspace: Path) -> None:
    """The success path: two sources, one token, digests carried by the answer."""
    responses, rows, _ = _pull(workspace, ["alpha.py", "beta.py"])
    assert all(r["result"]["isError"] is False for r in responses), responses

    evidence = collect_pull_evidence(rows)
    assert len(evidence.sources) == 2
    # The one-generation invariant is observable at all only because each audit
    # row names the token that produced it.
    assert len(evidence.token_fingerprints) == 1
    assert all(source.token_fingerprint for source in evidence.sources)

    answer = "Findings. Sources: " + " ".join(evidence.digests)
    correlated = verify_source_correlation(
        evidence,
        answer_text=answer,
        observed_branch_digest=BRANCH,
        accepted_branch_digest=BRANCH,
    )
    assert {source.path for source in correlated} == {"alpha.py", "beta.py"}


def test_one_source_is_not_enough(workspace: Path) -> None:
    """AC-33 requires at least two canary-bearing files."""
    _, rows, _ = _pull(workspace, ["alpha.py"], attempt_id="attempt-one")
    evidence = collect_pull_evidence(rows)
    with pytest.raises(RunnerError) as excinfo:
        verify_source_correlation(
            evidence,
            answer_text=" ".join(evidence.digests),
            observed_branch_digest=BRANCH,
            accepted_branch_digest=BRANCH,
        )
    assert excinfo.value.code is RunnerErrorCode.SOURCE_CORRELATION
    assert f"at least {MINIMUM_PULLED_SOURCES}" in str(excinfo.value)


def test_a_denied_source_blocks_publication(workspace: Path) -> None:
    """A real PATH_NOT_IN_MANIFEST refusal, produced by the connector itself."""
    responses, rows, _ = _pull(
        workspace, ["alpha.py", "beta.py", "outside.py"], attempt_id="attempt-denied"
    )
    # The connector refused the third read on its own authority.
    assert "PATH_NOT_IN_MANIFEST" in json.dumps(responses[-1])

    evidence = collect_pull_evidence(rows)
    assert evidence.refusals, "the refusal must reach the projection"
    with pytest.raises(RunnerError) as excinfo:
        verify_source_correlation(
            evidence,
            answer_text=" ".join(evidence.digests),
            observed_branch_digest=BRANCH,
            accepted_branch_digest=BRANCH,
        )
    assert excinfo.value.code is RunnerErrorCode.SOURCE_CORRELATION
    assert "refused a source read" in str(excinfo.value)


def test_a_sensitive_file_is_a_denial_not_a_source(workspace: Path) -> None:
    """`.env` is refused, so it can never be counted toward the AC-33 minimum."""
    _, rows, _ = _pull(workspace, ["alpha.py", ".env"], attempt_id="attempt-sensitive")
    evidence = collect_pull_evidence(rows)
    assert len(evidence.sources) == 1
    assert any(
        row["refusal_code"] in {"ACCESS_DENIED_SENSITIVE_FILE", "PATH_NOT_IN_MANIFEST"}
        for row in evidence.refusals
    )


def test_missing_audit_blocks_publication() -> None:
    """An answer that cites sources the connector never served does not publish."""
    evidence = collect_pull_evidence([])
    with pytest.raises(RunnerError) as excinfo:
        verify_source_correlation(
            evidence,
            answer_text="Findings citing sha256:whatever",
            observed_branch_digest=BRANCH,
            accepted_branch_digest=BRANCH,
        )
    assert "records no source read" in str(excinfo.value)


def test_a_substituted_digest_blocks_publication(workspace: Path) -> None:
    """The answer must carry the EXACT digest the connector returned."""
    _, rows, _ = _pull(workspace, ["alpha.py", "beta.py"], attempt_id="attempt-sub")
    evidence = collect_pull_evidence(rows)
    substituted = " ".join(digest[:-4] + "dead" for digest in evidence.digests)
    with pytest.raises(RunnerError) as excinfo:
        verify_source_correlation(
            evidence,
            answer_text=f"Findings. Sources: {substituted}",
            observed_branch_digest=BRANCH,
            accepted_branch_digest=BRANCH,
        )
    assert "does not carry the exact connector result digest" in str(excinfo.value)


def test_a_second_access_token_breaks_the_one_generation_invariant(workspace: Path) -> None:
    """Two tokens mean two generations; AC-33 admits exactly one."""
    server = bind_attempt(
        attempt_id="attempt-two-tokens",
        frozen_worktree=workspace,
        manifest=["alpha.py", "beta.py"],
        state_dir=workspace / ".state-two",
    )

    async def _run() -> None:
        async with ConnectorListener(server) as listener:
            for index, path in enumerate(["alpha.py", "beta.py"]):
                # A FRESH token per read — the thing the invariant forbids.
                token = server.store.issue_tokens(
                    client_id=f"model-{index}", scopes=["workspace.read"]
                )["access_token"]
                await asyncio.to_thread(
                    call_tool,
                    listener.base_url,
                    token,
                    "workspace_read_file",
                    {"path": path},
                    request_id=index + 1,
                )

    asyncio.run(_run())
    from cli_agent_orchestrator.api.routes_chatgpt_web_connector import connector_audit_projection

    evidence = collect_pull_evidence(connector_audit_projection(server))
    assert len(evidence.token_fingerprints) == 2
    with pytest.raises(RunnerError) as excinfo:
        verify_source_correlation(
            evidence,
            answer_text=" ".join(evidence.digests),
            observed_branch_digest=BRANCH,
            accepted_branch_digest=BRANCH,
        )
    assert "one reusable access token" in str(excinfo.value)


def test_the_wrong_branch_blocks_publication() -> None:
    """A correlation against a branch the GET did not accept is worthless."""
    evidence = SourcePullEvidence(
        attempt_id="a",
        sources=(
            PulledSource("alpha.py", "sha256:aa", "fp"),
            PulledSource("beta.py", "sha256:bb", "fp"),
        ),
        refusals=(),
        token_fingerprints=("fp",),
    )
    with pytest.raises(RunnerError) as excinfo:
        verify_source_correlation(
            evidence,
            answer_text="sha256:aa sha256:bb",
            observed_branch_digest="sha256:other-branch",
            accepted_branch_digest=BRANCH,
        )
    assert "branch digest mismatch" in str(excinfo.value)


def test_an_absent_branch_digest_blocks_publication() -> None:
    evidence = SourcePullEvidence(
        attempt_id="a",
        sources=(PulledSource("alpha.py", "sha256:aa", "fp"),),
        refusals=(),
        token_fingerprints=("fp",),
    )
    with pytest.raises(RunnerError) as excinfo:
        verify_source_correlation(
            evidence,
            answer_text="sha256:aa",
            observed_branch_digest="",
            accepted_branch_digest="",
        )
    assert "no canonical branch digest" in str(excinfo.value)


def test_workspace_info_alone_cannot_satisfy_the_minimum(workspace: Path) -> None:
    """The summary tool returns no file bytes, so it is not a SOURCE read."""
    evidence = collect_pull_evidence(
        [
            {
                "attempt_id": "a",
                "tool": "workspace_info",
                "subject": "workspace:/",
                "result_digest": "sha256:info",
                "refusal_code": None,
                "token_fingerprint": "fp",
            },
            {
                "attempt_id": "a",
                "tool": "workspace_list_directory",
                "subject": ".",
                "result_digest": "sha256:list",
                "refusal_code": None,
                "token_fingerprint": "fp",
            },
        ]
    )
    assert evidence.sources == ()


def test_the_listener_is_separately_killable(workspace: Path) -> None:
    """The pull plane comes down on its own, and stop() is idempotent."""
    import socket as _socket
    import urllib.request

    server = bind_attempt(
        attempt_id="attempt-kill",
        frozen_worktree=workspace,
        manifest=["alpha.py"],
        state_dir=workspace / ".state-kill",
    )

    def _health(base: str) -> int:
        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            return int(response.status)

    async def _run() -> str:
        listener = ConnectorListener(server)
        base = await listener.start()
        # In a worker thread: a blocking urlopen on the loop that SERVES the
        # request would deadlock against the listener it is probing.
        assert await asyncio.to_thread(_health, base) == 200
        await listener.stop()
        await listener.stop()  # idempotent
        return base

    base = asyncio.run(_run())
    host = base.split("//", 1)[1].rsplit(":", 1)[0]
    port = int(base.rsplit(":", 1)[1])
    with _socket.socket() as probe:
        probe.settimeout(2)
        assert probe.connect_ex((host, port)) != 0, "the listener outlived its attempt"


# =====================================================================
# AC-34 — one context path: the push surfaces are GONE, not merely unused
# =====================================================================

_DELETED_UPLOAD_SYMBOLS = [
    ("snapshot_upload", "build_attachment_identity"),
    ("snapshot_upload", "AttachmentIdentity"),
    ("snapshot_upload", "enforce_bundle_bounds"),
    ("snapshot_upload", "verify_attachment_on_turn"),
    ("snapshot_upload", "readiness_reached"),
    ("snapshot_upload", "MAX_BUNDLE_BYTES"),
    ("snapshot_upload", "MAX_BUNDLE_LINES"),
    ("in_page_transport", "SEL_FILE_INPUT"),
    ("in_page_transport", "SEL_PLUS_BTN"),
    ("in_page_transport", "build_conversation_fetch_script"),
    ("in_page_transport", "SubmitOutcome"),
]


@pytest.mark.parametrize(("module_name", "symbol"), _DELETED_UPLOAD_SYMBOLS)
def test_ac34_deleted_symbol_is_not_importable(module_name: str, symbol: str) -> None:
    import importlib

    module = importlib.import_module(f"cli_agent_orchestrator.chatgpt_web_runner.{module_name}")
    assert not hasattr(module, symbol), f"{module_name}.{symbol} came back (D10 kill list)"


_DELETED_TRANSPORT_METHODS = [
    "attach_file",
    "_wait_upload_complete",
    "summarize_readiness_probe",
    "_record_readiness_probe",
    "_record_stall_dom",
    "read_conversation",
    "_recover_and_redispatch",
    "submit_and_confirm",
    "poll_to_gate",
    "arm_send_observer",
]


@pytest.mark.parametrize("method", _DELETED_TRANSPORT_METHODS)
def test_ac34_deleted_transport_method_is_gone(method: str) -> None:
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import Transport

    assert not hasattr(Transport, method), f"Transport.{method} came back (D10 kill list)"


def test_ac34_the_shape_a_live_spike_module_is_gone() -> None:
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("cli_agent_orchestrator.chatgpt_web_runner.live_spike")


def test_ac34_production_takes_no_bundle_path() -> None:
    """The findings run has no attachment parameter to pass."""
    import inspect

    parameters = inspect.signature(production.run_production_review).parameters
    assert "bundle_path" not in parameters
    assert "source_manifest" in parameters


def test_ac34_the_dispatch_header_has_no_bundle_line() -> None:
    from cli_agent_orchestrator.chatgpt_web_runner.__main__ import _parse_task

    task = _parse_task("ARTIFACT: /a.md\nSOURCE: x.py\nBUNDLE: /b.txt\n\nreview")
    assert task.source_manifest == ("x.py",)
    # BUNDLE is no longer a recognised header, so it ends the header block and
    # falls into the body: it can never become an upload again.
    assert task.prompt.startswith("BUNDLE: /b.txt")
    assert not hasattr(task, "bundle_path")


def test_ac34_report_validation_no_longer_depends_on_an_upload(tmp_path, monkeypatch) -> None:
    """Deleting the bundle must not silently disable schema validation."""
    from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import (
        REQUIRED_MODEL_SLUG,
        REQUIRED_THINKING_EFFORT,
        AcceptedAnswer,
    )

    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    outcome = production.run_production_review(
        task_text="review it",
        artifact_path="/abs/pin.md",
        verify_pin=lambda path: True,
        callback=lambda message: None,
        browser_turn=lambda: AcceptedAnswer(
            text="Finding 1: no old quote, no replace.",
            model_slug=REQUIRED_MODEL_SLUG,
            thinking_effort=REQUIRED_THINKING_EFFORT,
            conversation_id="11111111-2222-3333-4444-555555555555",
            assistant_node_id="asst-1",
        ),
    )
    assert outcome.ok is False
    assert outcome.error_code is not None and outcome.error_code.value == "report_invalid"


# =====================================================================
# AC-35 — no dual sender, no model endpoint, no automatic fallback
# =====================================================================


def test_ac35_no_openai_compatible_surface_exists_in_the_runner() -> None:
    """No /v1 route, no OPENAI_BASE_URL guidance, no protocol translation."""
    root = Path(production.__file__).parent
    forbidden = ("/v1/chat/completions", "/v1/models", "OPENAI_BASE_URL")
    offenders = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders


def test_ac35_the_platform_api_host_is_denied_at_request_time() -> None:
    from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import enforce_no_api_egress

    with pytest.raises(RunnerError) as excinfo:
        enforce_no_api_egress("https://api.openai.com/v1/chat/completions")
    assert excinfo.value.code is RunnerErrorCode.EGRESS_FORBIDDEN


def test_ac35_the_sender_refuses_an_unvalidated_impersonation_posture() -> None:
    """A silently downgraded TLS template must fail BEFORE the network."""
    from cli_agent_orchestrator.chatgpt_web_runner.api_drive import (
        ApiDriveError,
        validate_impersonation,
    )

    with pytest.raises(ApiDriveError):
        validate_impersonation("chrome999")


def test_ac35_a_second_composer_submit_is_refused_by_the_durable_record(tmp_path) -> None:
    """There is no recovery budget left to spend: the second dispatch raises."""
    import time

    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import Transport
    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import (
        SendIntentLog,
        SendIntentViolation,
    )

    log = SendIntentLog(tmp_path / "attempt")
    log.open_attempt(run_id="r", attempt_id="a", prompt_sha="0" * 64, deadline_at=time.time() + 600)

    class _Locator:
        @property
        def first(self):
            return self

        async def press(self, *args, **kwargs):
            return None

    class _Page:
        def locator(self, selector):
            return _Locator()

    transport = Transport(_Page(), intent_log=log)
    asyncio.run(transport.trigger_composer_mint())
    with pytest.raises(SendIntentViolation):
        asyncio.run(transport.trigger_composer_mint())


def test_ac35_the_composed_path_has_no_transport_fallback() -> None:
    """One sender. No `except ... : try the other transport` anywhere."""
    text = Path(production.__file__).read_text(encoding="utf-8")
    assert text.count("send_once(") == 1, "more than one origin sender call site"

    # Structural, not textual: every `continue_` call in the dispatcher must sit
    # ABOVE the conversation-POST branch, i.e. on the non-conversation path. A
    # snippet match here would break on any reformat while proving less.
    import ast

    tree = ast.parse(text)
    dispatcher = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatcher"
    )
    # The `if not is_conversation_post(...)` statement, and its body.
    guard_if = next(
        node
        for node in ast.walk(dispatcher)
        if isinstance(node, ast.If)
        and any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "is_conversation_post"
            for inner in ast.walk(node.test)
        )
    )

    def _continues(scope) -> set:
        return {
            id(node)
            for node in ast.walk(scope)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "continue_"
        }

    all_continues = _continues(dispatcher)
    non_conversation = set()
    for statement in guard_if.body:
        non_conversation |= _continues(statement)
    # The defensive `except` before the guard also continues; it cannot reach
    # the conversation branch because it has no method/url to match on.
    defensive = {
        id(node)
        for handler in ast.walk(dispatcher)
        if isinstance(handler, ast.ExceptHandler) and handler.lineno < guard_if.lineno
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "continue_"
    }
    assert non_conversation, "non-conversation traffic must still load the page"
    leaked = all_continues - non_conversation - defensive
    assert not leaked, "a continue_ call is reachable from the conversation-POST branch"


def test_ac35_send_once_is_the_only_origin_post_in_the_runner() -> None:
    root = Path(production.__file__).parent
    posting = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "session.post(" in text:
            posting.append(path.name)
    assert posting == ["api_drive.py"], posting


# =====================================================================
# D11 — the build stop is reachable from production, not only from the arms
# =====================================================================


def test_d11_production_wires_the_disposition_oracle() -> None:
    """``observe()`` must be reachable from the composed path.

    Structural, not textual. The two source-text scans this replaces asserted
    the build-stop branch was *written* — which it was, while the control flow
    could not reach it on either ordering (B3 review finding 4). The arms that
    now prove reachability by EXECUTION live in
    ``test_f862_d11_composed_arms.py``; what is left here is the one static
    fact those arms cannot state: the wiring call exists in the dispatcher.
    """
    import ast

    text = Path(production.__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    dispatcher = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatcher"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_wire_request_events"
        for node in ast.walk(dispatcher)
    ), "the dispatcher does not feed the D1 disposition oracle"

    wire = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_wire_request_events"
    )
    events = {
        element.value
        for node in ast.walk(wire)
        if isinstance(node, ast.Tuple)
        for element in node.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    }
    assert {"requestfailed", "requestfinished", "response"} <= events


def test_d11_the_oracle_distinguishes_a_release_from_a_local_fulfil() -> None:
    """The distinction the build stop rests on, asserted directly.

    A ``response`` observed while the route is still HELD is the browser's copy
    reaching the origin. The same event during a local fulfil terminates as
    FULFILLED. If these collapsed, D11 could never fire.
    """
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
        HeldRoute,
        RouteDisposition,
        RouteGenerations,
    )

    gens = RouteGenerations(page=1, context=1, cdp_session=1)

    class _Route:
        async def fulfill(self, **kwargs):
            return None

    async def _live() -> None:
        await asyncio.sleep(3600)

    async def _run() -> tuple:
        owner = asyncio.ensure_future(_live())
        try:
            released = HeldRoute(
                _Route(), object(), attempt_id="a", generations=gens, owner_task=owner
            )
            first = await released.observe("response")

            fulfilled = HeldRoute(
                _Route(), object(), attempt_id="b", generations=gens, owner_task=owner
            )
            await fulfilled.fulfil(body=b"local")
            second = await fulfilled.observe("response")
            return first, second
        finally:
            owner.cancel()

    released_disposition, fulfilled_disposition = asyncio.run(_run())
    assert released_disposition is RouteDisposition.RELEASED_TO_ORIGIN
    assert fulfilled_disposition is RouteDisposition.FULFILLED


# =====================================================================
# D6 — the composed path's ledger sequence is legal end to end
# =====================================================================


def test_d6_the_composed_ledger_sequence_is_legal_and_accepts(tmp_path: Path) -> None:
    """Replay exactly the transitions ``_drive_composed_turn`` performs.

    The state machine refuses illegal orderings by raising, so a sequence that
    completes and then passes ``acceptance_failure`` is the proof that the
    composed path's ordering is admissible — including the two that are easy to
    get wrong: the relay window must close BEFORE the mint is reserved, and the
    submit-dispatch count must end equal to the observed-send count.
    """
    import time

    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import AttemptState, SendIntentLog

    log = SendIntentLog(tmp_path / "attempt")
    log.create_locked_attempt(
        run_id="r",
        attempt_id="a",
        prompt_sha="0" * 64,
        deadline_at=time.time() + 600,
        profile_epoch="epoch",
        mint_id="m",
        mint_ordinal=1,
        relay_token_hash="f" * 64,
        relay_token_expires_at=time.time() + 600,
    )
    log.transition(AttemptState.OWNED_BROWSER_READY)
    log.transition(AttemptState.CONNECTOR_READY)
    log.transition(AttemptState.INPUT_READY)
    log.transition(AttemptState.INTERCEPT_ARMED)
    log.record_send_intent(conversation_id=None, current_node=None, attempt_nonce="nonce")
    # The binding window closes here — before anything network-capable.
    log.record_relay_skipped(skipped_at=time.time())
    log.transition(AttemptState.COMPOSER_MINT_TRIGGERED)
    log.record_submit_dispatch()
    log.record_request_held(
        body_sha256="a" * 64,
        header_names=("content-type",),
        page_generation=1,
        context_generation=1,
        cdp_session_generation=1,
    )
    log.record_send_observed()
    log.reserve_mint()
    assert log.record.attempt_state == AttemptState.MINT_RESERVED.value
    assert log.record.reserved_at is not None
    log.record_python_post_invoked()
    assert log.record.attempt_state == AttemptState.PYTHON_POST_INVOKED.value
    log.transition(AttemptState.RAW_SSE_RELAY, relay_status="complete")
    log.transition(AttemptState.GET_VERIFY)
    log.transition(AttemptState.BROWSER_FULFIL)

    assert log.acceptance_failure() is None
    counters = log.counters()
    assert counters["submits_dispatched"] == 1
    assert counters["sends_observed"] == 1
    # The recovery path is deleted, so this can only ever be zero.
    assert counters["recoveries_used"] == 0


def test_d6_the_relay_window_cannot_reopen_after_the_mint(tmp_path: Path) -> None:
    """A reserved mint closes the binding window for good."""
    import time

    import pytest as _pytest

    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import (
        AttemptState,
        SendIntentLog,
        SendIntentViolation,
    )

    log = SendIntentLog(tmp_path / "attempt")
    log.create_locked_attempt(
        run_id="r",
        attempt_id="a",
        prompt_sha="0" * 64,
        deadline_at=time.time() + 600,
        profile_epoch="epoch",
        mint_id="m",
        mint_ordinal=1,
        relay_token_hash="f" * 64,
        relay_token_expires_at=time.time() + 600,
    )
    for state in (
        AttemptState.OWNED_BROWSER_READY,
        AttemptState.CONNECTOR_READY,
        AttemptState.INPUT_READY,
        AttemptState.INTERCEPT_ARMED,
    ):
        log.transition(state)
    log.record_send_intent(conversation_id=None, current_node=None, attempt_nonce="n")
    log.record_relay_skipped(skipped_at=time.time())
    log.transition(AttemptState.COMPOSER_MINT_TRIGGERED)
    log.record_submit_dispatch()
    log.record_request_held(
        body_sha256="a" * 64,
        header_names=(),
        page_generation=1,
        context_generation=1,
        cdp_session_generation=1,
    )
    log.record_send_observed()
    log.reserve_mint()

    with _pytest.raises(SendIntentViolation):
        log.record_relay_bound(subscriber_id="late", bound_at=time.time())
