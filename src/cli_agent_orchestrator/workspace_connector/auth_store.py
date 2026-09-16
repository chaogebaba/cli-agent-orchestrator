"""OAuth token store — port of ``src/auth/store.ts``.

Upstream: XiaoDuoYa/codex-with-chatgpt at commit
8fdd97c188c7678d0d9c43b3769b426940de568a (MIT).  Behavioural port: dynamic
client registration, single-use authorization codes, hashed token records with
owner-only file persistence, refresh-token rotation (the presented record is
deleted and a new pair issued; replay returns ``invalid_grant``), no family id
and no replay-triggered family revocation — upstream parity, deliberately
(D5, r3 review B5).  CAO addition: the access token is bound to the attempt's
allowlisted manifest (D5, r3 N4).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from threading import RLock
from typing import Any

SUPPORTED_SCOPES: tuple[str, ...] = (
    "workspace.read",
    "workspace.search",
    "git.read",
    "offline_access",
)
# CAO scope addition: the attempt binding.  A token without
# ``attempt:<id>`` cannot reach a tool when the server runs attempt-scoped.
ATTEMPT_SCOPE_PREFIX = "attempt:"

ACCESS_TOKEN_TTL_MS = 60 * 60 * 1000  # 1 hour
REFRESH_TOKEN_TTL_MS = 30 * 24 * 60 * 60 * 1000  # 30 days
AUTH_CODE_TTL_MS = 5 * 60 * 1000


def _sha256hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def base64url_sha256(value: str) -> str:
    """PKCE S256: base64url(SHA256(ascii(code_verifier))), no padding."""
    import base64

    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def safe_equal(a: str, b: str) -> bool:
    """Constant-time comparison for equal-length inputs."""
    if len(a) != len(b):
        return False
    return secrets.compare_digest(a, b)


class ClientRegistration:
    def __init__(self, client_id: str, client_name: str | None, redirect_uris: list[str]) -> None:
        self.client_id = client_id
        self.client_name = client_name
        self.redirect_uris = redirect_uris
        self.created_at = time.time()


class AuthorizationCodeRecord:
    def __init__(
        self,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scopes: list[str],
        workspace_id: str,
        pairing_session_id: str,
        resource: str | None,
        expires_at: float,
    ) -> None:
        self.code = code
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.code_challenge = code_challenge
        self.scopes = scopes
        self.workspace_id = workspace_id
        self.pairing_session_id = pairing_session_id
        self.resource = resource
        self.expires_at = expires_at


class TokenRecord:
    def __init__(
        self,
        token_hash: str,
        kind: str,
        client_id: str,
        workspace_id: str,
        scopes: list[str],
        attempt_id: str | None,
        manifest_digest: str | None,
        issued_at: float,
        expires_at: float,
    ) -> None:
        self.hash = token_hash
        self.kind = kind  # "access" | "refresh"
        self.client_id = client_id
        self.workspace_id = workspace_id
        self.scopes = scopes
        self.attempt_id = attempt_id
        self.manifest_digest = manifest_digest
        self.issued_at = issued_at
        self.expires_at = expires_at
        self.revoked = False


class AuthStore:
    """In-memory token store with owner-only JSON persistence (upstream parity)."""

    def __init__(
        self,
        workspace_id: str,
        *,
        attempt_id: str | None = None,
        manifest_digest: str | None = None,
        file: str | Path | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.attempt_id = attempt_id
        self.manifest_digest = manifest_digest
        self._lock = RLock()
        self._clients: dict[str, ClientRegistration] = {}
        self._tokens: dict[str, TokenRecord] = {}
        self._auth_codes: dict[str, AuthorizationCodeRecord] = {}
        if file is not None:
            self.file = Path(file)
        else:
            state_dir = Path(
                os.environ.get("CAO_WORKSPACE_CONNECTOR_STATE_DIR")
                or Path.home() / ".local" / "state" / "cao-workspace-connector"
            )
            auth_dir = state_dir / "auth"
            auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.file = auth_dir / f"{workspace_id}.json"
        self._load()

    # ---- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = time.time()
        for client in raw.get("clients") or []:
            self._clients[client["clientId"]] = ClientRegistration(
                client["clientId"], client.get("clientName"), client.get("redirectUris") or []
            )
        for token in raw.get("tokens") or []:
            if token.get("revoked") or token.get("expiresAt", 0) <= now:
                continue
            rec = TokenRecord(
                token["hash"],
                token["kind"],
                token["clientId"],
                token["workspaceId"],
                token.get("scopes") or [],
                token.get("attemptId"),
                token.get("manifestDigest"),
                token.get("issuedAt", 0),
                token["expiresAt"],
            )
            self._tokens[rec.hash] = rec

    def _save(self) -> None:
        now = time.time()
        state = {
            "clients": [
                {
                    "clientId": c.client_id,
                    "clientName": c.client_name,
                    "redirectUris": c.redirect_uris,
                    "createdAt": c.created_at,
                }
                for c in self._clients.values()
            ],
            "tokens": [
                {
                    "hash": t.hash,
                    "kind": t.kind,
                    "clientId": t.client_id,
                    "workspaceId": t.workspace_id,
                    "scopes": t.scopes,
                    "attemptId": t.attempt_id,
                    "manifestDigest": t.manifest_digest,
                    "issuedAt": t.issued_at,
                    "expiresAt": t.expires_at,
                }
                for t in self._tokens.values()
                if not t.revoked and t.expires_at > now
            ],
        }
        self.file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.file.parent, 0o700)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.file)
        directory_fd = os.open(self.file.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    # ---- Dynamic Client Registration ---------------------------------------

    def register_client(
        self, client_name: str | None, redirect_uris: list[str]
    ) -> ClientRegistration:
        with self._lock:
            client = ClientRegistration(
                f"cao_wc_client_{secrets.token_urlsafe(12)}",
                client_name[:200] if client_name else None,
                list(redirect_uris),
            )
            self._clients[client.client_id] = client
            self._save()
            return client

    def get_client(self, client_id: str) -> ClientRegistration | None:
        with self._lock:
            return self._clients.get(client_id)

    # ---- Authorization codes -------------------------------------------------

    def create_authorization_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scopes: list[str],
        pairing_session_id: str,
        resource: str | None,
    ) -> str:
        with self._lock:
            code = _new_token("cao_wc_ac")
            self._auth_codes[code] = AuthorizationCodeRecord(
                code,
                client_id,
                redirect_uri,
                code_challenge,
                list(scopes),
                self.workspace_id,
                pairing_session_id,
                resource,
                time.time() + AUTH_CODE_TTL_MS / 1000,
            )
            return code

    def consume_authorization_code(self, code: str) -> AuthorizationCodeRecord | None:
        """One-time consumption (upstream parity): a second presentation fails."""
        with self._lock:
            record = self._auth_codes.pop(code, None)
            if record is None:
                return None
            if time.time() > record.expires_at:
                return None
            return record

    # ---- Tokens ---------------------------------------------------------------

    def issue_tokens(
        self,
        *,
        client_id: str,
        scopes: list[str],
        workspace_id: str | None = None,
        attempt_id: str | None = None,
        manifest_digest: str | None = None,
        access_ttl_s: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            ws_id = workspace_id or self.workspace_id
            bound_attempt = attempt_id if attempt_id is not None else self.attempt_id
            bound_manifest = (
                manifest_digest if manifest_digest is not None else self.manifest_digest
            )
            access_ttl = access_ttl_s if access_ttl_s is not None else ACCESS_TOKEN_TTL_MS / 1000

            access_token = _new_token("cao_wc_at")
            self._tokens[_sha256hex(access_token)] = TokenRecord(
                _sha256hex(access_token),
                "access",
                client_id,
                ws_id,
                list(scopes),
                bound_attempt,
                bound_manifest,
                now,
                now + access_ttl,
            )

            refresh_token: str | None = None
            if "offline_access" in scopes:
                refresh_token = _new_token("cao_wc_rt")
                self._tokens[_sha256hex(refresh_token)] = TokenRecord(
                    _sha256hex(refresh_token),
                    "refresh",
                    client_id,
                    ws_id,
                    list(scopes),
                    bound_attempt,
                    bound_manifest,
                    now,
                    now + REFRESH_TOKEN_TTL_MS / 1000,
                )
            self._save()
            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": int(access_ttl),
                "scopes": list(scopes),
            }

    def verify_access_token(self, token: str) -> tuple[bool, TokenRecord | None, str]:
        """Return (ok, record, reason)."""
        with self._lock:
            record = self._tokens.get(_sha256hex(token))
            if record is None:
                return False, None, "unknown"
            if record.kind != "access":
                return False, None, "wrong_kind"
            if record.revoked:
                return False, None, "revoked"
            if time.time() > record.expires_at:
                return False, None, "expired"
            return True, record, ""

    def refresh(
        self, refresh_token: str, client_id: str
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """Refresh rotation (upstream parity): the old token is deleted, a new pair issued.

        Replay of the old token returns ``invalid_grant`` — the whole inherited
        contract; there is no family id and no replay-triggered revocation of
        the newly issued pair (D5, r3 review B5).
        """
        with self._lock:
            record = self._tokens.get(_sha256hex(refresh_token))
            if record is None or record.kind != "refresh":
                return False, None, "invalid_grant"
            if record.revoked or time.time() > record.expires_at:
                return False, None, "invalid_grant"
            if record.client_id != client_id:
                return False, None, "invalid_client"
            del self._tokens[record.hash]
            tokens = self.issue_tokens(
                client_id=client_id,
                scopes=record.scopes,
                workspace_id=record.workspace_id,
                attempt_id=record.attempt_id,
                manifest_digest=record.manifest_digest,
            )
            return True, tokens, ""

    def revoke_token(self, token: str) -> bool:
        with self._lock:
            record = self._tokens.pop(_sha256hex(token), None)
            if record is None:
                return False
            self._save()
            return True

    def token_count(self) -> int:
        with self._lock:
            return len(self._tokens)


def filter_scopes(requested: str | None) -> list[str]:
    """Grant the intersection of requested and supported scopes (upstream parity).

    CAO difference: unsupported scopes are dropped (never blanket-granted), and
    there is no ``execution.read`` (the execution tools are not ported).
    """
    if not requested or not requested.strip():
        return list(SUPPORTED_SCOPES)
    asked = [s for s in re_split(requested) if s]
    granted = [s for s in asked if s in SUPPORTED_SCOPES]
    return granted if granted else []


def re_split(value: str) -> list[str]:
    import re

    return re.split(r"[\s+]+", value)
