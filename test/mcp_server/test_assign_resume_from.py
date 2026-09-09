"""F829 A2.1 (Option A) — assign(resume_from=…) SHIM-level tests.

Under A2 the shim does NO client-side resume resolution or authorization: it
marks the assign as a resume and FORWARDS the raw handle + overrides + its own
terminal token to the create endpoint, where the SERVER runs
prepare→authorize→claim→create bound to the caller's X-CAO-Terminal-Token.

These tests verify the SHIM contract only:
* resume_from is forwarded to ``_create_terminal`` (with fork_context=None — a
  raw resume fork_context would be refused server-side as resume_not_admitted);
* the legacy ``fork_from + resume=True`` form forwards the SAME way (pure
  syntactic translation into the resume_from field);
* ``resume_from + fork_from`` is an input CONFLICT (client-side pre-check);
* ``resume=True`` alone (no handle) is refused (client-side pre-check);
* a server ``resume_refused`` envelope is relayed VERBATIM.

The authorization/claim/refusal semantics themselves are covered server-side
(test_f829_ac_matrix, test_f829_resume_launch, test_api_resume_admission).
"""

from unittest.mock import patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server import server


def _shim_patches(create_return=("new00001", "kiro_cli")):
    """Patch the create POST + the metadata reads the shim does around it."""
    return (
        patch.object(server, "_create_terminal", return_value=create_return),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"resolved_model": None},
        ),
        patch.object(server, "generate_window_name", return_value="w"),
        patch.object(server, "display_name", return_value="kiro_dev(new00001)"),
    )


def test_resume_from_forwards_handle_to_create(monkeypatch):
    """A2.1: the shim forwards resume_from to the create endpoint with
    fork_context=None (server resolves the launch spec) and surfaces the
    resume success line."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_create, p_meta, p_win, p_dn = _shim_patches()
    with p_create as create, p_meta, p_win, p_dn:
        result = server._assign_impl(
            "kiro_dev", "task", resume_from="old12345", working_directory="/repo/wt"
        )
    assert result["success"] is True
    assert result["terminal_id"] == "new00001"
    kwargs = create.call_args.kwargs
    assert kwargs["resume_from"] == "old12345"
    assert kwargs["fork_context"] is None  # no client-side resume fork_context
    assert kwargs["resume_inherit_pins"] is True
    assert result["resumed_from"] == "old12345"
    assert "resumed from old12345 as new00001" in result["resume_line"]


def test_resume_path_forwards_caller_provider_hint_to_create(monkeypatch):
    """r6 merge regression: main's F838 (#695) guard-checked provider carrier
    ``_f838_checked_provider`` is initialized only in the non-resume (``else``)
    branch of ``_assign_impl``, but BOTH branches converge on the shared
    ``_create_terminal`` call whose ``provider=_resolved_provider or
    _f838_checked_provider`` argument reads it. Merging main into the r5 resume
    branch left the resume path reaching that call with the carrier UNBOUND.

    The crash only manifests when ``_resolved_provider`` is FALSY (a cold caller
    provider hint of ``None`` — the common case), because ``or`` then evaluates
    the right operand and touches the unbound name → ``UnboundLocalError``. A
    truthy hint would short-circuit and hide the bug, so this test deliberately
    drives the resume path with NO provider hint (``provider`` defaults to
    ``None``) and asserts the caller's hint (``None``) is forwarded to
    ``_create_terminal`` — the resume path re-resolves provider SERVER-SIDE from
    the reaped root, so the shim forwards the hint unchanged.

    Without the resume-branch ``_f838_checked_provider = None`` init this fails:
    the UnboundLocalError is caught by the assign try/except and returns
    ``success=False`` with no ``_create_terminal`` call at all.
    """
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_create, p_meta, p_win, p_dn = _shim_patches()
    with p_create as create, p_meta, p_win, p_dn:
        result = server._assign_impl(
            "kiro_dev",
            "task",
            resume_from="old12345",
            working_directory="/repo/wt",
        )
    assert result["success"] is True, (
        "resume path must not raise UnboundLocalError on the F838 provider "
        f"carrier (got {result!r})"
    )
    create.assert_called_once()
    # The resume path forwards the caller's provider hint (None here) unchanged;
    # evaluating ``None or _f838_checked_provider`` must not raise.
    assert create.call_args.kwargs["provider"] is None


def test_legacy_fork_from_resume_forwards_as_resume(monkeypatch):
    """r1 #1 preserved: fork_from + resume=True is pure syntax that translates
    into the SAME resume_from forward (no separate execution path)."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_create, p_meta, p_win, p_dn = _shim_patches()
    with p_create as create, p_meta, p_win, p_dn:
        result = server._assign_impl(
            "kiro_dev", "task", fork_from="old12345", resume=True, working_directory="/repo/wt"
        )
    assert result["success"] is True
    assert create.call_args.kwargs["resume_from"] == "old12345"
    assert result["resumed_from"] == "old12345"


def test_inherit_pins_false_forwarded_to_server(monkeypatch):
    """A2.1: resume_inherit_pins=False is FORWARDED; the pins-drop refusal is a
    SERVER decision now, not a client-side pre-check."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_create, p_meta, p_win, p_dn = _shim_patches()
    with p_create as create, p_meta, p_win, p_dn:
        server._assign_impl(
            "kiro_dev",
            "task",
            resume_from="old12345",
            inherit_pins=False,
            working_directory="/repo/wt",
        )
    assert create.call_args.kwargs["resume_inherit_pins"] is False


def test_resume_from_plus_fork_from_is_input_conflict(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch.object(server, "_create_terminal") as create:
        result = server._assign_impl("kiro_dev", "task", fork_from="base", resume_from="old12345")
    assert result["success"] is False
    assert result["error"] == "resume_refused"
    assert result["reason"] == "resume_input_conflict"
    create.assert_not_called()


def test_resume_true_without_handle_refuses(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch.object(server, "_create_terminal") as create:
        result = server._assign_impl("kiro_dev", "task", resume=True)
    assert result["success"] is False
    assert result["error"] == "resume_refused"
    assert result["reason"] == "resume_true_without_handle"
    create.assert_not_called()


def test_server_resume_refused_relayed_verbatim(monkeypatch):
    """A2.1: a server-side resume_refused envelope (e.g. caller_unverified) is
    relayed to the caller verbatim, with no client-side reinterpretation."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

    resp = requests.Response()
    resp.status_code = 403
    resp._content = (
        b'{"detail":{"error":"resume_refused","missing":"identity",'
        b'"reason":"caller_unverified","retryable":false,"how":"x",'
        b'"message":"resume_refused (missing identity): caller_unverified"}}'
    )
    err = requests.HTTPError(response=resp)
    err.response = resp

    with patch.object(server, "_create_terminal", side_effect=err):
        result = server._assign_impl(
            "kiro_dev", "task", resume_from="old12345", working_directory="/repo/wt"
        )
    assert result["success"] is False
    assert result["error"] == "resume_refused"
    assert result["reason"] == "caller_unverified"
    assert result["retryable"] is False


def test_resume_from_reaches_body_even_with_defer_init_false(monkeypatch):
    """F829 A2.1 (r3, verdict SHOULD-2): the defer_init trap.

    Before the fix, ``_create_terminal`` added ``resume_from`` to the request
    body ONLY inside ``if defer_init:``. A ``defer_init=False`` resume therefore
    POSTed a body with NO ``resume_from`` — the server never entered admission
    and the resume degraded SILENTLY into a cold create. This test drives
    ``_create_terminal`` with ``defer_init=False`` and asserts the POSTed body
    carries ``resume_from`` (and the token header is sent). It FAILS on the
    pre-fix code path.
    """
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_TERMINAL_TOKEN", "tok-abcd")

    captured = {}

    class _Resp:
        status_code = 201

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _fake_get(path, **kw):
        # Supervisor terminal metadata lookup inside _create_terminal.
        return _Resp(
            {
                "provider": "kiro_cli",
                "session_name": "cao-session",
                "allowed_tools": None,
            }
        )

    def _fake_post(path, **kw):
        captured["path"] = path
        captured["json"] = kw.get("json")
        captured["headers"] = kw.get("headers")
        return _Resp({"id": "new00001"})

    with (
        patch.object(server.cao_http, "get", side_effect=_fake_get),
        patch.object(server.cao_http, "post", side_effect=_fake_post),
        patch.object(server, "resolve_provider", return_value="kiro_cli"),
        patch.object(server, "_resolve_child_allowed_tools", return_value=None),
    ):
        server._create_terminal(
            "kiro_dev",
            working_directory="/repo/wt",
            defer_init=False,
            resume_from="old12345",
        )

    body = captured["json"] or {}
    assert body.get("resume_from") == "old12345", (
        "defer_init=False resume must still carry resume_from in the body "
        f"(got {body!r}) — else it degrades to a silent cold create"
    )
    assert body.get("resume_inherit_pins") is True
    # The caller-binding token header is sent on the resume path regardless.
    assert captured["headers"] == {"X-CAO-Terminal-Token": "tok-abcd"}


def test_cold_assign_body_omits_resume_from_key(monkeypatch):
    """F829 A2 (r5, codex r4 close condition item 2): the shim's omission value
    for ``resume_from`` is Python ``None``. A COLD assign (``resume_from=None``)
    must OMIT the key from the POST body entirely — NOT send
    ``"resume_from": null`` — so the r5 server-side presence gate
    (``"resume_from" in body.model_fields_set``) does NOT mistake a cold assign
    for an explicit-null resume and refuse it.

    The regression that sends the key with a null value (e.g.
    ``json_body["resume_from"] = resume_from`` unconditionally) puts
    ``"resume_from"`` into the body and would trip the server's 422, breaking
    every cold assign. This test asserts the key is absent AND no token header
    is sent on the cold path.
    """
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

    captured = {}

    class _Resp:
        status_code = 201

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _fake_get(path, **kw):
        return _Resp(
            {
                "provider": "kiro_cli",
                "session_name": "cao-session",
                "allowed_tools": None,
            }
        )

    def _fake_post(path, **kw):
        captured["path"] = path
        captured["json"] = kw.get("json")
        captured["headers"] = kw.get("headers")
        return _Resp({"id": "new00001"})

    with (
        patch.object(server.cao_http, "get", side_effect=_fake_get),
        patch.object(server.cao_http, "post", side_effect=_fake_post),
        patch.object(server, "resolve_provider", return_value="kiro_cli"),
        patch.object(server, "_resolve_child_allowed_tools", return_value=None),
    ):
        server._create_terminal(
            "kiro_dev",
            working_directory="/repo/wt",
            defer_init=False,
            resume_from=None,
        )

    body = captured["json"] or {}
    assert "resume_from" not in body, (
        "a cold assign (resume_from=None) must OMIT the resume_from key, not "
        f"send it as null (got {body!r}) — else the server's presence gate "
        "refuses the cold assign"
    )
    assert "resume_inherit_pins" not in body
    # No caller-binding token header on the cold path.
    assert captured.get("headers") is None


@pytest.mark.parametrize("blank", ["", " ", "\t", "   \n  "])
def test_blank_resume_from_refused_zero_spawn_shim(monkeypatch, blank):
    """F829 A2 (r4, codex r3 fresh adversary) — the MCP-shim half.

    A present-but-blank ``resume_from`` classified by TRUTHINESS would fall
    through the ``if resume_from:`` gate to a COLD assign (no handle, no
    admission, no refusal). The shim now classifies by PRESENCE: a blank handle
    is a typed ``resume_refused`` / ``resume_handle_blank`` with ZERO spawn —
    ``_create_terminal`` is never called. The regression that restores the
    truthiness gate lets the blank degrade to a cold create and makes
    ``create.assert_not_called`` RED.
    """
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch.object(server, "_create_terminal") as create:
        result = server._assign_impl("kiro_dev", "task", resume_from=blank)
    assert result["success"] is False
    assert result["error"] == "resume_refused"
    assert result["reason"] == "resume_handle_blank"
    assert result["retryable"] is False
    create.assert_not_called()


def test_resume_threads_resume_cell_class(monkeypatch):
    """F868/F870 r2 (B4/D5c): an ordinary resume threads cell_request_class="resume"
    (ROUTING-EQUIVALENT continuation) to _create_terminal, so the shared choke
    point re-classifies the resumed cell as routing-equivalent (non-gate
    uncertified allowed with the marker, gate uncertified refused)."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_create, p_meta, p_win, p_dn = _shim_patches()
    with p_create as create, p_meta, p_win, p_dn:
        result = server._assign_impl(
            "kiro_dev", "task", resume_from="old12345", working_directory="/repo/wt"
        )
    assert result["success"] is True
    assert create.call_args.kwargs["cell_request_class"] == "resume"


def test_resume_bare_position_override_threads_explicit_cell_class(monkeypatch):
    """F868/F870 r2 (D5c): a caller-supplied bare-POSITION override on resume is
    the caller's EXPLICIT cell choice (the named position differs from the
    recorded identity's kiro_dev), so it threads cell_request_class="explicit"
    and must be PASS-certified."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_create, p_meta, p_win, p_dn = _shim_patches()
    with (
        p_create as create,
        p_meta,
        p_win,
        p_dn,
        # 'dev' is a real position AND differs from the identity's kiro_dev.
        patch(
            "cli_agent_orchestrator.utils.agent_profiles._position_exists",
            return_value=True,
        ),
    ):
        result = server._assign_impl(
            "dev", "task", resume_from="old12345", working_directory="/repo/wt"
        )
    assert result["success"] is True
    assert create.call_args.kwargs["cell_request_class"] == "explicit"
