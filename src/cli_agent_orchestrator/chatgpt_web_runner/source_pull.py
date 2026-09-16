"""Attempt-scoped pull plane: loopback connector + AC-33 correlation (D5/D7).

Amendment D replaces pushed review bundles with a PULL plane: the model reads
the reviewed source through the read-only workspace connector instead of being
handed an attachment. Three things have to be true for a findings run to publish,
and this module owns all three:

1. **The connector exists as a real listener, not an ASGI factory.** One attempt
   binds one :class:`ConnectorListener` on loopback with an OS-assigned port. It
   is separately killable (its own uvicorn server object and task) so the runner
   can take the pull plane down without touching the browser, and it never
   outlives the attempt.

2. **The model actually pulled sources.** The runner does not read the files for
   the model; it verifies, from the connector's own digest-only audit projection,
   that at least two distinct manifest paths were read.

3. **The answer is correlated to what was pulled.** AC-33: the accepted
   conversation branch must carry the exact result digests of the sources that
   were read, on ONE reusable access token, inside ONE generation. A missing
   audit, a substituted digest, a denied source, a second token or the wrong
   branch each BLOCK publication — they never degrade to a warning.

The verifier is deliberately offline and pure: it consumes the projection plus
the accepted answer, so every failure mode has an offline arm and the live turn
exercises the same code path the arms do.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)

#: AC-33: "a live findings turn pulls **at least two** canary-bearing files".
MINIMUM_PULLED_SOURCES = 2

#: Tools whose audit rows count as a SOURCE read. ``workspace_info`` returns
#: only the summary (no file bytes), so it can never satisfy AC-33's minimum.
_SOURCE_TOOLS = frozenset(
    {
        "workspace_read_file",
        "workspace_search",
        "workspace_git_diff",
    }
)


@dataclass(frozen=True)
class PulledSource:
    """One manifest path the model read, and the digest the connector returned."""

    path: str
    digest: str
    token_fingerprint: str


@dataclass(frozen=True)
class SourcePullEvidence:
    """What the connector observed for one attempt, bodies excluded."""

    attempt_id: str
    sources: tuple[PulledSource, ...]
    refusals: tuple[dict[str, Any], ...]
    token_fingerprints: tuple[str, ...]

    @property
    def digests(self) -> tuple[str, ...]:
        return tuple(source.digest for source in self.sources)


def collect_pull_evidence(audit_rows: Sequence[dict[str, Any]]) -> SourcePullEvidence:
    """Project the connector's audit rows into the verifier's input.

    Only successful source reads become :class:`PulledSource`; refusals are kept
    separately because a denied source is a publication blocker, not an absence.
    """
    attempt_ids = {str(row.get("attempt_id") or "") for row in audit_rows}
    sources: list[PulledSource] = []
    refusals: list[dict[str, Any]] = []
    fingerprints: list[str] = []
    for row in audit_rows:
        tool = str(row.get("tool") or "")
        digest = row.get("result_digest")
        fingerprint = str(row.get("token_fingerprint") or "")
        if row.get("refusal_code"):
            refusals.append(dict(row))
            continue
        if tool not in _SOURCE_TOOLS or not isinstance(digest, str) or not digest:
            continue
        sources.append(
            PulledSource(
                path=str(row.get("subject") or ""),
                digest=digest,
                token_fingerprint=fingerprint,
            )
        )
        if fingerprint and fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
    return SourcePullEvidence(
        attempt_id=next(iter(sorted(attempt_ids)), ""),
        sources=tuple(sources),
        refusals=tuple(refusals),
        token_fingerprints=tuple(fingerprints),
    )


def _blocked(reason: str) -> RunnerError:
    return RunnerError(
        RunnerErrorCode.SOURCE_CORRELATION,
        reason,
        delivery_state=DeliveryState.DELIVERED,
    )


def verify_source_correlation(
    evidence: SourcePullEvidence,
    *,
    answer_text: str,
    observed_branch_digest: str,
    accepted_branch_digest: str,
    minimum_sources: int = MINIMUM_PULLED_SOURCES,
) -> tuple[PulledSource, ...]:
    """AC-33. Raise unless the answer is correlated to what was actually pulled.

    Returns the correlated sources so the caller can record them. Every branch
    below is a *refusal to publish*; none of them is recoverable inside the
    attempt, because a second turn would be a second generation.
    """
    # Wrong branch: the digest the GET accepted is not the branch the audit was
    # correlated against. Nothing downstream is trustworthy after this.
    if not accepted_branch_digest or not observed_branch_digest:
        raise _blocked("no canonical branch digest to correlate the pulled sources against")
    if observed_branch_digest != accepted_branch_digest:
        raise _blocked(
            "the accepted branch is not the branch the sources were correlated against "
            "(conversation branch digest mismatch)"
        )

    # Missing audit: the connector saw nothing, so no source claim is supported.
    if not evidence.sources:
        raise _blocked(
            "the connector audit projection records no source read for this attempt; "
            "a findings answer that cites sources it never pulled is not publishable"
        )

    # Denied source: a refusal means the model asked for something the manifest
    # did not grant. Publishing an answer built around a denial is AC-33's
    # "denied source ... prevents publication".
    if evidence.refusals:
        codes = sorted({str(row.get("refusal_code")) for row in evidence.refusals})
        raise _blocked(f"the connector refused a source read during this attempt: {codes}")

    distinct_paths = {source.path for source in evidence.sources}
    if len(distinct_paths) < minimum_sources:
        raise _blocked(
            f"AC-33 requires at least {minimum_sources} distinct sources pulled through the "
            f"connector; the audit shows {len(distinct_paths)}"
        )

    # One reusable access token inside one generation. Two fingerprints mean two
    # tokens, which means the single-generation invariant was not observed.
    if len(evidence.token_fingerprints) != 1:
        raise _blocked(
            "AC-33 requires one reusable access token for the whole generation; the audit "
            f"shows {len(evidence.token_fingerprints)} distinct access tokens"
        )

    # Substituted digest: every digest the connector recorded must appear in the
    # accepted answer, so the branch cannot cite a result the connector did not
    # produce, and a rewritten digest cannot pass as the real one.
    missing = [source for source in evidence.sources if source.digest not in answer_text]
    if missing:
        raise _blocked(
            "the accepted answer does not carry the exact connector result digest for "
            f"{sorted(source.path for source in missing)}"
        )
    return evidence.sources


class ConnectorListener:
    """One attempt's loopback-bound connector, separately killable.

    Owns its socket, its uvicorn server and its serving task. ``stop()`` is
    idempotent and always runs in the attempt's teardown, so the pull plane can
    never outlive the attempt that authorised it.
    """

    def __init__(self, server: Any, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = server
        self._host = host
        self._port = port
        self._socket: Optional[socket.socket] = None
        self._uvicorn: Any = None
        self._task: "Optional[asyncio.Task[Any]]" = None
        self.base_url: str = ""

    async def start(self) -> str:
        """Bind loopback, start serving, and return the base URL."""
        import uvicorn

        from cli_agent_orchestrator.api.routes_chatgpt_web_connector import build_connector_app

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._host, self._port))
        listener.listen(64)
        self._socket = listener
        bound_host, bound_port = listener.getsockname()[:2]
        # Loopback only. The operator's HTTPS tunnel is a reachability surface,
        # never an auth boundary (D5): the bearer guard is the boundary.
        self.base_url = f"http://{bound_host}:{bound_port}"
        configured = uvicorn.Config(
            build_connector_app(self._server),
            fd=listener.fileno(),
            log_level="critical",
            access_log=False,
            ws="none",
        )
        self._uvicorn = uvicorn.Server(configured)
        self._task = asyncio.ensure_future(self._uvicorn.serve(sockets=[listener]))
        for _ in range(500):
            if getattr(self._uvicorn, "started", False):
                break
            if self._task.done():
                await self._task
                raise RuntimeError("connector listener exited before it started")
            await asyncio.sleep(0.01)
        return self.base_url

    async def stop(self) -> None:
        """Take the pull plane down. Idempotent; never raises."""
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:  # pragma: no cover - teardown must not mask a run
                pass
        self._task = None
        self._uvicorn = None
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:  # pragma: no cover
                pass
            self._socket = None

    async def __aenter__(self) -> "ConnectorListener":
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.stop()


def call_tool(
    base_url: str,
    token: str,
    name: str,
    arguments: dict[str, Any],
    *,
    request_id: int = 1,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """One Streamable-HTTP MCP ``tools/call`` against the loopback connector.

    Synchronous on purpose: callers run it in a worker thread so it never shares
    the event loop that is serving the listener.
    """
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    ).encode()
    request = urllib.request.Request(
        # The TRAILING SLASH matters: the connector mounts the MCP app at
        # ``/mcp`` and Starlette answers a bare ``/mcp`` POST with a 307 to
        # ``/mcp/``. Starlette's TestClient follows that silently; a real HTTP
        # client does not carry the body through, so it must be addressed
        # directly.
        f"{base_url.rstrip('/')}/mcp/",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
    )
    response: Any
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        # An HTTPError IS the response for a refusal; the connector's typed
        # refusals arrive with a non-2xx status and a JSON body.
        response = error
    with response:
        text = response.read().decode()
        content_type = response.headers.get_content_type()
    if content_type == "text/event-stream":
        frames = [line.partition(":")[2].lstrip() for line in text.splitlines()]
        frames = [frame for frame in frames if frame]
        text = frames[-1] if frames else "{}"
    try:
        return dict(json.loads(text))
    except json.JSONDecodeError:
        return {"error": {"message": "connector returned a non-JSON body"}}


#: The ephemeral pairing-code file's name inside the attempt directory.
PAIRING_CODE_FILENAME = "pairing.code"


class PairingCodeFile:
    """The raw pairing code's only on-disk home: 0600, and short-lived.

    The durable ledger refuses raw secrets, so the operator's copy lives here
    instead — next to the attempt, owner-only, and removed as soon as it stops
    being useful. "Stops being useful" is not a timer: the file is unlinked when
    the pairing session is no longer active, which is either because a client
    consumed the code or because it expired. A consumed code left on disk is a
    credential nobody is watching any more.

    ``revoke()`` is idempotent and never raises, so teardown can always call it.
    """

    def __init__(self, attempt_dir: Path, pairing: Any) -> None:
        self.path = Path(attempt_dir) / PAIRING_CODE_FILENAME
        self._pairing = pairing
        self._watcher: "Optional[asyncio.Task[None]]" = None

    def write(self, code: str) -> str:
        """Write the code 0600 and return its SHA-256, which the ledger keeps."""
        import hashlib
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL: never adopt a file someone else left here; 0o600 at creation,
        # so there is no window in which it exists with a wider mode.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(self.path), flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(code + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(self.path), 0o600)
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    def revoke(self) -> None:
        """Remove the file. Idempotent; safe in a finally."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - teardown must not mask a run
            pass

    def start_watch(self, *, interval: float = 1.0) -> "asyncio.Task[None]":
        """Unlink the file as soon as the pairing stops being active."""

        async def _watch() -> None:
            try:
                while True:
                    if not self._pairing.has_active_session():
                        self.revoke()
                        return
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:  # pragma: no cover - teardown path
                raise

        self._watcher = asyncio.ensure_future(_watch())
        return self._watcher

    async def stop_watch(self) -> None:
        """Cancel the watcher and remove the file unconditionally."""
        watcher = self._watcher
        self._watcher = None
        if watcher is not None and not watcher.done():
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        self.revoke()
