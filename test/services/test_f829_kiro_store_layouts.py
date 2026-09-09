"""F829 kiro-harness: capture_kiro_session_id_from_store supports BOTH kiro
session-store layouts (kiro-cli versions differ across laptop/boxes):
  * LEGACY: ~/.kiro/sessions/<sha256(cwd)[:16]>/sess_<uuid>/session.json + messages.jsonl
  * FLAT (kiro-cli 2.20.1): ~/.kiro/sessions/cli/<uuid>.json (+ <uuid>.jsonl)
Positive per-attempt attribution (the injected capture_nonce), NEVER newest-mtime,
and two same-cwd candidates must resolve to the OWN (nonce-carrying) id — identical
across layouts. Tests: legacy, flat, mixed (both present, nonce in ONE), negative
(nonce in neither → no attach).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cli_agent_orchestrator.services.resume_service import (
    capture_kiro_session_id_from_store,
)

CWD = "/work/repo"
NONCE = "cao-nonce-DEADBEEF"


def _cwd_hash(cwd: str) -> str:
    return hashlib.sha256(cwd.encode()).hexdigest()[:16]


def _legacy_session(root: Path, sess_id: str, *, cwd: str = CWD, marker: str | None) -> None:
    d = root / _cwd_hash(cwd) / sess_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(json.dumps({"id": sess_id, "rootPaths": [cwd]}))
    body = "some turn\n" + (marker + "\n" if marker else "")
    (d / "messages.jsonl").write_text(body)


def _flat_session(root: Path, uuid: str, *, cwd: str = CWD, marker: str | None) -> None:
    d = root / "cli"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{uuid}.json").write_text(json.dumps({"id": uuid, "rootPaths": [cwd]}))
    body = "some turn\n" + (marker + "\n" if marker else "")
    (d / f"{uuid}.jsonl").write_text(body)


def test_legacy_layout_nonce_attribution(tmp_path):
    root = tmp_path / "sessions"
    _legacy_session(root, "sess_legacy-1", marker=NONCE)
    _legacy_session(root, "sess_legacy-foreign", marker=None)  # no nonce → not attributed
    sid, reason, count = capture_kiro_session_id_from_store(
        CWD, "t1", capture_nonce=NONCE, sessions_root=root
    )
    assert sid == "sess_legacy-1", (sid, reason, count)
    assert reason is None


def test_flat_layout_nonce_attribution(tmp_path):
    root = tmp_path / "sessions"
    _flat_session(root, "flat-uuid-1", marker=NONCE)
    _flat_session(root, "flat-uuid-foreign", marker=None)
    sid, reason, count = capture_kiro_session_id_from_store(
        CWD, "t1", capture_nonce=NONCE, sessions_root=root
    )
    assert sid == "flat-uuid-1", (sid, reason, count)
    assert reason is None


def test_flat_two_same_cwd_candidates_returns_own_nonce_id(tmp_path):
    """Two flat sessions under the SAME cwd; only ONE carries THIS nonce → bind
    the OWN id, never the newer/foreign one (verdict B4 across the flat layout)."""
    root = tmp_path / "sessions"
    _flat_session(root, "flat-own", marker=NONCE)
    _flat_session(root, "flat-foreign-newer", marker=None)
    sid, reason, count = capture_kiro_session_id_from_store(
        CWD, "t1", capture_nonce=NONCE, sessions_root=root
    )
    assert sid == "flat-own", (sid, reason, count)
    assert count >= 2  # both cwd-matched; only one attributed


def test_mixed_store_nonce_only_in_flat(tmp_path):
    """Both layouts present; the nonce carrier is a FLAT session, the legacy one
    is foreign (no nonce) → attribute the flat id, still exactly one."""
    root = tmp_path / "sessions"
    _legacy_session(root, "sess_legacy-foreign", marker=None)
    _flat_session(root, "flat-own", marker=NONCE)
    sid, reason, count = capture_kiro_session_id_from_store(
        CWD, "t1", capture_nonce=NONCE, sessions_root=root
    )
    assert sid == "flat-own", (sid, reason, count)
    assert reason is None


def test_mixed_store_nonce_only_in_legacy(tmp_path):
    root = tmp_path / "sessions"
    _legacy_session(root, "sess_legacy-own", marker=NONCE)
    _flat_session(root, "flat-foreign", marker=None)
    sid, reason, count = capture_kiro_session_id_from_store(
        CWD, "t1", capture_nonce=NONCE, sessions_root=root
    )
    assert sid == "sess_legacy-own", (sid, reason, count)
    assert reason is None


def test_negative_nonce_in_neither_no_attach(tmp_path):
    """Nonce in NO session (legacy + flat both foreign) → refuse (capture_unknown),
    never bind on cwd/mtime alone."""
    root = tmp_path / "sessions"
    _legacy_session(root, "sess_legacy-foreign", marker=None)
    _flat_session(root, "flat-foreign", marker=None)
    sid, reason, count = capture_kiro_session_id_from_store(
        CWD, "t1", capture_nonce=NONCE, sessions_root=root
    )
    assert sid is None
    assert reason == "capture_unknown"
    assert count >= 2  # both cwd-matched, neither attributed


def test_ambiguous_nonce_in_both_layouts_refuses(tmp_path):
    """If the SAME nonce somehow appears in both a legacy and a flat session
    (>1 attributed across layouts), refuse rather than guess."""
    root = tmp_path / "sessions"
    _legacy_session(root, "sess_legacy-own", marker=NONCE)
    _flat_session(root, "flat-own", marker=NONCE)
    sid, reason, count = capture_kiro_session_id_from_store(
        CWD, "t1", capture_nonce=NONCE, sessions_root=root
    )
    assert sid is None
    assert reason == "capture_unknown"
