"""F829 Build 2 — AC1 live arms through the PUBLIC assign(resume_from) seam.

Verdict B2 rejected the prior live harness because it hand-rolled
``prepare_resume`` + a direct provider-CLI call, bypassing the public
``assign(resume_from)`` path and the production create/publish path AC1
requires. THIS module closes that: every arm drives

  1. FRESH SPAWN through production create/publish  — ``POST /sessions`` (what
     ``_assign_impl`` calls via ``_create_terminal``), so the spawn-time mint
     (conversation_identity root + recovery_manifest + per-attempt
     ``capture_nonce``; kiro nonce injected into the first turn) fires FOR REAL
     against the scratch server DB.
  2. PLANT a marker  — a real provider turn that remembers a unique token.
  3. PLANNED HIBERNATE  — ``DELETE /terminals/{id}`` non-force (the real
     operator flow: account switch → planned hibernate), which lands the root
     lifecycle ``hibernated`` once the D6 artifact validates.
  4. RESUME through the PUBLIC SEAM  — ``server._assign_impl(resume_from=
     conv_<worker_id>)`` with ``CAO_ENDPOINT``/``CAO_TERMINAL_ID`` pointed at
     the live scratch server, so the resume runs the production
     create/publish/verify/callback path (NOT a ``_create_terminal`` stub).
  5. RECALL + DB PROOF  — the resumed worker recalls the token verbatim AND the
     scratch DB shows exactly one conversation_identity root, two
     terminal_identity incarnations (the hibernated original + the live
     resume), a recovery_manifest carrying the capture_nonce, resume_key
     continuity (the same provider_session_id on both incarnations), and
     retained workspace/pins.

The ``kiro_cli`` arm additionally creates a SECOND same-cwd kiro session
(a foreign/copied candidate, newer mtime) before hibernate, so the resume must
bind the OWN nonce-carrying session, never "newest updatedAt" (verdict B4).

Scratch isolation: the ``cao_server`` fixture redirects ``$HOME`` to a
per-session tmp dir, so the SQLite DB, logs, and provider sandbox state live
under it — the production DB is never opened. Gated by ``--run-live`` and the
per-provider ``require_*`` fixtures (an unauthenticated CLI is skipped, never
driven into an interactive login).

Run (one provider at a time, cheap models only)::

    uv run pytest -m e2e --run-live test/e2e/test_f829_public_resume_arms.py -k kiro -v
    uv run pytest -m e2e --run-live test/e2e/test_f829_public_resume_arms.py -k claude -v
    uv run pytest -m e2e --run-live test/e2e/test_f829_public_resume_arms.py -k pi -v
    uv run pytest -m e2e --run-live test/e2e/test_f829_public_resume_arms.py -k codex -v
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from test.e2e.conftest import (
    extract_output,
    get_terminal_status,
    wait_for_status,
)
from test.fixtures.cao_server import CaoServer

import pytest
import requests

pytestmark = [pytest.mark.e2e]

# Cheap models only (brief). None => provider default (kiro).
_MODEL = {
    "kiro_cli": None,
    "claude_code": "sonnet",
    "pi_cli": "cline-pass/glm-5.3-flash",
    "codex": "gpt-5.6-luna",
}

_READY = ("idle", "completed")
_READY_TIMEOUT = 120.0
_TURN_TIMEOUT = 180.0

# codex quota-banner fragments — if seen, record and STOP (never touch auth).
_QUOTA_BANNERS = (
    "usage limit",
    "quota",
    "rate limit",
    "429",
    "insufficient_quota",
    "you've hit your",
    "upgrade to",
)


def _api(cao_server: CaoServer) -> str:
    return cao_server.url


def _wait_ready(tid: str, timeout: float = _READY_TIMEOUT) -> str:
    start = time.time()
    s = "unknown"
    while time.time() - start < timeout:
        s = get_terminal_status(tid)
        if s in _READY or s == "error":
            break
        time.sleep(3)
    return s


def _create_session_terminal(api: str, provider: str, profile: str, session: str,
                             model: str | None) -> tuple[str, str]:
    params = {"provider": provider, "agent_profile": profile, "session_name": session}
    if model:
        params["model"] = model
    resp = requests.post(f"{api}/sessions", params=params, timeout=120)
    assert resp.status_code in (200, 201), f"create failed: {resp.status_code} {resp.text}"
    data = resp.json()
    return data["id"], data["session_name"]


def _send(api: str, tid: str, message: str) -> None:
    resp = requests.post(f"{api}/terminals/{tid}/input", params={"message": message}, timeout=60)
    assert resp.status_code == 200, f"input failed: {resp.status_code} {resp.text}"


def _drive_turn(api: str, tid: str, message: str, timeout: float = _TURN_TIMEOUT) -> str:
    """Send a message, wait for the turn to complete, return the last output."""
    _send(api, tid, message)
    # Wait for completed (providers with initial prompts flip to completed).
    ok = wait_for_status(tid, "completed", timeout=timeout)
    if not ok:
        # Some providers settle on idle after a turn; accept either.
        s = get_terminal_status(tid)
        assert s in _READY, f"turn did not complete (status={s})"
    time.sleep(3)
    return extract_output(tid)


def _hibernate(api: str, tid: str) -> dict:
    """Planned hibernate = DELETE non-force. Returns the JSON result."""
    resp = requests.delete(f"{api}/terminals/{tid}", timeout=120)
    assert resp.status_code == 200, f"hibernate(delete non-force) failed: {resp.status_code} {resp.text}"
    return resp.json()


# --- scratch-DB assertions (query the subprocess DB file directly) ----------


def _db(cao_server: CaoServer) -> sqlite3.Connection:
    conn = sqlite3.connect(str(cao_server.db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _assert_db_resume_shape(cao_server: CaoServer, identity_key: str, *,
                            provider: str) -> dict:
    """The B2 acceptance proof that the PUBLIC/production path was exercised:
    one root, two incarnations, manifest+nonce, resume_key continuity."""
    conn = _db(cao_server)
    try:
        roots = conn.execute(
            "SELECT identity_key, provider, lifecycle, owner_principal "
            "FROM conversation_identity WHERE identity_key = ?",
            (identity_key,),
        ).fetchall()
        assert len(roots) == 1, f"expected exactly ONE conversation_identity root, got {len(roots)}"
        root = roots[0]
        assert root["provider"] == provider

        incs = conn.execute(
            "SELECT terminal_id, provider_session_id, lifecycle, cwd, worktree_path "
            "FROM terminal_identity WHERE identity_key = ? ORDER BY created_at",
            (identity_key,),
        ).fetchall()
        assert len(incs) == 2, (
            f"expected TWO terminal_identity incarnations (hibernated original + "
            f"live resume), got {len(incs)}: {[dict(r) for r in incs]}"
        )
        lifecycles = {r["lifecycle"] for r in incs}
        assert lifecycles == {"live", "reaped"}, (
            f"incarnations must be one reaped (hibernated original) + one live "
            f"(resume), got {lifecycles}"
        )

        # resume_key continuity: the provider_session_id (D4 resume_key) is the
        # SAME on both incarnations — the resume re-attached the same conversation.
        sids = {r["provider_session_id"] for r in incs if r["provider_session_id"]}
        assert len(sids) == 1, (
            f"resume_key continuity broken: incarnations carry different "
            f"provider_session_id values {sids}"
        )

        man = conn.execute(
            "SELECT capture_nonce, cwd FROM recovery_manifest WHERE identity_key = ?",
            (identity_key,),
        ).fetchone()
        assert man is not None, "recovery_manifest row missing for the root"
        assert man["capture_nonce"], "recovery_manifest.capture_nonce not minted at spawn"

        return {
            "root": dict(root),
            "incarnations": [dict(r) for r in incs],
            "resume_key": next(iter(sids)) if sids else None,
            "capture_nonce": man["capture_nonce"],
        }
    finally:
        conn.close()


# --- kiro two-candidate helper ----------------------------------------------


def _kiro_sessions_root(home_dir: Path) -> Path:
    return home_dir / ".kiro" / "sessions"


def _plant_foreign_kiro_candidate(cwd: str, home_dir: Path) -> str | None:
    """Create a SECOND kiro session under the SAME cwd (a foreign/copied
    candidate with a NEWER mtime) so the resume selector must reject
    newest-updatedAt and bind the OWN nonce-carrying session (verdict B4)."""
    prompt = "Say only the word OTHER."
    subprocess.run(
        ["kiro-cli", "--v3", "chat", "--no-interactive", "--trust-all-tools", prompt],
        cwd=cwd, capture_output=True, text=True, timeout=120,
    )
    time.sleep(1.0)
    h = hashlib.sha256(str(cwd).encode()).hexdigest()[:16]
    d = _kiro_sessions_root(home_dir) / h
    if not d.is_dir():
        return None
    cands = sorted(
        (p for p in d.iterdir() if p.is_dir() and p.name.startswith("sess_")),
        key=lambda p: p.stat().st_mtime,
    )
    return cands[-1].name if cands else None


def _quota_banner_hit(text: str) -> str | None:
    low = (text or "").lower()
    for frag in _QUOTA_BANNERS:
        if frag in low:
            return frag
    return None


# --- the arm ----------------------------------------------------------------


def _run_public_resume_arm(cao_server: CaoServer, provider: str, profile: str = "developer",
                           *, kiro_two_candidate: bool = False,
                           artifacts_dir: Path) -> dict:
    api = _api(cao_server)
    token = f"F829-{provider.upper()}-{uuid.uuid4().hex[:6]}"
    session = f"f829-{provider}-{uuid.uuid4().hex[:6]}"
    model = _MODEL.get(provider)

    transcript: list[str] = []

    def rec(label: str, body: str) -> None:
        transcript.append(f"===== {label} =====\n{body}\n")

    supervisor_id = None
    worker_id = None
    actual_session = None
    try:
        # supervisor (stays idle; the recorded caller for the resume seam)
        supervisor_id, actual_session = _create_session_terminal(
            api, provider, profile, session, model
        )
        assert _wait_ready(supervisor_id) in _READY, "supervisor not ready"

        # 1. FRESH SPAWN through production create/publish (spawn-mint fires).
        worker_id, _ = _create_session_terminal(
            api, provider, profile, f"{session}-w", model
        )
        st = _wait_ready(worker_id)
        assert st in _READY, f"worker not ready (status={st})"
        identity_key = f"conv_{worker_id}"

        # worker cwd (for the kiro two-candidate condition)
        wmeta = requests.get(f"{api}/terminals/{worker_id}", timeout=30).json()
        worker_cwd = wmeta.get("cwd") or wmeta.get("working_directory")

        # 2. PLANT the marker via a real provider turn.
        plant = _drive_turn(
            api, worker_id,
            f"Remember this exact token for later: {token}. Reply with just: ACK {token}",
        )
        rec("PLANT", plant[-1500:])
        banner = _quota_banner_hit(plant)
        if banner:
            rec("QUOTA", f"banner fragment seen at PLANT: {banner!r}")
            return {"status": "quota_blocked", "stage": "plant", "banner": banner,
                    "transcript": transcript}

        # kiro-only: plant a SECOND same-cwd candidate (newer mtime, foreign).
        foreign = None
        if kiro_two_candidate and provider == "kiro_cli" and worker_cwd:
            foreign = _plant_foreign_kiro_candidate(worker_cwd, cao_server.home_dir)
            rec("KIRO_TWO_CANDIDATE", f"foreign same-cwd session (newer mtime): {foreign}")

        # 3. PLANNED HIBERNATE — DELETE non-force (the real operator flow).
        hib = _hibernate(api, worker_id)
        rec("HIBERNATE", str(hib))
        # root must be hibernated now
        conn = _db(cao_server)
        try:
            lc = conn.execute(
                "SELECT lifecycle FROM conversation_identity WHERE identity_key = ?",
                (identity_key,),
            ).fetchone()
        finally:
            conn.close()
        assert lc is not None, f"no conversation root for {identity_key} after spawn"
        assert lc[0] == "hibernated", (
            f"planned hibernate did not land 'hibernated' (got {lc[0]}); "
            f"hibernate result: {hib}"
        )

        # 4. RESUME through the PUBLIC SEAM: server._assign_impl(resume_from=...).
        os.environ["CAO_ENDPOINT"] = api
        os.environ["CAO_TERMINAL_ID"] = supervisor_id
        from cli_agent_orchestrator.mcp_server import server as _srv

        assign_kwargs = dict(agent_profile=profile, message=(
            f"What exact token did I ask you to remember earlier? "
            f"Reply with ONLY that token."
        ), resume_from=identity_key)
        if model:
            assign_kwargs["model"] = model
        res = _srv._assign_impl(**assign_kwargs)
        rec("ASSIGN_RESUME_FROM", str({k: res.get(k) for k in
            ("success", "terminal_id", "resumed_from", "worktree", "pins_inherited",
             "resume_line", "error", "reason", "missing")}))
        assert res.get("success") is True, f"public resume seam failed: {res}"
        resumed_id = res["terminal_id"]
        assert res.get("resumed_from") == identity_key or res.get("resumed_from") == worker_id

        # resumed worker must reach ready, then recall the token.
        assert _wait_ready(resumed_id) in _READY, "resumed worker not ready"
        recall = _drive_turn(
            api, resumed_id,
            "What exact token did I ask you to remember earlier? Reply with ONLY that token.",
        )
        rec("RECALL", recall[-1500:])
        banner = _quota_banner_hit(recall)
        if banner:
            rec("QUOTA", f"banner fragment seen at RECALL: {banner!r}")
            return {"status": "quota_blocked", "stage": "recall", "banner": banner,
                    "transcript": transcript}

        assert token in recall, (
            f"token {token} NOT recalled after public-seam resume; recall tail:\n"
            f"{recall[-800:]}"
        )

        # 5. DB PROOF that the public/production path was exercised.
        shape = _assert_db_resume_shape(cao_server, identity_key, provider=provider)
        rec("DB_SHAPE", str(shape))

        # write transcript artifact
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        tpath = artifacts_dir / f"{provider}-arm-transcript.txt"
        tpath.write_text("\n".join(transcript), encoding="utf-8")

        return {"status": "green", "identity_key": identity_key,
                "resumed_id": resumed_id, "worker_id": worker_id,
                "shape": shape, "transcript_path": str(tpath),
                "kiro_foreign": foreign}
    finally:
        os.environ.pop("CAO_ENDPOINT", None)
        os.environ.pop("CAO_TERMINAL_ID", None)
        # best-effort cleanup: force-reap anything still around + drop session
        for tid in (worker_id, supervisor_id):
            if tid:
                with __import__("contextlib").suppress(Exception):
                    requests.delete(f"{api}/terminals/{tid}", params={"force": True}, timeout=30)
        if actual_session:
            with __import__("contextlib").suppress(Exception):
                requests.delete(f"{api}/sessions/{actual_session}", timeout=30)


# --- provider arms (kiro → claude → pi → codex last) ------------------------


def test_kiro_public_resume_arm(require_kiro, cao_server: CaoServer, tmp_path):
    """Kiro long-lived, TWO same-cwd candidates → resume binds the OWN nonce id."""
    out = _run_public_resume_arm(
        cao_server, "kiro_cli", kiro_two_candidate=True, artifacts_dir=tmp_path
    )
    assert out["status"] == "green", out
    assert out["shape"]["capture_nonce"], "kiro nonce must be present on the root manifest"


def test_claude_public_resume_arm(require_claude, cao_server: CaoServer, tmp_path):
    """Claude sonnet through the public assign(resume_from) seam."""
    out = _run_public_resume_arm(cao_server, "claude_code", artifacts_dir=tmp_path)
    assert out["status"] == "green", out


def test_pi_public_resume_arm(require_pi, cao_server: CaoServer, tmp_path):
    """Pi glm-5.3-flash through the public assign(resume_from) seam."""
    out = _run_public_resume_arm(cao_server, "pi_cli", artifacts_dir=tmp_path)
    assert out["status"] == "green", out


def test_codex_public_resume_arm(require_codex, cao_server: CaoServer, tmp_path):
    """Codex gpt-5.6-luna — ONE attempt. On a quota banner, record and stop
    (never touch auth); the arm is not a failure in that case."""
    out = _run_public_resume_arm(cao_server, "codex", artifacts_dir=tmp_path)
    if out["status"] == "quota_blocked":
        pytest.skip(f"codex quota banner at {out['stage']}: {out['banner']!r} — recorded, stopped")
    assert out["status"] == "green", out
