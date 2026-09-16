"""Loopback HTTPS fake origin for F862 Amendment D lifecycle arms.

The deterministic page issues the same routed conversation path the production
handler matches. The server records receipt before it emits or resets a
response, so its ledger is independent of Playwright response/failed events and
can adjudicate whether the browser copy actually reached the origin.
"""

from __future__ import annotations

import hashlib
import json
import socket
import ssl
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

DETERMINISTIC_PAGE = b"""<!doctype html>
<meta charset=utf-8><title>F862 fake origin</title>
<button id=send>send</button><pre id=result></pre>
<script>
const body = {messages:[{id:"fake-user",author:{role:"user"},content:{content_type:"text",parts:["fixture"]}}]};
window.issueConversation = async () => {
  const response = await fetch('/backend-api/f/conversation', {
    method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)
  });
  document.querySelector('#result').textContent = await response.text();
};
document.querySelector('#send').addEventListener('click', window.issueConversation);
</script>"""

DEFAULT_SSE = b'event: delta_encoding\ndata: "v1"\n\ndata: [DONE]\n\n'


@dataclass(frozen=True)
class OriginReceipt:
    sequence: int
    method: str
    path: str
    body_sha256: str
    body_bytes: int
    received_at: float
    response_mode: str


class OriginReceiptLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[OriginReceipt] = []

    def append(self, *, method: str, path: str, body: bytes, response_mode: str) -> None:
        with self._lock:
            self._rows.append(
                OriginReceipt(
                    sequence=len(self._rows) + 1,
                    method=method,
                    path=path,
                    body_sha256=hashlib.sha256(body).hexdigest(),
                    body_bytes=len(body),
                    received_at=time.time(),
                    response_mode=response_mode,
                )
            )

    def snapshot(self) -> tuple[OriginReceipt, ...]:
        with self._lock:
            return tuple(self._rows)


class FakeOrigin(AbstractContextManager["FakeOrigin"]):
    """Own one TLS loopback server and its independent receipt ledger."""

    def __init__(
        self,
        directory: Path,
        *,
        response_mode: str = "complete",
        sse_body: bytes = DEFAULT_SSE,
        conversation_body: Optional[dict[str, Any]] = None,
    ) -> None:
        if response_mode not in {"complete", "reset", "slow_sse"}:
            raise ValueError("response_mode must be complete, reset or slow_sse")
        self.directory = directory
        self.response_mode = response_mode
        self.sse_body = sse_body
        self.conversation_body = conversation_body or {
            "conversation_id": "fake-conversation",
            "current_node": None,
            "mapping": {},
        }
        self.ledger = OriginReceiptLedger()
        #: ``slow_sse`` only. The server writes the first SSE chunk, opens
        #: ``first_chunk_sent``, then blocks on ``release_stream`` before
        #: writing the rest. That makes "tear the browser down MID-STREAM" an
        #: exact instant the test chooses rather than a sleep race.
        self.first_chunk_sent = threading.Event()
        self.release_stream = threading.Event()
        #: ``slow_sse`` only. Counts conversation GETs, so the during-GET arm
        #: can prove the poll really was in flight.
        self.get_gate_reached = threading.Event()
        self.release_get = threading.Event()
        self.gate_timeout_s = 30.0
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.cert_path: Optional[Path] = None

    @property
    def origin(self) -> str:
        if self.server is None:
            raise RuntimeError("fake origin is not running")
        _host, port = self.server.server_address[:2]
        return f"https://localhost:{port}"

    def __enter__(self) -> "FakeOrigin":
        cert, key = _write_loopback_certificate(self.directory)
        self.cert_path = cert
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                if self.path == "/":
                    self._reply(200, "text/html; charset=utf-8", DETERMINISTIC_PAGE)
                    return
                if self.path.startswith("/backend-api/conversation/"):
                    owner.get_gate_reached.set()
                    if owner.response_mode == "slow_sse":
                        owner.release_get.wait(owner.gate_timeout_s)
                    body = json.dumps(owner.conversation_body, separators=(",", ":")).encode()
                    self._reply(200, "application/json", body)
                    return
                self._reply(404, "text/plain", b"not found")

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length)
                owner.ledger.append(
                    method="POST",
                    path=self.path,
                    body=body,
                    response_mode=owner.response_mode,
                )
                if owner.response_mode == "reset":
                    # Receipt has already been committed independently. Closing
                    # before response headers makes browsers report requestfailed.
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                if owner.response_mode == "slow_sse":
                    self._reply_slow_sse(owner.sse_body)
                    return
                self._reply(200, "text/event-stream", owner.sse_body)

            def _reply_slow_sse(self, body: bytes) -> None:
                """Chunked SSE that pauses, on command, after its first frame."""
                head, _, tail = body.partition(b"\n\n")
                first = head + b"\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self._write_chunk(first)
                owner.first_chunk_sent.set()
                owner.release_stream.wait(owner.gate_timeout_s)
                if tail:
                    self._write_chunk(tail)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                self.close_connection = True

            def _write_chunk(self, payload: bytes) -> None:
                self.wfile.write(f"{len(payload):x}\r\n".encode())
                self.wfile.write(payload)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            def _reply(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


def _write_loopback_certificate(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path = directory / "fake-origin.key"
    cert_path = directory / "fake-origin.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path
