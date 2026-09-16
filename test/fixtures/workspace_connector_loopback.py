"""Real loopback listener and privilege-separated client for Amendment D tests.

This is an offline test harness, not per-attempt production composition.  The
client runs with nobody's UID and receives credentials only over private stdin;
its output contains statuses/booleans, never tokens or returned source bodies.
"""

from __future__ import annotations

import asyncio
import socket
from multiprocessing.connection import Connection
from typing import Any

import uvicorn

from cli_agent_orchestrator.workspace_connector.http_server import ConnectorServer


def run_listener(
    server: ConnectorServer, listener: socket.socket, stop: Any, audit: Connection
) -> None:
    async def serve() -> None:
        configured = uvicorn.Config(
            server.build_app(),
            fd=listener.fileno(),
            log_level="critical",
            access_log=False,
            ws="none",
        )
        running = uvicorn.Server(configured)
        task = asyncio.create_task(running.serve(sockets=[listener]))
        while not stop.is_set() and not task.done():
            await asyncio.sleep(0.02)
        running.should_exit = True
        await task

    try:
        asyncio.run(serve())
    finally:
        audit.send(server.audit_projection())
        audit.close()


CLIENT_SCRIPT = r"""
import json, os, urllib.error, urllib.request
configuration = json.load(__import__('sys').stdin)
try:
    with open(configuration['source_path'], 'rb') as source:
        source.read(1)
    source_readable = True
except PermissionError:
    source_readable = False
results = []
for case in configuration['cases']:
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
    if case.get('token'):
        headers['Authorization'] = 'Bearer ' + case['token']
    body = case.get('raw_body')
    if body is None:
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': case.get('tool', 'workspace_info'),
                                     'arguments': case.get('arguments', {})}})
    request = urllib.request.Request(configuration['url'], data=body.encode(), headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        text = response.read().decode()
        content_type = response.headers.get_content_type()
        frames = [line.partition(':')[2].lstrip() for line in text.splitlines()
                  if line.startswith('data:')]
        try:
            data = json.loads(frames[-1] if content_type == 'text/event-stream' else text)
        except (IndexError, json.JSONDecodeError):
            print(json.dumps({'parse_error': case['name'], 'status': response.status,
                              'content_type': content_type, 'bytes': len(text),
                              'frames': len(frames)}))
            raise
        results.append({'name': case['name'], 'status': response.status,
                        'challenge': bool(response.headers.get('WWW-Authenticate')),
                        'is_error': data.get('result', {}).get('isError'),
                        'error': data.get('error'),
                        'canary': configuration['canary'] in text,
                        'scope_refusal': 'requires the' in text and 'scope' in text})
print(json.dumps({'uid': os.geteuid(), 'source_readable': source_readable, 'results': results}))
"""


def redacted_cases(tokens: dict[str, Any]) -> list[dict[str, Any]]:
    cases = [
        {"name": "unauthenticated"},
        {"name": "unauthenticated-malformed", "raw_body": "{"},
        {"name": "invalid-token", "token": "not-a-valid-access-token"},
        {"name": "empty-scope", "token": tokens["empty"]},
        {"name": "wrong-scope", "token": tokens["search"]},
        {"name": "wrong-attempt", "token": tokens["attempt"]},
        {"name": "wrong-manifest", "token": tokens["manifest"]},
        {
            "name": "unlisted-path",
            "token": tokens["read"],
            "tool": "workspace_read_file",
            "arguments": {"path": "outside.txt"},
        },
    ]
    cases.extend(
        {
            "name": f"read-{index}",
            "token": tokens["read"],
            "tool": "workspace_read_file",
            "arguments": {"path": "allowed.txt"},
        }
        for index in range(5)
    )
    return cases
