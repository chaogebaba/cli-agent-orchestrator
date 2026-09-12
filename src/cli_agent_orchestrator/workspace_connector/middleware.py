"""Bearer-token guard for ``/mcp`` — port of ``src/auth/middleware.ts``.

Upstream: XiaoDuoYa/codex-with-chatgpt at commit
8fdd97c188c7678d0d9c43b3769b426940de568a (MIT).  Behavioural port: a missing,
invalid or expired token is refused 401 with a ``WWW-Authenticate`` challenge
pointing at the protected-resource metadata; a valid token for another
workspace is refused 403.  CAO addition (D5): the token must also carry the
attempt binding the server is serving; a cross-attempt token is refused with
``PATH_NOT_IN_MANIFEST`` semantics at the tool layer and 403 here when its
attempt differs.
"""

from __future__ import annotations

from cli_agent_orchestrator.workspace_connector.auth_store import AuthStore

REALM = "cao-workspace-connector"


class BearerVerdict:
    def __init__(
        self,
        *,
        ok: bool,
        status: int = 200,
        token: str | None = None,
        client_id: str | None = None,
        scopes: tuple[str, ...] = (),
        expires_at: float | None = None,
        body: dict[str, object] | None = None,
        www_authenticate: str | None = None,
    ) -> None:
        self.ok = ok
        self.status = status
        self.token = token
        self.client_id = client_id
        self.scopes = scopes
        self.expires_at = expires_at
        self.body = body
        self.www_authenticate = www_authenticate


def bearer_auth(
    *,
    store: AuthStore,
    workspace_id: str,
    base_url: str,
    authorization_header: str | None,
    attempt_id: str | None = None,
    manifest_digest: str | None = None,
) -> BearerVerdict:
    """Evaluate the bearer guard; framework-agnostic (the HTTP layer adapts)."""

    def challenge(error: str, description: str) -> str:
        return (
            f'Bearer realm="{REALM}", error="{error}", error_description="{description}", '
            f'resource_metadata="{base_url}/.well-known/oauth-protected-resource/mcp"'
        )

    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        return BearerVerdict(
            ok=False,
            status=401,
            www_authenticate=challenge("invalid_token", "Missing bearer token"),
            body={"error": "unauthorized", "error_description": "Authentication required"},
        )
    token = authorization_header[7:].strip()
    ok, record, reason = store.verify_access_token(token)
    if not ok:
        return BearerVerdict(
            ok=False,
            status=401,
            www_authenticate=challenge("invalid_token", f"Token {reason}"),
            body={"error": "unauthorized", "error_description": f"Token {reason}"},
        )
    assert record is not None
    if record.workspace_id != workspace_id:
        return BearerVerdict(
            ok=False,
            status=403,
            body={
                "error": "forbidden",
                "error_description": ("This token is not authorized for the connected workspace"),
            },
        )
    if attempt_id is not None or manifest_digest is not None:
        if record.attempt_id != attempt_id or record.manifest_digest != manifest_digest:
            return BearerVerdict(
                ok=False,
                status=403,
                body={
                    "error": "PATH_NOT_IN_MANIFEST",
                    "error_description": (
                        "This token is bound to another attempt's source manifest"
                    ),
                },
            )
    return BearerVerdict(
        ok=True,
        token=token,
        client_id=record.client_id,
        scopes=tuple(record.scopes),
        expires_at=record.expires_at,
    )
