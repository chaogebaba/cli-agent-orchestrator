"""F970 (#819) step 3 — the detached HTTP client: TLS posture and destinations.

Two rules from Amendment C govern every byte this lane sends from Python, and
both are here rather than at the call sites so they cannot be forgotten by the
next caller.

**C5's fail-loud TLS template.** *"Before constructing or using any enabled
Python HTTP client, validate the configured TLS impersonation template against
an explicit allow-list supported by the pinned client version and the posture
accepted in the route-specific probes. A miss or unavailable template fails
startup before network traffic; do not clamp, alias or silently choose a
default. Successful client construction does not prove template validity:
curl_cffi 0.13.0 accepted an unknown template silently in r4."* That last
sentence is the whole reason this module exists: the library will happily build
a client on a template it does not have, and the first evidence would be a
strange 403 weeks later. So the check runs BEFORE construction, against the
intersection of what r4 actually exercised and what the pinned client declares,
and it raises :class:`TlsTemplateUnavailable` — a typed STARTUP exception that
precedes the condition plane (NB-1: it invents no terminal status).

**C5's signed-upload carve-out.** The upload byte transfer is off-origin by
protocol — a signed ``oaiusercontent.com`` destination named by a same-origin
allocation response, not a redirect. The carve-out permits exactly that, so the
destination check is an exact-domain / dot-delimited-subdomain match (a suffix
match would accept ``oaiusercontent.com.evil.test``), with userinfo, odd ports
and redirects refused, and it forwards no session auth of its own.

``curl_cffi`` is imported lazily and is an OPTIONAL extra: the base install and
every offline test run without it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)
from cli_agent_orchestrator.chatgpt_web_runner.session_export import SessionBundle

logger = logging.getLogger(__name__)

BASE_ORIGIN = "https://chatgpt.com"

#: The impersonation templates r4 actually exercised against the read routes
#: (probe §2: ``GET /backend-api/me`` → 200 on all three). This is the "posture
#: accepted in the route-specific probes" half of C5's allow-list; the other
#: half is the pinned client's own declared set, and BOTH must contain the
#: configured template.
#:
#: ``chrome`` is the library's floating alias for its newest Chrome profile, so
#: it moves when the pinned client version moves — which is precisely why C5
#: says to pin client version and posture together and re-run the route checks
#: on a bump.
CERTIFIED_TEMPLATES: frozenset[str] = frozenset({"chrome", "chrome124", "chrome131"})

#: What the runner uses unless ``CAO_CHATGPT_TLS_TEMPLATE`` names another
#: CERTIFIED one. Never a fallback: an unknown configured value fails, it does
#: not quietly become this.
DEFAULT_TEMPLATE = "chrome"

#: The one permitted off-origin upload destination (C5 carve-out, r4 §8).
UPLOAD_DESTINATION_DOMAIN = "oaiusercontent.com"


class TlsTemplateUnavailable(Exception):
    """The configured TLS impersonation template is not usable — fail startup.

    Deliberately NOT a :class:`RunnerError`: it is raised before any request is
    constructed, ahead of the condition plane, so it can never be mistaken for a
    transient network condition (NB-1). If a caller ever does surface it to the
    condition plane it maps to ``ERROR``/protocol drift, never to a retryable
    kind.
    """


def supported_templates() -> frozenset[str]:
    """The pinned client's own declared template set.

    Raises :class:`TlsTemplateUnavailable` when the optional dependency is
    absent, which is the honest answer to "is this template supported": with no
    client there is no supported template, and the caller must not proceed to
    build one.
    """
    try:
        import typing

        from curl_cffi.requests import impersonate as _imp
    except Exception as exc:  # pragma: no cover - exercised only without the extra
        raise TlsTemplateUnavailable(
            "curl_cffi is not installed; the detached HTTP path requires the "
            "'chatgpt_web' extra. No request was made."
        ) from exc
    declared = typing.get_args(getattr(_imp, "BrowserTypeLiteral", None) or ())
    return frozenset(str(name) for name in declared)


def assert_template_supported(template: str, *, supported: Optional[frozenset[str]] = None) -> str:
    """Validate a template against BOTH allow-lists, or raise before any traffic.

    Returns the template unchanged on success — never a clamped, aliased or
    defaulted substitute (C5: "do not clamp, alias or silently choose a
    default").
    """
    if not isinstance(template, str) or not template.strip():
        raise TlsTemplateUnavailable(
            "no TLS impersonation template configured. No request was made."
        )
    name = template.strip()
    if name not in CERTIFIED_TEMPLATES:
        raise TlsTemplateUnavailable(
            f"TLS impersonation template {name!r} is not in the probe-certified set "
            f"{sorted(CERTIFIED_TEMPLATES)}. Certify it with the route probes before use. "
            "No request was made."
        )
    declared = supported if supported is not None else supported_templates()
    if name not in declared:
        raise TlsTemplateUnavailable(
            f"TLS impersonation template {name!r} is not declared by the pinned client "
            "(it would be accepted silently and the posture would be unknown). "
            "No request was made."
        )
    return name


def configured_template(env: Optional[Dict[str, str]] = None) -> str:
    """The configured template, unvalidated (the caller asserts it)."""
    import os

    source = env if env is not None else dict(os.environ)
    return source.get("CAO_CHATGPT_TLS_TEMPLATE", DEFAULT_TEMPLATE)


def build_session(
    bundle: SessionBundle,
    *,
    template: Optional[str] = None,
    supported: Optional[frozenset[str]] = None,
    session_factory: Optional[Callable[..., Any]] = None,
) -> Any:
    """Construct a cookie-loaded HTTP session — after the template check.

    Ordering is the contract: the assertion happens BEFORE the factory is
    called, so an invalid template can never produce a live client, and the
    unknown-template fixture can assert "typed failure AND zero requests".
    """
    name = assert_template_supported(
        template if template is not None else configured_template(), supported=supported
    )
    if session_factory is None:  # pragma: no cover - requires the optional extra
        from curl_cffi import requests as _cr

        session_factory = _cr.Session
    session = session_factory(impersonate=name)
    for cookie in bundle.cookies:
        try:
            session.cookies.set(
                cookie.get("name"),
                cookie.get("value"),
                domain=cookie.get("domain") or ".chatgpt.com",
                path=cookie.get("path") or "/",
            )
        except Exception:  # pragma: no cover - a malformed cookie is skipped
            continue
    return session


def base_headers(bundle: SessionBundle, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The same-origin header set r4 validated on the read routes."""
    headers = {
        "accept": "*/*",
        "accept-language": f"{bundle.language},en;q=0.9",
        "authorization": f"Bearer {bundle.bearer}",
        "oai-language": "en-US",
        "oai-device-id": bundle.device_id,
        "origin": BASE_ORIGIN,
        "referer": BASE_ORIGIN + "/",
        "user-agent": bundle.user_agent,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if extra:
        headers.update(extra)
    return headers


def _host_matches_domain(host: str, domain: str) -> bool:
    """Exact domain or a dot-delimited subdomain — never a suffix match.

    ``oaiusercontent.com.evil.test`` and ``notoaiusercontent.com`` must both
    fail; ``files.oaiusercontent.com`` must pass.
    """
    host = (host or "").lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def assert_upload_destination(url: str) -> str:
    """Bind the byte transfer to the one permitted signed destination (C5).

    Refuses anything that is not HTTPS on ``oaiusercontent.com`` (exact or a
    real subdomain), plus URLs carrying userinfo or a non-default port — the
    shapes that make a lookalike host or a credential-bearing URL look
    legitimate at a glance.
    """
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        raise RunnerError(
            RunnerErrorCode.EGRESS_FORBIDDEN,
            "upload destination must be https",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    if parsed.username or parsed.password or "@" in (parsed.netloc.split("/")[0] or ""):
        raise RunnerError(
            RunnerErrorCode.EGRESS_FORBIDDEN,
            "upload destination carries userinfo",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    if parsed.port not in (None, 443):
        raise RunnerError(
            RunnerErrorCode.EGRESS_FORBIDDEN,
            "upload destination uses an unexpected port",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    if not _host_matches_domain(parsed.hostname or "", UPLOAD_DESTINATION_DOMAIN):
        raise RunnerError(
            RunnerErrorCode.EGRESS_FORBIDDEN,
            "upload destination is not the measured signed-upload domain",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    return url


def upload_headers(content_type: str = "text/plain") -> Dict[str, str]:
    """Headers for the signed PUT: upload-specific only.

    C5: *"Do not forward ChatGPT cookies, bearer, sentinel or general session
    headers; use an isolated upload request context. The signed URL's own
    authorization is permitted."* The signature is in the URL, so this set
    carries no identity of ours at all.
    """
    return {
        "x-ms-blob-type": "BlockBlob",
        "x-ms-version": "2020-04-08",
        "content-type": content_type,
    }


def assert_no_session_auth(headers: Dict[str, str]) -> None:
    """Fail closed if a session credential ever reaches the signed destination."""
    leaked: List[str] = [
        key
        for key in headers
        if key.lower() in ("authorization", "cookie", "oai-device-id", "oai-session-id")
    ]
    if leaked:
        raise RunnerError(
            RunnerErrorCode.EGRESS_FORBIDDEN,
            f"session headers would be forwarded off-origin: {sorted(leaked)}",
            delivery_state=DeliveryState.NOTHING_SENT,
        )


def probe_facts(bundle: SessionBundle, template: str) -> Tuple[str, Dict[str, Any]]:
    """A non-secret ``(template, facts)`` pair for logs and the envelope."""
    return template, {"template": template, **bundle.summary()}
