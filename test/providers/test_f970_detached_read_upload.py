"""F970 (#819) step 3 — reads and uploads off the browser, under C5's rules.

r4 measured both capabilities (probes §7, §8). What these tests pin is not that
HTTP works — it is the three ways this path could go quietly wrong:

1. **A silently-downgraded TLS posture.** ``curl_cffi`` 0.13.0 accepted an
   unknown impersonation template without raising, so "the client constructed"
   proves nothing. C5 therefore demands validation BEFORE construction and a
   fixture asserting a typed startup failure AND zero requests.
2. **An off-origin destination that merely looks right.** The signed upload
   leaves the origin by protocol; a suffix match would happily accept
   ``oaiusercontent.com.evil.test``.
3. **A readiness predicate that lies.** r4's first pass keyed on the completion
   call's ``status`` and on ``retrieval_index_status`` and polled 27 times
   while the file was already usable; the authoritative field is ``state``.

Everything runs offline against fakes — the optional ``curl_cffi`` extra is not
installed in the test environment, which is itself part of the contract.
"""

from __future__ import annotations

import json

import pytest

from cli_agent_orchestrator.chatgpt_web_runner import detached_http as dh
from cli_agent_orchestrator.chatgpt_web_runner.detached_transport import (
    DetachedReader,
    DetachedUploader,
)
from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError, RunnerErrorCode
from cli_agent_orchestrator.chatgpt_web_runner.session_export import (
    SessionBundle,
    export_session,
    load_session,
    save_session,
)

pytestmark = pytest.mark.unit

_SUPPORTED = frozenset({"chrome", "chrome124", "chrome131", "chrome142", "firefox133"})

#: A real backend conversation id shape (bare uuid — the ``WEB:`` route id is
#: a different thing entirely, F862 r2).
_CONV = "6aa15468-e7a0-83e9-8db7-18a35b16e573"


def _bundle(**kwargs) -> SessionBundle:
    base = dict(
        cookies=({"name": "__Secure-next-auth.session-token", "value": "SECRET-COOKIE"},),
        bearer="SECRET-BEARER",
        user_agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/146",
        device_id="dev-123",
        exported_at=1_788_000_000.0,
    )
    base.update(kwargs)
    return SessionBundle(**base)  # type: ignore[arg-type]


class _Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _FakeSession:
    """Records every call so a test can assert "zero requests" credibly."""

    def __init__(self, responses=None):
        self.calls: list = []
        self.cookies = type("C", (), {"set": lambda *a, **k: None})()
        self._responses = responses or {}

    def _respond(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for pattern, resp in self._responses.items():
            if pattern in url:
                return resp() if callable(resp) else resp
        return _Resp(404, {"detail": "no fake response"})

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._respond("PUT", url, **kwargs)

    def close(self):
        pass


# ── 1. TLS posture: fail loud, before any traffic ────────────────────────────


def test_an_unknown_template_fails_typed_and_issues_zero_requests():
    calls = []

    def _factory(**kwargs):
        calls.append(kwargs)
        return _FakeSession()

    with pytest.raises(dh.TlsTemplateUnavailable) as exc:
        dh.build_session(
            _bundle(), template="not_a_real_browser", supported=_SUPPORTED, session_factory=_factory
        )
    assert "No request was made" in str(exc.value)
    assert calls == []  # the client was never even constructed


def test_a_certified_template_the_pinned_client_does_not_declare_also_fails():
    """Both halves of C5's allow-list must hold: our probe posture AND the
    library's own declared set. A client bump that drops a profile must fail
    loudly, not fall back."""
    with pytest.raises(dh.TlsTemplateUnavailable):
        dh.assert_template_supported("chrome124", supported=frozenset({"chrome142"}))


def test_a_valid_template_is_returned_unchanged_never_clamped():
    assert dh.assert_template_supported("chrome131", supported=_SUPPORTED) == "chrome131"
    assert dh.assert_template_supported("chrome", supported=_SUPPORTED) == "chrome"


def test_an_empty_or_uncertified_template_is_refused():
    for bad in ("", "   ", "firefox133", "safari17_0"):
        with pytest.raises(dh.TlsTemplateUnavailable):
            dh.assert_template_supported(bad, supported=_SUPPORTED)


def test_the_configured_template_comes_from_env_and_defaults_without_aliasing():
    assert dh.configured_template({}) == dh.DEFAULT_TEMPLATE
    assert dh.configured_template({"CAO_CHATGPT_TLS_TEMPLATE": "chrome131"}) == "chrome131"
    # An env value that is not certified reaches the assertion and fails there —
    # it is never silently replaced by the default.
    with pytest.raises(dh.TlsTemplateUnavailable):
        dh.assert_template_supported(
            dh.configured_template({"CAO_CHATGPT_TLS_TEMPLATE": "chrome999"}),
            supported=_SUPPORTED,
        )


def test_the_absent_optional_extra_is_a_typed_startup_failure_not_a_default():
    """The test environment has no curl_cffi — with no client there is no
    supported template, and the caller must not proceed."""
    with pytest.raises(dh.TlsTemplateUnavailable):
        dh.supported_templates()


# ── 2. destination binding for the signed upload (C5 carve-out) ──────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://files.oaiusercontent.com/abc?sig=x",
        "https://oaiusercontent.com/abc",
    ],
)
def test_the_measured_signed_destination_is_allowed(url):
    assert dh.assert_upload_destination(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://oaiusercontent.com.evil.test/abc",  # suffix lookalike
        "https://notoaiusercontent.com/abc",  # substring lookalike
        "http://files.oaiusercontent.com/abc",  # not https
        "https://user:pw@files.oaiusercontent.com/abc",  # userinfo
        "https://files.oaiusercontent.com:8443/abc",  # odd port
        "https://chatgpt.com/backend-api/files",  # same-origin is not the destination
        "",
    ],
)
def test_every_lookalike_destination_is_refused(url):
    with pytest.raises(RunnerError) as exc:
        dh.assert_upload_destination(url)
    assert exc.value.code is RunnerErrorCode.EGRESS_FORBIDDEN


def test_the_upload_request_carries_no_session_auth():
    headers = dh.upload_headers()
    dh.assert_no_session_auth(headers)  # must not raise
    assert set(headers) == {"x-ms-blob-type", "x-ms-version", "content-type"}
    with pytest.raises(RunnerError):
        dh.assert_no_session_auth({**headers, "authorization": "Bearer x"})
    with pytest.raises(RunnerError):
        dh.assert_no_session_auth({**headers, "Cookie": "a=b"})


def test_same_origin_headers_carry_the_identity_the_read_routes_need():
    headers = dh.base_headers(_bundle())
    assert headers["authorization"] == "Bearer SECRET-BEARER"
    assert headers["oai-device-id"] == "dev-123"
    assert headers["origin"] == "https://chatgpt.com"


# ── 3. the detached read ─────────────────────────────────────────────────────


def test_the_detached_read_returns_the_in_page_shape_verbatim():
    """The swap only works because the poll loop cannot tell the transports
    apart — same keys, same types, and no header/cookie/bearer in the result."""
    body = {"conversation_id": _CONV, "current_node": "n1", "mapping": {}}
    session = _FakeSession({"/backend-api/conversation/": _Resp(200, body)})
    probe = DetachedReader(_bundle(), session=session).read_conversation(_CONV)
    assert probe == {"httpStatus": 200, "ok": True, "retryAfter": None, "body": body}
    assert "SECRET" not in json.dumps(probe)


def test_a_429_surfaces_its_retry_after_for_the_poll_backoff():
    session = _FakeSession(
        {"/backend-api/conversation/": _Resp(429, None, headers={"retry-after": "7"})}
    )
    probe = DetachedReader(_bundle(), session=session).read_conversation(_CONV)
    assert (probe["httpStatus"], probe["ok"], probe["retryAfter"]) == (429, False, 7.0)


def test_read_containment_binds_the_detached_path_too():
    """The bound is the transport-independent one (AC-11b): the owned
    conversation only, and no enumeration.

    ``../conversations`` is the case that matters: the path COMPARES equal to
    the permitted read (neither urlparse nor the old check normalizes ``..``),
    while a real client resolves it to the enumeration endpoint the exception
    exists to keep unreachable. The id-shape guard added with this step closes
    it for BOTH transports at once."""
    session = _FakeSession({"/backend-api/conversation/": _Resp(200, {})})
    reader = DetachedReader(_bundle(), session=session)
    for bad in ("../conversations", "WEB:" + _CONV, "", "*"):
        with pytest.raises(RunnerError) as exc:
            reader.read_conversation(bad)
        assert exc.value.code is RunnerErrorCode.READ_FORBIDDEN
    assert session.calls == []  # refused before any request


def test_the_in_page_transport_gets_the_same_traversal_guard():
    """Same hole, other transport: an in-page
    ``fetch('/backend-api/conversation/' + '../conversations')`` is resolved by
    the browser to the enumeration endpoint."""
    import asyncio

    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import Transport

    class _Page:
        def __init__(self):
            self.evaluated = []

        async def evaluate(self, script, *a):
            self.evaluated.append(script)
            return {"httpStatus": 200, "ok": True, "body": {}}

    page = _Page()
    with pytest.raises(RunnerError) as exc:
        asyncio.run(Transport(page).read_conversation("../conversations"))
    assert exc.value.code is RunnerErrorCode.READ_FORBIDDEN
    assert page.evaluated == []


# ── 4. the upload chain (built, protocol-pinned, NOT wired in) ───────────────


def _upload_session(**overrides):
    responses = {
        "/backend-api/files/": _Resp(200, {"state": "ready", "file_name": "b.txt"}),
        "oaiusercontent.com": _Resp(201),
        "/backend-api/files": _Resp(
            200,
            {"file_id": "file-1", "upload_url": "https://files.oaiusercontent.com/abc?sig=x"},
        ),
    }
    responses.update(overrides)
    return _FakeSession(responses)


def test_the_chain_runs_allocate_transfer_complete_then_readiness(tmp_path):
    path = tmp_path / "b.txt"
    path.write_text("hello bundle", encoding="utf-8")
    session = _upload_session()
    result = DetachedUploader(_bundle(), session=session, sleep=lambda _s: None).upload(str(path))

    assert result.file_id == "file-1"
    assert result.byte_length == len("hello bundle")
    assert result.state == "ready"
    methods = [(m, u.split("?")[0]) for m, u, _ in session.calls]
    assert methods[0] == ("POST", "https://chatgpt.com/backend-api/files")
    assert methods[1] == ("PUT", "https://files.oaiusercontent.com/abc")
    assert methods[2] == ("POST", "https://chatgpt.com/backend-api/files/file-1/uploaded")
    assert methods[3][0] == "GET"


def test_the_byte_transfer_forwards_no_session_credential(tmp_path):
    path = tmp_path / "b.txt"
    path.write_text("x", encoding="utf-8")
    session = _upload_session()
    DetachedUploader(_bundle(), session=session, sleep=lambda _s: None).upload(str(path))
    put = next(call for call in session.calls if call[0] == "PUT")
    assert "SECRET" not in json.dumps(put[2].get("headers"))
    assert put[2]["allow_redirects"] is False


def test_completion_success_is_not_readiness(tmp_path):
    """r4's correction, encoded: the completion call's own status field and
    retrieval_index_status are NOT the predicate — ``state`` is."""
    path = tmp_path / "b.txt"
    path.write_text("x", encoding="utf-8")
    session = _upload_session(
        **{
            "/backend-api/files/": _Resp(
                200, {"status": "success", "retrieval_index_status": "done", "state": "processing"}
            )
        }
    )
    uploader = DetachedUploader(
        _bundle(), session=session, sleep=lambda _s: None, readiness_timeout_s=0.05
    )
    with pytest.raises(RunnerError) as exc:
        uploader.upload(str(path))
    assert exc.value.code is RunnerErrorCode.UPLOAD_UNCONFIRMED


def test_a_signed_destination_that_redirects_is_refused(tmp_path):
    path = tmp_path / "b.txt"
    path.write_text("x", encoding="utf-8")
    session = _upload_session(**{"oaiusercontent.com": _Resp(302)})
    with pytest.raises(RunnerError) as exc:
        DetachedUploader(_bundle(), session=session, sleep=lambda _s: None).upload(str(path))
    assert exc.value.code is RunnerErrorCode.EGRESS_FORBIDDEN


def test_an_allocation_naming_a_foreign_destination_never_transfers(tmp_path):
    path = tmp_path / "b.txt"
    path.write_text("x", encoding="utf-8")
    session = _upload_session(
        **{
            "/backend-api/files": _Resp(
                200, {"file_id": "file-1", "upload_url": "https://evil.test/abc"}
            )
        }
    )
    with pytest.raises(RunnerError) as exc:
        DetachedUploader(_bundle(), session=session, sleep=lambda _s: None).upload(str(path))
    assert exc.value.code is RunnerErrorCode.EGRESS_FORBIDDEN
    assert not any(call[0] == "PUT" for call in session.calls)


# ── 5. the session export ────────────────────────────────────────────────────


class _FakeContext:
    async def cookies(self):
        return [{"name": "a", "value": "SECRET", "httpOnly": True}]


class _FakePage:
    async def evaluate(self, _script):
        return {
            "bearer": "SECRET-BEARER",
            "user_id": "user-1",
            "user_agent": "UA/1",
            "device_id": "dev-1",
            "language": "en-US",
            "session_status": 200,
        }


def test_export_produces_a_usable_bundle_whose_summary_leaks_nothing():
    import asyncio

    bundle = asyncio.run(export_session(_FakeContext(), _FakePage(), profile="/p"))
    assert bundle.usable is True
    summary = json.dumps(bundle.summary())
    assert "SECRET" not in summary
    assert '"cookie_count": 1' in summary


def test_a_cookie_jar_without_a_bearer_is_not_a_session():
    """r4's cookie-only control returned 404 on the conversation route."""
    assert _bundle(bearer="").usable is False
    assert SessionBundle(cookies=(), bearer="b").usable is False


def test_the_export_is_refused_outside_the_approved_profile_root(tmp_path):
    with pytest.raises(RunnerError) as exc:
        save_session(_bundle(), tmp_path / "session-export.json")
    assert exc.value.code is RunnerErrorCode.ACCESS_DENIED


def test_save_and_load_round_trip_0600(monkeypatch, tmp_path):
    import os

    monkeypatch.setattr(
        "cli_agent_orchestrator.chatgpt_web_runner.runtime._APPROVED_PROFILE_ROOT", tmp_path
    )
    target = save_session(_bundle(), tmp_path / "p1" / "session-export.json")
    assert oct(os.stat(target).st_mode & 0o777) == "0o600"
    loaded = load_session(target)
    assert loaded is not None and loaded.bearer == "SECRET-BEARER"
    assert load_session(tmp_path / "nope.json") is None
