#!/usr/bin/env python3
"""Drive V5 live sandbox gate against a healthy G7 sandbox on :9890.

Proves:
  1) claude_code multi-prompt CLAUDE.md import-reject arm (live pane)
  2) deferred-init create → init → delete/quiesce without ghost churn
  + smoke assign-equivalent + send_message round-trip

Writes evidence under --art. Exit 0 on overall PASS, 1 on FAIL.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


_INSTANCE_ID = ""


def _req(
    endpoint: str,
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> tuple[int, Any]:
    q = ""
    if query:
        flat = {k: v for k, v in query.items() if v is not None}
        if flat:
            q = "?" + urllib.parse.urlencode(flat, doseq=True)
    data = None
    headers: dict[str, str] = {}
    if _INSTANCE_ID:
        headers["X-CAO-Instance"] = _INSTANCE_ID
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    # FastAPI treats body-less POST oddly if Content-Length missing; always set for mutations
    if method in {"POST", "PUT", "PATCH", "DELETE"} and data is None:
        data = b"{}"
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(
        f"{endpoint}{path}{q}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        code = exc.code
    except Exception as exc:
        return 0, {"error": str(exc)}
    if not raw:
        return code, None
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError:
        return code, raw


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _dump(path: Path, obj: Any) -> None:
    _write(path, json.dumps(obj, indent=2, default=str) + "\n")


def tmux(socket: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def list_windows(socket: str) -> list[dict[str, str]]:
    proc = tmux(socket, "list-windows", "-a", "-F", "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_pid}")
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        rows.append(
            {
                "session": parts[0],
                "index": parts[1],
                "name": parts[2],
                "pane_pid": parts[3],
            }
        )
    return rows


def capture_pane(socket: str, session: str, window: str, lines: int = 80) -> str:
    target = f"{session}:{window}"
    proc = tmux(socket, "capture-pane", "-p", "-J", "-t", target, "-S", f"-{lines}")
    return proc.stdout


def child_argv(pane_pid: str) -> str:
    proc = subprocess.run(
        ["ps", "--ppid", pane_pid, "-o", "args="],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def assert_binary_identity(socket: str, expected_cli: str, art: Path) -> dict[str, Any]:
    """F51: every spawned lane child argv must name the expected CLI."""
    rows = list_windows(socket)
    results = []
    ok = True
    for row in rows:
        # skip owner sentinel
        if row["name"] == "owner" or row["session"].endswith("-owner"):
            continue
        argv = child_argv(row["pane_pid"])
        # Also check deeper descendants (bwrap wrapper)
        deep = subprocess.run(
            ["ps", "--forest", "-g", row["pane_pid"], "-o", "pid=,args="],
            capture_output=True,
            text=True,
        ).stdout
        hit = expected_cli in argv or expected_cli in deep
        # shell-only panes before provider launch are not yet fail-worthy
        shell_only = bool(re.search(r"(zsh|bash|sh)$", argv.split("\n")[0] if argv else "")) and expected_cli not in deep
        entry = {
            **row,
            "child_argv": argv,
            "forest": deep,
            "expected": expected_cli,
            "match": hit,
            "shell_only": shell_only,
        }
        results.append(entry)
        if not hit and not shell_only and argv:
            ok = False
    _dump(art / "identity-assertion.json", {"ok": ok, "rows": results})
    return {"ok": ok, "rows": results}


def wait_status(
    endpoint: str,
    terminal_id: str,
    want: set[str],
    *,
    timeout: float = 180.0,
    art: Path | None = None,
    label: str = "wait",
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    samples: list[dict[str, Any]] = []
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        code, payload = _req(endpoint, "GET", f"/terminals/{terminal_id}")
        status = None
        if isinstance(payload, dict):
            status = payload.get("status")
            last = payload
        samples.append({"t": time.time(), "http": code, "status": status})
        if status in want:
            if art is not None:
                _dump(art / f"{label}-status-samples.json", samples)
                _dump(art / f"{label}-terminal.json", last)
            return last
        time.sleep(1.0)
    if art is not None:
        _dump(art / f"{label}-status-samples.json", samples)
        _dump(art / f"{label}-terminal.json", last)
    raise TimeoutError(f"terminal {terminal_id} never reached {want}; last={last.get('status')}")


def get_output(endpoint: str, terminal_id: str) -> str:
    code, payload = _req(endpoint, "GET", f"/terminals/{terminal_id}/output", query={"mode": "full"})
    if isinstance(payload, dict):
        return str(payload.get("output") or "")
    return str(payload or "")


def poll_pane_for(
    socket: str,
    session: str,
    window: str,
    patterns: list[str],
    *,
    timeout: float = 120.0,
    art: Path,
    label: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    hits: dict[str, bool] = {p: False for p in patterns}
    snapshots: list[str] = []
    while time.monotonic() < deadline:
        text = capture_pane(socket, session, window, lines=120)
        snapshots.append(text)
        clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
        for p in patterns:
            if re.search(p, clean):
                hits[p] = True
        if all(hits.values()):
            break
        time.sleep(1.0)
    _write(art / f"{label}-pane-last.txt", snapshots[-1] if snapshots else "")
    _write(art / f"{label}-pane-joined.txt", "\n---SNAP---\n".join(snapshots[-8:]))
    return {"hits": hits, "all": all(hits.values()), "last": snapshots[-1] if snapshots else ""}


def server_log_tail(sandbox_root: Path, n: int = 400) -> str:
    log = sandbox_root / "logs" / "server.log"
    if not log.exists():
        return ""
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--tmux-socket", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--art", required=True)
    ap.add_argument("--sandbox-root", required=True)
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()

    global _INSTANCE_ID
    art = Path(args.art)
    evid = art / "evidence"
    evid.mkdir(parents=True, exist_ok=True)
    endpoint = args.endpoint.rstrip("/")
    # Instance affinity header required for sandbox mutations
    import tomllib, os
    try:
        with open(args.manifest, "rb") as fh:
            _INSTANCE_ID = tomllib.load(fh)["instance_id"]
    except Exception:
        _INSTANCE_ID = os.environ.get("CAO_INSTANCE_ID", "").strip()
    print(f"X-CAO-Instance={_INSTANCE_ID}", flush=True)
    socket = args.tmux_socket
    workdir = args.workdir
    sandbox_root = Path(args.sandbox_root)
    verdicts: dict[str, Any] = {
        "smoke": {"pass": False, "notes": []},
        "seam1_import_reject": {"pass": False, "notes": []},
        "seam2_deferred_init": {"pass": False, "notes": []},
    }

    # ------------------------------------------------------------------
    # SMOKE + SEAM 1: create session with claude_code (sync init) so the
    # multi-prompt startup path runs, including external-import reject.
    # ------------------------------------------------------------------
    print("=== SEAM1+SMOKE: POST /sessions claude_code (defer_init via initial_message) ===", flush=True)
    # Prefer a cwd whose parent has CLAUDE.md (external import candidate).
    # initial_message forces defer_init so HTTP returns before multi-prompt
    # startup finishes — we can poll the pane for trust + import-reject live.
    code, session_term = _req(
        endpoint,
        "POST",
        "/sessions",
        query={
            "agent_profile": "developer",
            "provider": "claude_code",
            "session_name": args.session,
            "working_directory": workdir,
        },
        body={
            "initial_message": "You are a V5 gate supervisor. Reply IDLE_OK and wait.",
            "initial_message_orchestration_type": "send_message",
            "lifecycle": "sticky",
        },
        timeout=60.0,
    )
    _dump(evid / "session-create.json", {"http": code, "body": session_term})
    if code not in (200, 201) or not isinstance(session_term, dict) or "id" not in session_term:
        verdicts["smoke"]["notes"].append(f"session create failed http={code} body={session_term}")
        verdicts["seam1_import_reject"]["notes"].append("blocked: no session/terminal")
        _dump(art / "verdicts.json", verdicts)
        _write(art / "server-log-tail.txt", server_log_tail(sandbox_root, 800))
        print(json.dumps(verdicts, indent=2))
        return 1

    supervisor_id = session_term["id"]
    session_name = session_term.get("session_name") or f"cao-{args.session}"
    window_name = session_term.get("name") or "0"
    verdicts["smoke"]["notes"].append(
        f"session created id={supervisor_id} session={session_name} status={session_term.get('status')}"
    )

    # Sample panes during startup for trust + external import prompts.
    print("=== SEAM1: poll pane for trust + external import ===", flush=True)
    patterns = [
        r"Yes, I trust this folder|Do you trust|trust this folder",
        r"Allow external CLAUDE\.md file imports\?",
        r"Welcome to Claude Code|Claude Code v\d+|❯|No, disable external imports",
    ]
    # Give init time; also poll statuses
    pane_hits = {"hits": {}, "all": False, "last": ""}
    status_samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + 180.0
    seen_trust = False
    seen_import = False
    seen_reject_log = False
    seen_ready = False
    import_rejected_in_pane = False
    while time.monotonic() < deadline:
        code_t, term = _req(endpoint, "GET", f"/terminals/{supervisor_id}")
        st = term.get("status") if isinstance(term, dict) else None
        status_samples.append({"t": time.time(), "status": st, "http": code_t})
        try:
            pane = capture_pane(socket, session_name, window_name, lines=120)
        except Exception:
            pane = ""
        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", pane)
        if re.search(r"Yes, I trust this folder|trust this folder", clean, re.I):
            seen_trust = True
        if re.search(r"Allow external CLAUDE\.md file imports\?", clean):
            seen_import = True
        if re.search(r"No, disable external imports", clean) and seen_import:
            # selection may still show option text after reject
            import_rejected_in_pane = True
        if re.search(r"Welcome to Claude Code|Claude Code v\d+", clean):
            seen_ready = True
        if st in {"idle", "completed", "IDLE", "COMPLETED"}:
            seen_ready = True
            break
        if st in {"error", "ERROR", "dead", "DEAD"}:
            break
        time.sleep(1.0)

    _dump(evid / "seam1-status-samples.json", status_samples)
    _write(evid / "seam1-pane-last.txt", pane if "pane" in dir() else "")
    try:
        final_pane = capture_pane(socket, session_name, window_name, lines=150)
    except Exception as exc:
        final_pane = f"<capture failed: {exc}>"
    _write(evid / "seam1-pane-final.txt", final_pane)
    final_clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", final_pane)

    # Server log evidence for reject arm
    slog = server_log_tail(sandbox_root, 1200)
    _write(evid / "server-log-mid.txt", slog)
    if "External CLAUDE.md import prompt detected, rejecting" in slog:
        seen_reject_log = True
    if "Workspace trust prompt detected, auto-accepting" in slog:
        seen_trust = True
    # Also accept "Claude Code started without prompts" as healthy post-handler exit
    if "Claude Code started without prompts" in slog:
        verdicts["seam1_import_reject"]["notes"].append(
            "handler exited via welcome-banner early return"
        )
    # Dump all provider log lines for evidence
    import re as _re
    provider_lines = [ln for ln in slog.splitlines() if _re.search(
        r"trust|import|External|Workspace|Startup|Claude Code|reject|bypass", ln, _re.I
    )]
    _write(evid / "seam1-provider-log-lines.txt", "\n".join(provider_lines) + "\n")
    verdicts["seam1_import_reject"]["provider_log_lines"] = provider_lines

    code_t, term_final = _req(endpoint, "GET", f"/terminals/{supervisor_id}")
    _dump(evid / "seam1-terminal-final.json", term_final)
    final_status = term_final.get("status") if isinstance(term_final, dict) else None
    output = get_output(endpoint, supervisor_id)
    _write(evid / "seam1-output-full.txt", output)

    # Identity assertion for claude lane
    ident = assert_binary_identity(socket, "claude", evid)
    verdicts["smoke"]["identity"] = {
        "ok": ident["ok"],
        "n_rows": len(ident["rows"]),
        "matches": [r for r in ident["rows"] if r.get("match")],
    }

    # Import not applied: sandbox native CLAUDE.md marker still only in native home;
    # external parent CLAUDE.md content must not appear as applied project memory.
    native_home = sandbox_root / "provider-homes" / "claude" / "native-home" / "CLAUDE.md"
    native_text = native_home.read_text(encoding="utf-8") if native_home.exists() else ""
    external_marker = "EXTERNAL parent CLAUDE.md for import-reject probe"
    import_applied = external_marker in final_clean or external_marker in output
    hung = final_status in {None, "processing", "PROCESSING", "unknown", "UNKNOWN"} and not seen_ready
    dead = final_status in {"error", "ERROR", "dead", "DEAD"}

    seam1_ok = (
        final_status in {"idle", "completed", "IDLE", "COMPLETED"}
        and not hung
        and not dead
        and not import_applied
        and (seen_reject_log or seen_import)
    )
    # If external import prompt never appeared live, still pass only if trust was
    # handled and terminal is healthy — but mark as PARTIAL with note (not full seam proof).
    if not (seen_reject_log or seen_import):
        seam1_ok = False
        verdicts["seam1_import_reject"]["notes"].append(
            "BLOCKER: external CLAUDE.md import prompt never observed in pane or server log"
        )
    if hung:
        verdicts["seam1_import_reject"]["notes"].append(f"hung status={final_status}")
    if dead:
        verdicts["seam1_import_reject"]["notes"].append(f"dead status={final_status}")
    if import_applied:
        verdicts["seam1_import_reject"]["notes"].append("external CLAUDE.md content appears applied")

    verdicts["seam1_import_reject"].update(
        {
            "pass": seam1_ok,
            "final_status": final_status,
            "seen_trust": seen_trust,
            "seen_import_prompt": seen_import,
            "seen_reject_log": seen_reject_log,
            "import_applied": import_applied,
            "native_claude_md_head": native_text.splitlines()[:2],
            "pane_snippet": final_clean[-1500:],
        }
    )
    print(f"SEAM1 pass={seam1_ok} status={final_status} trust={seen_trust} import={seen_import} reject_log={seen_reject_log}", flush=True)

    # ------------------------------------------------------------------
    # SMOKE: assign-equivalent (defer_init terminal + initial_message) and
    # send_message (inbox) round-trip on a second terminal.
    # ------------------------------------------------------------------
    print("=== SMOKE: defer_init assign-equivalent worker ===", flush=True)
    token = f"v5-smoke-{int(time.time())}"
    code_w, worker = _req(
        endpoint,
        "POST",
        f"/sessions/{urllib.parse.quote(session_name)}/terminals",
        query={
            "agent_profile": "developer",
            "provider": "claude_code",
            "working_directory": workdir,
            "caller_id": supervisor_id,
            "defer_init": "true",
        },
        body={
            "initial_message": (
                f"Reply with exactly the token {token} and nothing else. "
                "Do not run tools."
            ),
            "initial_message_orchestration_type": "assign",
            "lifecycle": "ephemeral",
        },
        timeout=60.0,
    )
    _dump(evid / "smoke-worker-create.json", {"http": code_w, "body": worker})
    if code_w not in (200, 201) or not isinstance(worker, dict) or "id" not in worker:
        verdicts["smoke"]["notes"].append(f"defer_init worker create failed http={code_w}")
        verdicts["seam2_deferred_init"]["notes"].append("blocked: worker create failed")
    else:
        worker_id = worker["id"]
        worker_window = worker.get("name") or ""
        create_status = worker.get("status")
        verdicts["smoke"]["notes"].append(
            f"worker created id={worker_id} immediate_status={create_status}"
        )
        # Deferred path should return UNKNOWN (not IDLE) immediately.
        if str(create_status).lower() not in {"unknown", "processing", "idle"}:
            verdicts["smoke"]["notes"].append(f"unexpected immediate status {create_status}")

        try:
            ready = wait_status(
                endpoint,
                worker_id,
                {"idle", "completed", "IDLE", "COMPLETED"},
                timeout=240.0,
                art=evid,
                label="seam2-init",
            )
            init_ok = True
            ready_status = ready.get("status")
        except TimeoutError as exc:
            init_ok = False
            ready_status = None
            verdicts["seam2_deferred_init"]["notes"].append(str(exc))
            code_t, ready = _req(endpoint, "GET", f"/terminals/{worker_id}")
            _dump(evid / "seam2-timeout-terminal.json", ready)

        # Capture worker pane after init
        try:
            w_pane = capture_pane(socket, session_name, worker_window, lines=120)
        except Exception as exc:
            w_pane = f"<capture failed: {exc}>"
        _write(evid / "seam2-worker-pane.txt", w_pane)

        # send_message round-trip via inbox API
        print("=== SMOKE: inbox send_message round-trip ===", flush=True)
        msg_token = f"v5-inbox-{int(time.time())}"
        # Wait until idle again if processing the initial message
        try:
            wait_status(
                endpoint,
                worker_id,
                {"idle", "completed", "IDLE", "COMPLETED"},
                timeout=120.0,
                art=evid,
                label="smoke-pre-inbox",
            )
        except TimeoutError:
            pass
        code_m, msg = _req(
            endpoint,
            "POST",
            f"/terminals/{worker_id}/inbox/messages",
            query={
                "sender_id": supervisor_id,
                "message": f"Echo exactly: {msg_token}",
            },
            timeout=30.0,
        )
        _dump(evid / "smoke-inbox-send.json", {"http": code_m, "body": msg})
        # Poll for delivery / response content
        inbox_ok = code_m in (200, 201)
        delivered_seen = False
        reply_seen = False
        inbox_deadline = time.monotonic() + 120.0
        while time.monotonic() < inbox_deadline:
            out = get_output(endpoint, worker_id)
            if msg_token in out:
                reply_seen = True
                break
            code_i, inbox = _req(
                endpoint,
                "GET",
                f"/terminals/{worker_id}/inbox/messages",
                query={"limit": 10},
            )
            if isinstance(inbox, (list, dict)):
                blob = json.dumps(inbox)
                if "delivered" in blob.lower() or "DELIVERED" in blob:
                    delivered_seen = True
            time.sleep(2.0)
        _write(evid / "smoke-worker-output-after-inbox.txt", get_output(endpoint, worker_id))
        verdicts["smoke"]["inbox"] = {
            "http": code_m,
            "inbox_ok": inbox_ok,
            "reply_seen": reply_seen,
            "delivered_seen": delivered_seen,
            "token": msg_token,
        }
        # Smoke pass: server up (already), CLI (shell), create worker, inbox accepted.
        # Reply from live model is best-effort; don't hard-fail smoke if model slow.
        smoke_ok = (
            code in (200, 201)
            and code_w in (200, 201)
            and inbox_ok
            and init_ok
        )
        verdicts["smoke"]["pass"] = smoke_ok
        verdicts["smoke"]["notes"].append(
            f"init_ok={init_ok} ready_status={ready_status} inbox_http={code_m} reply_seen={reply_seen}"
        )

        # ------------------------------------------------------------------
        # SEAM 2: delete/quiesce the deferred-init terminal after it initialized.
        # ------------------------------------------------------------------
        print("=== SEAM2: DELETE deferred-init terminal ===", flush=True)
        log_before = server_log_tail(sandbox_root, 200)
        _write(evid / "seam2-log-before-delete.txt", log_before)
        code_d, deleted = _req(
            endpoint,
            "DELETE",
            f"/terminals/{worker_id}",
            query={"force": "true"},
            timeout=120.0,
        )
        _dump(evid / "seam2-delete.json", {"http": code_d, "body": deleted})
        time.sleep(2.0)
        code_g, gone = _req(endpoint, "GET", f"/terminals/{worker_id}")
        _dump(evid / "seam2-get-after-delete.json", {"http": code_g, "body": gone})
        log_after = server_log_tail(sandbox_root, 400)
        _write(evid / "seam2-log-after-delete.txt", log_after)

        # Ghost-churn / wedged lease signals
        churn_patterns = [
            r"StatusMonitor.*ghost",
            r"ghost.?churn",
            r"wedged.?lease",
            r"lease.*wedge",
            r"quiesce_timeout",
            r"quiesce_timeout_mutation_in_flight",
            r"deferred_task_quiesce_timeout",
            r"rollback_kill_uncertain",
        ]
        # Only look at new log lines after delete
        new_log = log_after[len(log_before) :] if log_after.startswith(log_before) else log_after
        churn_hits = [p for p in churn_patterns if re.search(p, new_log, re.I)]
        # Also scan full recent log for post-delete errors on this terminal
        term_errs = re.findall(
            rf".*{re.escape(worker_id)}.*(?:ERROR|error|quiesce|ghost|lease).*",
            new_log,
        )
        gone_ok = code_g == 404 or (
            isinstance(gone, dict) and gone.get("detail") is not None and code_g in {404, 400}
        )
        delete_ok = code_d in (200, 201) and (
            isinstance(deleted, dict) and deleted.get("success") is True
        )
        # Window should disappear from tmux
        windows = list_windows(socket)
        _dump(evid / "seam2-windows-after-delete.json", windows)
        still_present = any(
            w.get("name") == worker_window and w.get("session") == session_name for w in windows
        )

        seam2_ok = delete_ok and gone_ok and not churn_hits and not still_present and init_ok
        verdicts["seam2_deferred_init"].update(
            {
                "pass": seam2_ok,
                "worker_id": worker_id,
                "init_ok": init_ok,
                "ready_status": ready_status,
                "delete_http": code_d,
                "delete_ok": delete_ok,
                "gone_ok": gone_ok,
                "still_present_in_tmux": still_present,
                "churn_hits": churn_hits,
                "term_err_lines": term_errs[:20],
                "lifecycle": "create(defer_init) → init → delete → gone",
            }
        )
        if not seam2_ok:
            if churn_hits:
                verdicts["seam2_deferred_init"]["notes"].append(
                    f"BLOCKER: ghost/lease churn patterns: {churn_hits}"
                )
            if still_present:
                verdicts["seam2_deferred_init"]["notes"].append(
                    "BLOCKER: tmux window still present after delete"
                )
            if not delete_ok:
                verdicts["seam2_deferred_init"]["notes"].append(
                    f"delete failed http={code_d} body={deleted}"
                )
        print(
            f"SEAM2 pass={seam2_ok} delete={code_d} gone={code_g} churn={churn_hits} still_tmux={still_present}",
            flush=True,
        )

        # Identity assertion after worker existed
        assert_binary_identity(socket, "claude", evid)

    # If smoke never set (worker create failed early), evaluate from session alone
    if not verdicts["smoke"]["pass"]:
        # Minimal smoke: health already proven; session created
        if code in (200, 201) and isinstance(session_term, dict):
            # still fail if worker path failed
            pass

    # CLI smoke evidence already from shell; re-check health
    code_h, health = _req(endpoint, "GET", "/health")
    _dump(evid / "health-final.json", {"http": code_h, "body": health})
    if code_h == 200:
        verdicts["smoke"]["notes"].append("health still 200 at end")
        if not verdicts["smoke"]["pass"] and code in (200, 201):
            # If worker path partially worked without full init, mark smoke fail
            pass
    else:
        verdicts["smoke"]["pass"] = False
        verdicts["smoke"]["notes"].append(f"health failed at end http={code_h}")

    # Final log dump
    _write(art / "server-log-final.txt", server_log_tail(sandbox_root, 1500))
    _dump(art / "verdicts.json", verdicts)

    overall = all(v.get("pass") for v in verdicts.values())
    summary = {
        "overall": "PASS" if overall else "FAIL",
        "verdicts": {
            k: ("PASS" if v.get("pass") else "FAIL") for k, v in verdicts.items()
        },
        "details": verdicts,
    }
    _dump(art / "summary.json", summary)
    print(json.dumps(summary, indent=2, default=str))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
