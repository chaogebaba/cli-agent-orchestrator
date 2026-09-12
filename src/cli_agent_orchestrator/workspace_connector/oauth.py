"""OAuth 2.1 authorization-code + PKCE S256 + DCR — port of ``src/auth/oauth.ts``.

Upstream: XiaoDuoYa/codex-with-chatgpt at commit
8fdd97c188c7678d0d9c43b3769b426940de568a (MIT).  Behavioural port: RFC 8414
authorization-server metadata, RFC 9728 protected-resource metadata, RFC 7591
dynamic client registration with redirect validation (https, or loopback http),
the authorization endpoint (PKCE S256 mandatory, single-use pairing gate), the
token endpoint (single-use authorization code, PKCE verification, refresh
rotation with ``invalid_grant`` on replay) and RFC 7009 revocation.  The pairing
page is re-authored minimal in CAO (upstream ``src/auth/html.ts`` is not
ported); it renders no user data beyond the workspace name and scope list.
"""

from __future__ import annotations

import html
import secrets
import time
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cli_agent_orchestrator.workspace_connector.auth_store import (
    AuthStore,
    base64url_sha256,
    filter_scopes,
    safe_equal,
)
from cli_agent_orchestrator.workspace_connector.pairing import PairingManager

PRODUCT_NAME = "CAO Workspace Connector"


def is_allowed_redirect_uri(uri: str) -> bool:
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
        return True
    return False


def authorization_server_metadata(base: str) -> dict[str, object]:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [
            "workspace.read",
            "workspace.search",
            "git.read",
            "offline_access",
        ],
    }


def protected_resource_metadata(base: str) -> dict[str, object]:
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": [
            "workspace.read",
            "workspace.search",
            "git.read",
            "offline_access",
        ],
        "bearer_methods_supported": ["header"],
        "resource_name": PRODUCT_NAME,
    }


def _pairing_page(
    request_id: str, workspace_name: str, scopes: list[str], error: str | None = None
) -> str:
    """Minimal re-authored pairing page (upstream ``auth/html.ts`` not ported)."""
    scope_list = "".join(f"<li>{html.escape(s)}</li>" for s in scopes)
    error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(PRODUCT_NAME)}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, sans-serif; display: flex; align-items: center;
       justify-content: center; min-height: 100vh; margin: 0; }}
.card {{ border: 1px solid #888; border-radius: 12px; padding: 32px; max-width: 420px; width: 90%; }}
input[type=text] {{ width: 100%; box-sizing: border-box; font-size: 24px; letter-spacing: 4px;
       text-align: center; text-transform: uppercase; padding: 12px; }}
button {{ width: 100%; margin-top: 16px; padding: 12px; font-size: 16px; }}
.error {{ color: #d70015; }}
</style></head>
<body><div class="card">
<h1>{html.escape(PRODUCT_NAME)}</h1>
<p>ChatGPT is requesting read-only access to workspace <strong>{html.escape(workspace_name)}</strong>:</p>
<ul>{scope_list}</ul>
<form method="POST" action="authorize">
<input type="hidden" name="request_id" value="{html.escape(request_id)}">
<input type="text" name="pairing_code" placeholder="XXXX-XXXX" autocomplete="one-time-code"
       autofocus maxlength="9" required>
{error_html}
<button type="submit">Connect</button>
</form>
<p>The pairing code was generated on this computer. It expires in a few minutes.</p>
</div></body></html>"""


class OAuthService:
    """Stateless handler backend for the OAuth routes (framework-agnostic).

    The HTTP layer (``http_server.py``) adapts requests/responses to these
    methods; keeping the policy here makes it testable without any server.
    """

    def __init__(
        self,
        *,
        store: AuthStore,
        pairing: PairingManager,
        workspace_name: str,
        request_ttl_s: float = 10 * 60,
    ) -> None:
        self.store = store
        self.pairing = pairing
        self.workspace_name = workspace_name
        self.pending_requests: dict[str, dict[str, object]] = {}
        self.request_ttl_s = request_ttl_s

    def prune_pending(self) -> None:
        now = time.time()
        expired = [
            k
            for k, v in self.pending_requests.items()
            if now > float(cast(float | int, v.get("expiresAt", 0)))
        ]
        for rid in expired:
            del self.pending_requests[rid]

    # ---- Dynamic Client Registration (RFC 7591) ------------------------------

    def register(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        redirect_uris_raw = body.get("redirect_uris")
        redirect_uris = (
            [u for u in redirect_uris_raw if isinstance(u, str)]
            if isinstance(redirect_uris_raw, list)
            else []
        )
        if not redirect_uris or not all(is_allowed_redirect_uri(u) for u in redirect_uris):
            return 400, {
                "error": "invalid_redirect_uri",
                "error_description": (
                    "redirect_uris must be https URLs (or http://localhost for development)"
                ),
            }
        client_name_raw = body.get("client_name")
        client_name = client_name_raw[:200] if isinstance(client_name_raw, str) else None
        client = self.store.register_client(client_name, redirect_uris)
        return 201, {
            "client_id": client.client_id,
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }

    # ---- Authorization endpoint -----------------------------------------------

    def authorize_get(self, query: dict[str, str]) -> tuple[int, dict[str, str], str]:
        """Return (status, headers, body).  200 = pairing page; 302 = redirect."""
        self.prune_pending()
        client = (
            self.store.get_client(query.get("client_id", "")) if query.get("client_id") else None
        )
        if not client:
            return (
                400,
                {"content-type": "text/html; charset=utf-8"},
                ("Unknown client. Please reconnect from ChatGPT."),
            )
        redirect_uri = query.get("redirect_uri")
        if not redirect_uri or redirect_uri not in client.redirect_uris:
            return 400, {"content-type": "text/html; charset=utf-8"}, "Invalid redirect_uri."

        def fail(error: str, description: str) -> tuple[int, dict[str, str], str]:
            url = _add_params(
                redirect_uri,
                {"error": error, "error_description": description},
                state=query.get("state"),
            )
            return 302, {"location": url}, ""

        if query.get("response_type") != "code":
            return fail("unsupported_response_type", "Only response_type=code is supported")
        if not query.get("code_challenge") or query.get("code_challenge_method") != "S256":
            return fail("invalid_request", "PKCE with S256 is required")

        scopes = filter_scopes(query.get("scope"))
        request: dict[str, object] = {
            "id": secrets.token_hex(16),
            "clientId": client.client_id,
            "redirectUri": redirect_uri,
            "scopes": scopes,
            "state": query.get("state"),
            "codeChallenge": query["code_challenge"],
            "resource": query.get("resource"),
            "expiresAt": time.time() + self.request_ttl_s,
        }
        self.pending_requests[str(request["id"])] = request
        body = _pairing_page(str(request["id"]), self.workspace_name, scopes)
        return 200, {"content-type": "text/html; charset=utf-8"}, body

    def authorize_post(
        self, form: dict[str, str], ip: str | None = None
    ) -> tuple[int, dict[str, str], str]:
        self.prune_pending()
        request = self.pending_requests.get(form.get("request_id", ""))
        if not request:
            return (
                400,
                {"content-type": "text/html; charset=utf-8"},
                ("This authorization request has expired. Please reconnect from ChatGPT."),
            )
        verdict = self.pairing.verify(form.get("pairing_code") or "", ip)
        if not verdict.get("ok"):
            reason = str(verdict.get("reason"))
            messages = {
                "invalid": "Incorrect pairing code.",
                "expired": "This pairing code has expired. Ask for a new one.",
                "too_many_attempts": "Too many incorrect attempts. Ask for a new pairing code.",
                "rate_limited": "Too many attempts. Please wait a minute and try again.",
                "no_active_session": "No active pairing session. Ask for a new pairing code.",
            }
            body = _pairing_page(
                str(request["id"]),
                self.workspace_name,
                [
                    str(scope)
                    for scope in cast(list[object], request["scopes"])
                    if isinstance(scope, str)
                ],
                messages.get(reason, "Verification failed."),
            )
            status = 401 if reason == "invalid" else 410
            return status, {"content-type": "text/html; charset=utf-8"}, body
        del self.pending_requests[str(request["id"])]
        code = self.store.create_authorization_code(
            client_id=str(request["clientId"]),
            redirect_uri=str(request["redirectUri"]),
            code_challenge=str(request["codeChallenge"]),
            scopes=[
                str(scope)
                for scope in cast(list[object], request["scopes"])
                if isinstance(scope, str)
            ],
            pairing_session_id=str(verdict.get("session_id")),
            resource=str(request["resource"]) if request.get("resource") else None,
        )
        url = _add_params(
            str(request["redirectUri"]),
            {"code": code},
            state=str(request["state"]) if request.get("state") else None,
        )
        return 302, {"location": url}, ""

    # ---- Token endpoint ----------------------------------------------------------

    def token(self, form: dict[str, str]) -> tuple[int, dict[str, object]]:
        grant_type = form.get("grant_type")

        if grant_type == "authorization_code":
            code = form.get("code")
            code_verifier = form.get("code_verifier")
            client_id = form.get("client_id")
            redirect_uri = form.get("redirect_uri")
            if not code or not code_verifier or not client_id:
                return 400, {"error": "invalid_request"}
            record = self.store.consume_authorization_code(code)
            if record is None or record.client_id != client_id:
                return 400, {"error": "invalid_grant"}
            if redirect_uri and redirect_uri != record.redirect_uri:
                return 400, {"error": "invalid_grant", "error_description": "redirect_uri mismatch"}
            if not safe_equal(base64url_sha256(code_verifier), record.code_challenge):
                return 400, {
                    "error": "invalid_grant",
                    "error_description": "PKCE verification failed",
                }
            tokens = self.store.issue_tokens(client_id=client_id, scopes=record.scopes)
            return 200, {
                "access_token": tokens["access_token"],
                "token_type": "Bearer",
                "expires_in": tokens["expires_in"],
                "refresh_token": tokens["refresh_token"],
                "scope": " ".join(tokens["scopes"]),
            }

        if grant_type == "refresh_token":
            refresh_token = form.get("refresh_token")
            client_id = form.get("client_id")
            if not refresh_token or not client_id:
                return 400, {"error": "invalid_request"}
            ok, refreshed_tokens, reason = self.store.refresh(refresh_token, client_id)
            if not ok:
                return 400, {"error": reason}
            if refreshed_tokens is None:
                return 400, {"error": "invalid_grant"}
            return 200, {
                "access_token": refreshed_tokens["access_token"],
                "token_type": "Bearer",
                "expires_in": refreshed_tokens["expires_in"],
                "refresh_token": refreshed_tokens["refresh_token"],
                "scope": " ".join(refreshed_tokens["scopes"]),
            }

        return 400, {"error": "unsupported_grant_type"}

    # ---- Revocation (RFC 7009) ------------------------------------------------------

    def revoke(self, form: dict[str, str]) -> tuple[int, dict[str, object]]:
        token = form.get("token")
        if token:
            self.store.revoke_token(token)
        return 200, {}


def _add_params(uri: str, params: dict[str, str], state: str | None = None) -> str:
    scheme, netloc, path, query, fragment = urlsplit(uri)
    pairs = parse_qsl(query, keep_blank_values=True)
    pairs.extend(params.items())
    if state:
        pairs.append(("state", state))
    return urlunsplit((scheme, netloc, path, urlencode(pairs), fragment))


def parse_form(body: str) -> dict[str, str]:
    return dict(parse_qsl(body, keep_blank_values=True))
