"""Instance-bound HTTP transport for the CAO API."""

from __future__ import annotations

import os
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

_PRODUCTION_PORT = 9889


class EndpointConfigurationError(RuntimeError):
    """The process has an invalid or incomplete CAO endpoint binding."""


def resolve_endpoint() -> str:
    """Resolve the CAO API endpoint at call time, failing closed in a sandbox."""
    explicit = os.environ.get("CAO_ENDPOINT", "").strip()
    if explicit:
        parsed = urlsplit(explicit)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise EndpointConfigurationError("CAO_ENDPOINT must be a loopback http origin")
        if os.environ.get("CAO_INSTANCE_ID") and parsed.port == _PRODUCTION_PORT:
            raise EndpointConfigurationError("sandbox endpoint must not resolve to production")
        return explicit.rstrip("/")

    if os.environ.get("CAO_INSTANCE_ID"):
        raise EndpointConfigurationError("CAO_ENDPOINT is required for a sandbox instance")

    host = os.environ.get("CAO_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.environ.get("CAO_API_PORT", str(_PRODUCTION_PORT)))
    except ValueError as exc:
        raise EndpointConfigurationError("CAO_API_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise EndpointConfigurationError("CAO_API_PORT is outside the valid range")
    return f"http://{host}:{port}"


def instance_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Return caller headers with the immutable instance binding attached."""
    result = dict(headers or {})
    instance_id = os.environ.get("CAO_INSTANCE_ID", "").strip()
    if instance_id:
        supplied = result.get("X-CAO-Instance")
        if supplied is not None and supplied != instance_id:
            raise EndpointConfigurationError("X-CAO-Instance override does not match this process")
        result["X-CAO-Instance"] = instance_id
    return result


# ── F689 (#544): Production API fence ─────────────────────────────────────────
# F334 (#190) fenced DIRECT IN-PROCESS DB ACCESS: clients/database.py refuses to
# bind the real DATABASE_FILE when PYTEST_CURRENT_TEST is set. That fence has a
# hole on the other axis — a test that talks HTTP to the live server on :9889
# never touches the fenced code path. That hole is how fixture id ``abcd1234``
# reached the RUNNING production instance 48x in one burst (cao_2026-09-01
# 04-28-22.log), interleaved with live status_monitor traffic for the real
# supervisor seat.
#
# This is the symmetric fence, on the single choke point every product HTTP call
# goes through (constants.API_BASE_URL is a downstream-test fixture only; product
# code resolves at call time via cao_http). It fires ONLY when all of:
#   1. PYTEST_CURRENT_TEST is set (pytest injects it automatically), AND
#   2. the callable about to run IS the genuine ``requests`` verb captured at
#      import — identity, not a heuristic. Any test that injects its own
#      transport or patches the verb (with a mock OR a plain stub function)
#      fails that identity check and is deliberately untouched, because nothing
#      it does can put a packet on the wire, AND
#   3. the resolved origin is loopback:9889, the production endpoint.
# Override with CAO_ALLOW_PROD_API_IN_TESTS=1, mirroring
# CAO_ALLOW_PROD_DB_IN_TESTS (contract tests only).
_ALLOW_PROD_API_ENV = "CAO_ALLOW_PROD_API_IN_TESTS"

# The genuine requests verbs, bound once at import — before any test can patch
# them. Identity against these is what separates "a real socket is about to
# open" from "this call is stubbed".
_LIVE_SENDERS = {
    verb: getattr(requests, verb) for verb in ("request", "get", "post", "put", "patch", "delete")
}


def _sender_is_live(transport: Any, verb: str, sender: Any) -> bool:
    """True only when ``sender`` is the unpatched ``requests`` verb itself."""
    if transport is not requests:
        return False  # caller injected its own transport
    return sender is _LIVE_SENDERS.get(verb)


def _fence_production_api(url: str, verb: str, transport: Any, sender: Any) -> None:
    """Raise if a test process is about to send a real request to production."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return  # not in a test — no fence
    if os.environ.get(_ALLOW_PROD_API_ENV) == "1":
        return  # explicit override — operator knows what they're doing
    if not _sender_is_live(transport, verb, sender):
        return  # stubbed: nothing leaves the process
    parsed = urlsplit(url)
    if parsed.port != _PRODUCTION_PORT or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return
    raise EndpointConfigurationError(
        f"F689 FENCE: test process attempted a live request to the PRODUCTION "
        f"CAO server at {url}. Point the test at a fixture server (the "
        f"``cao_server`` fixture sets CAO_ENDPOINT to a free port), or set "
        f"{_ALLOW_PROD_API_ENV}=1 to override (contract tests only)."
    )


class CAOHttpClient:
    """Small requests-compatible facade bound to :func:`resolve_endpoint`."""

    def __init__(self, transport: Callable[[], Any] | None = None) -> None:
        self._transport = transport or (lambda: requests)

    @staticmethod
    def _url(path: str, base_url: str | None = None) -> str:
        if not path.startswith("/"):
            raise EndpointConfigurationError("CAO API request path must start with '/'")
        resolved = resolve_endpoint()
        selected = base_url.rstrip("/") if base_url is not None else resolved
        if os.environ.get("CAO_INSTANCE_ID") and selected != resolved:
            raise EndpointConfigurationError(
                "sandbox HTTP base_url must match the bound CAO_ENDPOINT"
            )
        return f"{selected}{path}"

    def _prepare(self, verb: str, path: str, kwargs: dict[str, Any]) -> tuple[Any, str]:
        """Bind headers, resolve the URL, and apply the F689 production fence."""
        base_url = kwargs.pop("base_url", None)
        self._bind_headers(kwargs)
        transport = self._transport()
        url = self._url(path, base_url)
        sender = getattr(transport, verb)
        _fence_production_api(url, verb, transport, sender)
        return sender, url

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        sender, url = self._prepare("request", path, kwargs)
        return sender(method, url, **kwargs)

    @staticmethod
    def _bind_headers(kwargs: dict[str, Any]) -> None:
        had_headers = "headers" in kwargs
        original = kwargs.get("headers")
        headers = instance_headers(original)
        if headers:
            kwargs["headers"] = headers
        elif had_headers and original is None:
            kwargs["headers"] = None
        elif not had_headers:
            kwargs.pop("headers", None)

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        sender, url = self._prepare("get", path, kwargs)
        return sender(url, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        sender, url = self._prepare("post", path, kwargs)
        return sender(url, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        sender, url = self._prepare("put", path, kwargs)
        return sender(url, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> requests.Response:
        sender, url = self._prepare("patch", path, kwargs)
        return sender(url, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        sender, url = self._prepare("delete", path, kwargs)
        return sender(url, **kwargs)


cao_http = CAOHttpClient()
