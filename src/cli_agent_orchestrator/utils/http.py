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


class CAOHttpClient:
    """Small requests-compatible facade bound to :func:`resolve_endpoint`."""

    def __init__(self, transport: Callable[[], Any] | None = None) -> None:
        self._transport = transport or (lambda: requests)

    @staticmethod
    def _url(path: str, base_url: str | None = None) -> str:
        if not path.startswith("/"):
            raise EndpointConfigurationError("CAO API request path must start with '/'")
        return f"{(base_url or resolve_endpoint()).rstrip('/')}{path}"

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        base_url = kwargs.pop("base_url", None)
        self._bind_headers(kwargs)
        return self._transport().request(method, self._url(path, base_url), **kwargs)

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
        base_url = kwargs.pop("base_url", None)
        self._bind_headers(kwargs)
        return self._transport().get(self._url(path, base_url), **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        base_url = kwargs.pop("base_url", None)
        self._bind_headers(kwargs)
        return self._transport().post(self._url(path, base_url), **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        base_url = kwargs.pop("base_url", None)
        self._bind_headers(kwargs)
        return self._transport().put(self._url(path, base_url), **kwargs)

    def patch(self, path: str, **kwargs: Any) -> requests.Response:
        base_url = kwargs.pop("base_url", None)
        self._bind_headers(kwargs)
        return self._transport().patch(self._url(path, base_url), **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        base_url = kwargs.pop("base_url", None)
        self._bind_headers(kwargs)
        return self._transport().delete(self._url(path, base_url), **kwargs)


cao_http = CAOHttpClient()
