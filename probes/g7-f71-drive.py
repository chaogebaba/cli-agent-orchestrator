#!/usr/bin/env python3
"""G7 LIVE SANDBOX PROOF — F71 sweep completion-branch.

Runs against the isolated G7 sandbox on :9898 (never production :9889).
Proves (per G7 doctrine):
  Arm 1 — completion fire: an all-ARRIVED barrier (real dispatch + real arrival
          for one member, orphan-stamped ARRIVED for the others) fires the barrier
          itself as FIRED_COMPLETE well before timeout_at, producing exactly one
          combined PENDING callback row.
  Arm 2 — single-winner CAS: no duplicate fire/callback rows even with the 1s
          sweep racing arrival; a second manual sweep is a no-op.
  Arm 3 — owner-gone precedence: all-ARRIVED barrier whose owner terminal is
          deleted before fire closes CANCELLED/owner_gone, no PENDING callback,
          no daemon crash.

TRIGGER, not reply: the sweep fires the row itself. We never hand-mint the
FIRED row or the combined callback. The orphan-ARRIVED stamp is the F71 input
state (a crash artifact no public API can produce — see blueprint §1 + the
unit test `_seed_all_arrived_barrier`), not the reply.
"""
from __future__ import annotations

import json, os, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator")
SANDBOX_ROOT = Path("/home/chao/cao-sandbox-f71")
ART = Path("/home/chao/VScode_projects/cli-subagents/tmp/orch/g7-f71-artifacts")
ENDPOINT = "http://127.0.0.1:9898"
INSTANCE = "620d0de5"
DB = SANDBOX_ROOT / "db" / "cli-agent-orchestrator.db"

# Point the CAO module tree at the sandbox DB (same override the server uses).
os.environ["CAO_HOME_DIR"] = str(SANDBOX_ROOT)
os.environ["CAO_INSTANCE_ID"] = INSTANCE
os.environ["CAO_ENDPOINT"] = ENDPOINT
os.environ["CAO_SANDBOX_MANIFEST"] = str(SANDBOX_ROOT / "instance-manifest.toml")
os.environ["CLAUDE_CONFIG_DIR"] = str(SANDBOX_ROOT / "provider-homes" / "claude")
os.environ["CODEX_HOME"] = str(SANDBOX_ROOT / "provider-homes" / "codex")
# Server log path for daemon-crash checks.
SERVER_LOG = sorted((SANDBOX_ROOT / "logs").glob("cao_*.log"))[-1]

sys.path.insert(0, str(ROOT / "src"))
from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import (
    SessionLocal, create_terminal, create_inbox_message,
    fire_due_barriers, delete_terminal_and_warm_intent,
    CallbackBarrierModel, CallbackBarrierMemberModel, InboxModel,
)
from cli_agent_orchestrator.models.inbox import MessageStatus

def now_utc():
    return datetime.now(timezone.utc)

def rows(q):
    import sqlite3
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(q).fetchall()
    finally:
        con.close()

def barrier_state(label):
    r = rows("SELECT state, close_reason, fired_at, timeout_at, combined_message_id, created_at "
             f"FROM callback_barrier WHERE label='{label}'")
    return dict(r[0]) if r else None

def barrier_members(label):
    return [dict(r) for r in rows(
        "SELECT m.member_key, m.state, m.arrived_at, m.message_id FROM callback_barrier_member m "
        f"JOIN callback_barrier b ON b.id=m.barrier_id WHERE b.label='{label}' ORDER BY m.position")]

def combined_rows(label):
    return [dict(r) for r in rows(
        "SELECT i.id, i.status, i.receiver_id, i.sender_id, substr(i.message,1,120) head "
        "FROM inbox i JOIN callback_barrier b ON b.combined_message_id=i.id "
        f"WHERE b.label='{label}'")]

def full_combined(label):
    return [dict(r) for r in rows(
        "SELECT i.id, i.status, i.message FROM inbox i "
        f"JOIN callback_barrier b ON b.combined_message_id=i.id WHERE b.label='{label}'")]

def wait_until(pred, timeout=20, label="wait"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.5)
    return False

def sweep_once():
    # a real sweep invocation (same function the daemon calls every 1s)
    return fire_due_barriers(now_utc())

def log(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    (ART / "evidence" / "driver.log").open("a").write(line + "\n")

def punch(name, obj):
    (ART / "evidence" / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str) + "\n")

def main():
    ART.mkdir(parents=True, exist_ok=True)
    (ART / "evidence" / "driver.log").open("w").close()
    log("=== G7-F71 DRIVE START", now_utc().isoformat(), "===")
    log("ENDPOINT", ENDPOINT, "DB", DB, "SERVER_LOG", SERVER_LOG)

    # Self-cleaning preamble: the sandbox DB is isolated, so wipe the test namespace
    # so a re-run (or a prior partial run) cannot collide with leftover rows.
    with SessionLocal.begin() as db:
        db.query(dbmod.CallbackBarrierMemberModel).delete()
        db.query(dbmod.CallbackBarrierModel).delete()
        db.query(dbmod.InboxModel).delete()
        db.query(dbmod.TerminalModel).delete()
    log("cleaned test tables (barrier/member/inbox/terminal)")

    owner = "g7f71-supervisor"
    wa, wb = "g7f71-worker-a", "g7f71-worker-b"
    # ---- seed terminal rows (the DB-backed identity surface) ----
    with SessionLocal.begin() as db:
        n = db.query(dbmod.TerminalModel).filter(dbmod.TerminalModel.id.in_([owner, wa, wb])).delete()
    for tid, caller in ((owner, None), (wa, owner), (wb, owner)):
        create_terminal(tid, "cao-g7f71", tid, "kiro_cli", agent_profile="developer",
                        caller_id=caller, lifecycle="ephemeral")
    log("terminals seeded:", owner, wa, wb)

    # ================= ARM 1 + ARM 2: completion fire =================
    label = "g7-f71-complete"
    t0 = now_utc()
    # REAL dispatch: owner sends barrier'd tasks to both workers (real surface).
    create_inbox_message(owner, wa, "task a", dispatch_barrier={"label": label, "timeout_seconds": 600})
    create_inbox_message(owner, wb, "task b", dispatch_barrier={"label": label, "timeout_seconds": 600})
    b = barrier_state(label)
    log("ARM1 dispatch ->", b)
    # REAL arrival for worker-a (1/2, not complete -> no fire yet).
    create_inbox_message(wa, owner, "answer a")
    log("ARM1 worker-a arrived; barrier:", barrier_state(label), "members:", barrier_members(label))
    # ORPHAN-STAMP worker-b ARRIVED (the F71 crash artifact: last-arrival fireskip).
    # This is the trigger's input state, NOT the reply — the sweep must fire it.
    with SessionLocal.begin() as db:
        row = db.query(CallbackBarrierMemberModel).join(CallbackBarrierModel).filter(
            CallbackBarrierModel.label == label,
            CallbackBarrierMemberModel.member_key == "developer-2"
        ).one()
        row.state = "ARRIVED"
        row.arrived_at = now_utc()
    log("ARM1 orphan-stamped worker-b ARRIVED; members:", barrier_members(label))
    # Observe the LIVE 1s daemon sweep fire it (no manual sweep on the hot path).
    fired_ok = wait_until(lambda: barrier_state(label)["state"] in ("FIRED_COMPLETE","CANCELLED"), 20)
    st = barrier_state(label)
    t1 = now_utc()
    latency_s = round((t1 - t0).total_seconds(), 2)
    log("ARM1 barrier after sweep:", st)
    log("ARM1 latency since dispatch:", latency_s, "s")
    punch("arm1-after-sweep", {"state": st, "members": barrier_members(label),
                               "combined": combined_rows(label), "latency_s": latency_s})
    assert st["state"] == "FIRED_COMPLETE", f"ARM1 FAIL: expected FIRED_COMPLETE got {st['state']}"
    assert st["close_reason"] == "complete"
    comb = combined_rows(label)
    assert len(comb) == 1, f"ARM1 FAIL: want exactly 1 combined row, got {len(comb)}"
    assert comb[0]["status"] == "pending"
    full = full_combined(label)
    assert full[0]["message"].startswith("[callback barrier COMPLETE]"), "ARM1 FAIL: not COMPLETE header"
    assert "answer a" in full[0]["message"], "ARM1 FAIL: missing worker-a content"
    # prove it did NOT wait for timeout: timeout was 600s out; we fired in ~1-2s.
    assert latency_s < 30, "ARM1 FAIL: sweep waited for timeout-ish"
    log("ARM1 PASS: FIRED_COMPLETE, 1 combined PENDING, COMPLETE header, fired in", latency_s, "s (timeout 600s)")

    # ---- ARM 2: single-winner CAS — second sweep is a no-op, no duplicate rows ----
    again = sweep_once()
    comb2 = combined_rows(label)
    assert len(comb2) == 1, f"ARM2 FAIL: duplicate combined after 2nd sweep: {len(comb2)}"
    assert again == [], f"ARM2 FAIL: second sweep returned {again}"
    fire_rows = rows("SELECT COUNT(*) c FROM inbox WHERE sender_id LIKE 'barrier:%' "
                     f"AND id IN (SELECT combined_message_id FROM callback_barrier WHERE label='{label}')")
    log("ARM2 PASS: second sweep no-op; still exactly 1 combined row; barrier:", barrier_state(label))

    # ================= ARM 3: owner-gone precedence =================
    label3 = "g7-f71-ownergone"
    create_inbox_message(owner, wa, "task a3", dispatch_barrier={"label": label3, "timeout_seconds": 600})
    create_inbox_message(owner, wb, "task b3", dispatch_barrier={"label": label3, "timeout_seconds": 600})
    # drive both members ARRIVED via the orphan stamp (all-ARRIVED, owner still present)
    with SessionLocal.begin() as db:
        for m in db.query(CallbackBarrierMemberModel).join(CallbackBarrierModel).filter(
                CallbackBarrierModel.label == label3).all():
            m.state = "ARRIVED"
            m.arrived_at = now_utc()
    log("ARM3 all-ARRIVED ownergone-barrier:", barrier_state(label3), barrier_members(label3))
    # DELETE the owner terminal (real owner-gone: the row is gone, barrier left OPEN).
    # We delete the terminal ROW directly (not delete_terminal_and_warm_intent, which
    # eagerly closes owned barriers via the delete path and would short-circuit the
    # sweep's owner-gone check). This mirrors the live crash-loop shape reproduced by
    # the unit test `_orphan_owner`: owner gone, OPEN barrier orphaned. The SWEEP must
    # then run _resolve_barrier_owner_or_none -> owner-gone close, winning over COMPLETE.
    with SessionLocal.begin() as db:
        gone = db.query(dbmod.TerminalModel).filter(dbmod.TerminalModel.id == owner).delete()
    assert gone == 1, "ARM3: owner terminal must be gone"
    log("ARM3 owner terminal DELETED (row gone, barrier left OPEN):", barrier_state(label3))
    gone_ok = wait_until(lambda: barrier_state(label3)["state"] == "CANCELLED", 20)
    st3 = barrier_state(label3)
    log("ARM3 barrier after owner-gone sweep:", st3)
    comb3 = combined_rows(label3)
    # Only count THIS barrier's inbox rows — not the ARM1 combined callback which
    # legitimately fired to owner before owner was deleted.
    pending3 = rows("SELECT status, COUNT(*) c FROM inbox WHERE barrier_id IN "
                    "(SELECT id FROM callback_barrier WHERE label='%s') GROUP BY status" % label3)
    punch("arm3-after-sweep", {"state": st3, "combined": comb3,
                               "this-barrier-inbox-statuses": [dict(r) for r in pending3]})
    assert st3["state"] == "CANCELLED", f"ARM3 FAIL: expected CANCELLED got {st3['state']}"
    assert st3["close_reason"] == "owner_gone"
    assert len(comb3) == 0, f"ARM3 FAIL: minted a combined row to dead owner: {comb3}"
    assert not any(dict(r)["status"] == "pending" for r in pending3), f"ARM3 FAIL: PENDING row on this barrier: {pending3}"
    # held callbacks must be cancelled, not PENDING
    held3 = rows("SELECT status, COUNT(*) c FROM inbox WHERE barrier_id IN "
                 "(SELECT id FROM callback_barrier WHERE label='%s') GROUP BY status" % label3)
    log("ARM3 held-message statuses:", [dict(r) for r in held3])
    assert all(dict(r)["status"] == "cancelled" for r in held3), f"ARM3 FAIL held not cancelled: {held3}"
    # daemon must still be alive (no crash)
    loglines = SERVER_LOG.read_text().splitlines()
    crashes = [l for l in loglines if "callback_barrier_fire_failed" in l or "Traceback" in l]
    pid_alive = os.path.exists("/proc/%s" % "152091")
    log("ARM3 daemon crash-lines:", crashes if crashes else "NONE", "| server pid alive:", pid_alive)
    assert not crashes, f"ARM3 FAIL: daemon crashed: {crashes}"
    log("ARM3 PASS: owner gone -> CANCELLED/owner_gone, no PENDING, held cancelled, daemon alive")

    log("=== G7-F71 DRIVE DONE", now_utc().isoformat(), "===")
    print(json.dumps({"result": "PASS", "arms": ["arm1","arm2","arm3"]}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
