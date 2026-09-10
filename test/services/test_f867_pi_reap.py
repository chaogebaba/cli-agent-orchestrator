"""F867 (#723): pi_cli lanes were unreapable — no provider_session_id captured
so a planned hibernate refused session_artifact_missing, and the force delete
then 409'd resume_in_progress because the SESSION-wide teardown lease was blocked
by shared leases held for UNRELATED sibling terminals.

Two decisions, tested fail-before / pass-after:

* D1 — pi captures its known-at-spawn session identity (session-id == terminal_id,
  transcript <ts>_<terminal_id>.jsonl under the session dir) so the F829 root's
  provider_session_id is non-null. Before the first completed turn the artifact
  is still MISSING (a mid-turn crash is unrecoverable, D8) so hibernate still
  refuses; once a turn has written the file, hibernate is allowed/resumable.

* D2 — the per-terminal teardown lease is TERMINAL-scoped, so a force delete of
  terminal X is NOT blocked by a shared lifecycle lease held for a sibling Y (a
  concurrent create/resume in flight). A LIVE resume of THIS terminal (its
  provider-session uuid lease held) still 409s, and a stale resume CAS claim is
  reconciled on the way.

Mutants guarded:
  (a) skip the terminal-scoping (revert to session-wide exclusive) → the
      sibling-shared-lease force-delete test 409s (fails).
  (b) drop the pi session-id capture (base default None) → the resumable-capture
      test refuses hibernate forever (fails). See test_pi_cli_unit.py
      TestSpawnCapturedIdentity for the provider-level mutant guard.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import cli_agent_orchestrator.services.session_lifecycle_lease as lease_mod
from cli_agent_orchestrator.services import terminal_service


# ---------------------------------------------------------------------------
# Shared seed + delete-seam mocking
# ---------------------------------------------------------------------------
def _seed_pi_lane(d, tid: str, session: str, *, namespace: str | None = None) -> None:
    d.create_terminal(
        terminal_id=tid,
        tmux_session=session,
        tmux_window=f"win-{tid}",
        agent_profile="empirical_reviewer_lite",
        provider="pi_cli",
    )
    d.mint_spawn_identity(
        identity_key=f"conv_{tid}",
        provider="pi_cli",
        provider_namespace=namespace,
        agent_profile="empirical_reviewer_lite",
        model=None,
        reasoning_effort=None,
        origin_callback_ref=None,
        current_terminal_id=tid,
        cwd="/data/cao-scratch/x",
    )


def _mock_delete_seams(monkeypatch):
    monkeypatch.setattr(terminal_service, "get_backend", lambda: MagicMock())
    monkeypatch.setattr(
        terminal_service,
        "_delete_terminal_under_lease",
        lambda t, token, **kw: {
            "terminal_deleted": True,
            "resumable": False,
            "reason": "abandoned",
        },
    )
    monkeypatch.setattr(
        terminal_service,
        "status_monitor",
        MagicMock(get_boundary_observation=MagicMock(return_value=MagicMock(status=MagicMock()))),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
        lambda t: MagicMock(terminal_id=t),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease", lambda _t: None
    )


# ---------------------------------------------------------------------------
# D1: capture makes a pi lane resumable
# ---------------------------------------------------------------------------
def test_d1_pi_lane_with_captured_session_id_hibernates(real_sqlite_env, monkeypatch):
    """D1 (fail-before/pass-after): once pi's spawn identity is bound AND a turn
    has written the transcript, a NON-force delete hibernates the lane instead of
    refusing session_artifact_missing. Before the turn it still refuses (D8)."""
    import cli_agent_orchestrator.clients.database as d
    from cli_agent_orchestrator.providers.pi_cli import PiCliProvider
    from cli_agent_orchestrator.services import conversation_transition as ct

    tid = "d1capaaa"
    session = "cao-d1cap"
    sess_dir = Path(real_sqlite_env["tmp_path"]) / "pi-sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    _seed_pi_lane(d, tid, session)

    # The provider reports its known-at-spawn identity; the create path binds it.
    p = PiCliProvider(tid, session, f"win-{tid}", agent_profile="empirical_reviewer_lite")
    p.session_dir = sess_dir
    psid, pns, ploc = p.spawn_captured_identity()
    ct.attach_captured_uuid(
        tid,
        provider_session_id=psid,
        provider="pi_cli",
        provider_namespace=pns,
        artifact_locator=ploc,
    )

    # Before the first completed turn: no transcript → MISSING → refuse (correct).
    before = ct.evaluate_planned_hibernate(tid)
    assert before.allowed is False
    assert before.reason == "session_artifact_missing"

    # After a completed turn: pi wrote <ts>_<tid>.jsonl → hibernate allowed.
    (sess_dir / f"2026-09-09T20-00-00-000Z_{tid}.jsonl").write_text('{"x":1}\n')
    after = ct.evaluate_planned_hibernate(tid)
    assert after.allowed is True
    assert after.lifecycle == "hibernated"
    assert after.artifact_locator is not None


def test_d1_pi_lane_declared_nonresumable_reaps_cleanly(real_sqlite_env, monkeypatch):
    """D1: a pi lane that never captured an id (no spawn-identity bind) reaps
    cleanly on non-force with resumable=false and a typed reason (it does NOT
    hang or error) — the refusal is the clean, typed hibernate_refused."""
    import cli_agent_orchestrator.clients.database as d

    tid = "d1nonres"
    session = "cao-d1nonres"
    _seed_pi_lane(d, tid, session)  # NULL provider_session_id
    _mock_delete_seams(monkeypatch)

    r = terminal_service.delete_terminal(tid)  # non-force
    skipped = r["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["kind"] == "hibernate_refused"
    assert skipped[0]["reason"] == "session_artifact_missing"
    assert skipped[0]["provider"] == "pi_cli"


# ---------------------------------------------------------------------------
# D2: force delete is not blocked by shared leases on OTHER terminals
# ---------------------------------------------------------------------------
def test_d2_refused_hibernate_then_force_reaps_no_409(real_sqlite_env, monkeypatch):
    """D2: a refused non-force hibernate followed by force=True reaps with NO
    409 (the refusal never took/leaked a lease; force proceeds)."""
    import cli_agent_orchestrator.clients.database as d

    tid = "d2reffor"
    session = "cao-d2ref"
    _seed_pi_lane(d, tid, session)
    _mock_delete_seams(monkeypatch)

    r1 = terminal_service.delete_terminal(tid)  # refused
    assert r1["skipped"][0]["kind"] == "hibernate_refused"
    r2 = terminal_service.delete_terminal(tid, force=True)  # must not 409
    assert r2["reaped"] and r2["reaped"][0]["id"] == tid


def test_d2_force_not_blocked_by_sibling_shared_lease(real_sqlite_env, monkeypatch):
    """D2 (fail-before/pass-after; MUTANT (a) guard): a shared lifecycle lease
    held for ANOTHER terminal (a sibling create in flight on the same session)
    must NOT 409 the force delete of THIS terminal. Reverting to the session-wide
    exclusive lease makes this raise resume_in_progress."""
    import cli_agent_orchestrator.clients.database as d

    tid = "d2sibfor"
    session = "cao-d2sib"
    _seed_pi_lane(d, tid, session)
    _mock_delete_seams(monkeypatch)

    held = lease_mod.acquire_session_lifecycle_shared(session)  # sibling create
    assert held is not None
    try:
        r = terminal_service.delete_terminal(tid, force=True)
    finally:
        lease_mod.release_session_lifecycle_lease(held)
    assert r["reaped"] and r["reaped"][0]["id"] == tid


def test_d2_force_still_409s_while_resume_of_this_terminal_in_flight(real_sqlite_env, monkeypatch):
    """D2 (gate keeps its purpose): a LIVE resume of THIS terminal holds its
    provider-session uuid lease, so the force delete still 409s
    resume_in_progress; once the resume releases, the force delete succeeds."""
    import cli_agent_orchestrator.clients.database as d
    from cli_agent_orchestrator.services import conversation_transition as ct
    from cli_agent_orchestrator.services import provider_session_lease as psl

    tid = "d2resinf"
    session = "cao-d2res"
    uuid = tid  # pi's provider_session_id == terminal_id
    _seed_pi_lane(d, tid, session, namespace="/data/cao-scratch/x")
    # Bind the root's provider_session_id so the resume-in-flight guard sees it.
    ct.attach_captured_uuid(
        tid, provider_session_id=uuid, provider="pi_cli", provider_namespace="/data/cao-scratch/x"
    )
    _mock_delete_seams(monkeypatch)

    held = psl.acquire_provider_session_lease(uuid)  # live resume of THIS terminal
    assert held is not None
    try:
        with pytest.raises(RuntimeError, match="resume_in_progress"):
            terminal_service.delete_terminal(tid, force=True)
    finally:
        psl.release_provider_session_lease(held)

    # After the resume releases, the force delete succeeds.
    r = terminal_service.delete_terminal(tid, force=True)
    assert r["reaped"] and r["reaped"][0]["id"] == tid


def test_d2_stale_resume_claim_reconciled_on_force(real_sqlite_env, monkeypatch):
    """D2: the force-delete path reconciles a STALE resume CAS claim on the way
    (best-effort), so a stale claim does not have to wait out the 600s TTL. A
    non-stale/live resume is unaffected (its lease still blocks — see the
    resume-in-flight test)."""
    import cli_agent_orchestrator.clients.database as d

    tid = "d2stalec"
    session = "cao-d2stale"
    _seed_pi_lane(d, tid, session)
    _mock_delete_seams(monkeypatch)

    called = {"n": 0}
    real = terminal_service  # reconcile is imported inside _delete_terminal_inner

    import cli_agent_orchestrator.services.conversation_reconcile as cr

    def _spy():
        called["n"] += 1
        return []

    monkeypatch.setattr(cr, "reconcile_stale_claims", _spy)
    r = terminal_service.delete_terminal(tid, force=True)
    assert r["reaped"] and r["reaped"][0]["id"] == tid
    assert called["n"] >= 1, "force delete must reconcile stale resume claims on the way"


# ---------------------------------------------------------------------------
# D2: terminal-scoped lease unit semantics
# ---------------------------------------------------------------------------
def _fresh_lease():
    m = lease_mod
    with m._guard:
        m._shared.clear()
        m._exclusive.clear()
        m._terminal_exclusive.clear()
    return m


def test_terminal_exclusive_not_blocked_by_shared():
    """A terminal-scoped exclusive acquires even while a session shared lease is
    held (that shared lease belongs to some other terminal's create)."""
    m = _fresh_lease()
    shared = m.acquire_session_lifecycle_shared("s1")
    assert shared is not None
    tok = m.acquire_session_lifecycle_terminal_exclusive("s1", "term0001")
    assert tok is not None
    m.release_session_lifecycle_terminal_exclusive(tok)
    m.release_session_lifecycle_lease(shared)


def test_terminal_exclusive_conflicts_with_session_exclusive_both_ways():
    """A terminal-scoped exclusive and a full session teardown are mutually
    exclusive in BOTH directions (a session close never races a terminal delete)."""
    m = _fresh_lease()
    # Session exclusive held → terminal exclusive refused.
    sx = m.acquire_session_lifecycle_exclusive("s2")
    assert sx is not None
    assert m.acquire_session_lifecycle_terminal_exclusive("s2", "term0001") is None
    m.release_session_lifecycle_lease(sx)
    # Terminal exclusive held → session exclusive refused.
    tx = m.acquire_session_lifecycle_terminal_exclusive("s2", "term0001")
    assert tx is not None
    assert m.acquire_session_lifecycle_exclusive("s2") is None
    m.release_session_lifecycle_terminal_exclusive(tx)


def test_terminal_exclusive_same_id_is_exclusive():
    """Two deletes of the SAME terminal cannot both hold the lease."""
    m = _fresh_lease()
    a = m.acquire_session_lifecycle_terminal_exclusive("s3", "term0001")
    assert a is not None
    assert m.acquire_session_lifecycle_terminal_exclusive("s3", "term0001") is None
    # A DIFFERENT terminal on the same session is independent.
    b = m.acquire_session_lifecycle_terminal_exclusive("s3", "term0002")
    assert b is not None
    m.release_session_lifecycle_terminal_exclusive(a)
    m.release_session_lifecycle_terminal_exclusive(b)


# ===========================================================================
# F867 r2 — fold codex EMPIRICAL-GATE-NO (verdict-f867-r1)
# ===========================================================================


def test_r2_1_commit_hibernate_persists_locator_and_resume_arm_uses_it(
    real_sqlite_env, monkeypatch
):
    """R2-1 (verdict §1, the adversary shape): evaluate finds the JSONL →
    commit_hibernate persists it onto the root → the pi resume arm launches with
    ``--session <that JSONL>`` (no pi_artifact_locator_null).

    Fail-before: without R2-1a, commit_hibernate left root.artifact_locator None
    and prepare_resume raised pi_artifact_locator_null."""
    from pathlib import Path

    import cli_agent_orchestrator.clients.database as d
    from cli_agent_orchestrator.services import conversation_transition as ct

    tid = "r2locaaa"
    session = "cao-r2loc"
    sess_dir = Path(real_sqlite_env["tmp_path"]) / "pi-sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    jsonl = sess_dir / f"2026-09-09T20-00-00-000Z_{tid}.jsonl"
    jsonl.write_text('{"x":1}\n')

    d.create_terminal(
        terminal_id=tid,
        tmux_session=session,
        tmux_window=f"win-{tid}",
        agent_profile="empirical_reviewer_lite",
        provider="pi_cli",
    )
    # Bind the spawn identity with the session dir as namespace (as create does),
    # an explicit owner so the resume authorize gate passes, and a REAL cwd so
    # the resume cwd-reconstruction gate is satisfied (this test isolates the
    # locator, not cwd provenance).
    _cwd = str(real_sqlite_env["tmp_path"])
    d.mint_spawn_identity(
        identity_key=f"conv_{tid}",
        provider="pi_cli",
        provider_namespace=str(sess_dir),
        agent_profile="empirical_reviewer_lite",
        model=None,
        reasoning_effort=None,
        origin_callback_ref=None,
        current_terminal_id=tid,
        cwd=_cwd,
        owner_principal="owner-1",
    )
    ct.attach_captured_uuid(
        tid, provider_session_id=tid, provider="pi_cli", provider_namespace=str(sess_dir)
    )

    # Evaluate → allowed, locator discovered.
    decision = ct.evaluate_planned_hibernate(tid)
    assert decision.allowed and decision.lifecycle == "hibernated"
    assert decision.artifact_locator == str(jsonl)

    # Commit → the locator is PERSISTED onto the root (R2-1a).
    ct.commit_hibernate(decision)
    root = d.get_conversation_identity(f"conv_{tid}")
    assert root["artifact_locator"] == str(jsonl), "commit_hibernate must persist the JSONL locator"
    assert root["lifecycle"] == "hibernated"

    # The pi resume arm reads that locator and would launch pi with --session it,
    # WITHOUT raising pi_artifact_locator_null. Resolve through the F829 identity
    # path (owner authorises); assert the JSONL rides into session_artifact_path.
    from cli_agent_orchestrator.services import resume_service

    prepared = resume_service._prepare_resume_via_identity(
        resume_from=tid,
        requested_agent_profile=None,
        requested_working_directory=None,
        caller_principal="owner-1",
        inherit_pins=True,
    )
    assert prepared is not None
    assert prepared["session_artifact_path"] == str(jsonl)
    assert prepared["fork_context"].session_artifact_path == str(jsonl)


def test_r2_1_reap_resolver_reports_pi_resumable(real_sqlite_env, monkeypatch):
    """R2-1b (MUTANT (b) guard): a pi lane whose root has a captured id reports
    resumable=true with reason 'resumable' (was provider_pi_cli_not_resumable
    because the resolver read only legacy supports_resume)."""
    import cli_agent_orchestrator.clients.database as d
    from cli_agent_orchestrator.services import terminal_service as ts

    tid = "r2reapaa"
    session = "cao-r2reap"
    d.create_terminal(
        terminal_id=tid,
        tmux_session=session,
        tmux_window=f"win-{tid}",
        agent_profile="empirical_reviewer_lite",
        provider="pi_cli",
        provider_session_id=tid,
    )
    d.mint_spawn_identity(
        identity_key=f"conv_{tid}",
        provider="pi_cli",
        provider_namespace="/data/cao-scratch/x",
        agent_profile="empirical_reviewer_lite",
        model=None,
        reasoning_effort=None,
        origin_callback_ref=None,
        current_terminal_id=tid,
        cwd="/data/cao-scratch/x",
    )
    # Fill the terminal_identity row's provider_session_id (as create's bind does).
    from cli_agent_orchestrator.services import conversation_transition as ct

    ct.attach_captured_uuid(
        tid, provider_session_id=tid, provider="pi_cli", provider_namespace="/data/cao-scratch/x"
    )

    captured, resumable, reason = ts._resolve_reap_resume_key(
        tid, d.get_terminal_metadata(tid), force=False
    )
    assert resumable is True, (captured, resumable, reason)
    assert reason == "resumable"


def test_r2_1_provider_supports_resume_honors_declared_capabilities():
    """R2-1b unit: provider_supports_resume is True for pi_cli via
    declared_capabilities['resume'] even though it sets no legacy supports_resume."""
    from cli_agent_orchestrator.services.resume_service import provider_supports_resume

    assert provider_supports_resume("pi_cli") is True
    # codex/kiro (legacy flag) remain resumable; an unknown provider is not.
    assert provider_supports_resume("codex") is True
    assert provider_supports_resume("no_such_provider") is False


def test_r2_2_delayed_create_after_parent_force_delete_is_refused(real_sqlite_env, monkeypatch):
    """R2-2 (verdict §2 adversary, restores the §3 no-late-publication invariant):
    an admitted same-session create whose captured caller_id was force-deleted
    between admission and publication is REFUSED with E-CALLER-GONE and publishes
    NO child row.

    Fail-before: without the fail-closed revalidation, the child row was
    published pointing at a dead caller_id."""
    import asyncio
    from types import SimpleNamespace

    import cli_agent_orchestrator.clients.database as d
    from cli_agent_orchestrator.services import terminal_service as ts

    session = "cao-r2race"
    parent = "r2parent0"
    child = "r2child00"
    # Seed a live parent.
    d.create_terminal(
        terminal_id=parent,
        tmux_session=session,
        tmux_window=f"win-{parent}",
        agent_profile="developer",
        provider="pi_cli",
    )
    # Simulate the parent being under teardown (delete_terminal marks this BEFORE
    # killing the window) AND then gone.
    from cli_agent_orchestrator.services.teardown_intent_service import mark_teardown

    mark_teardown(parent)
    d.delete_terminal(parent)  # parent row removed → caller now missing

    # A backend/profile stub so create reaches the publication gate.
    monkeypatch.setattr(ts, "get_backend", lambda: MagicMock())
    monkeypatch.setattr(
        ts,
        "load_agent_profile",
        lambda _n: SimpleNamespace(
            sessionBrief=None,
            lifecycle=None,
            contextPolicy=None,
            name="developer",
            skills=None,
            allowedTools=None,
            role=None,
            mcpServers=None,
            engine=None,
            default_use_worktree=None,
        ),
    )

    with pytest.raises(RuntimeError, match="E-CALLER-GONE"):
        asyncio.run(
            ts.create_terminal(
                "pi_cli",
                "developer",
                session_name=session,
                new_session=False,
                caller_id=parent,
                terminal_id=child,
            )
        )
    # No child row was published.
    assert d.get_terminal_metadata(child) is None


def test_r3_delayed_create_after_parent_row_vanishes_is_refused(real_sqlite_env, monkeypatch):
    """F867 r3 MUTANT SENTINEL for the DB read itself.

    ``test_r2_2_delayed_create_after_parent_force_delete_is_refused`` marks the
    parent under teardown as well as deleting it, so the teardown-scope clause
    alone refuses it: a mutant that fabricates the caller metadata instead of
    reading it (``_caller_meta = {"id": caller_id}``) survives that test. Here
    the parent row simply VANISHES with no teardown intent recorded, so the
    ONLY thing that can refuse the publish is the real
    ``get_terminal_metadata`` lookup returning ``None`` — and the refusal must
    say ``missing``, not ``under_teardown``.
    """
    import asyncio
    from types import SimpleNamespace

    import cli_agent_orchestrator.clients.database as d
    from cli_agent_orchestrator.services import terminal_service as ts

    session = "cao-r3gone"
    parent = "r3parent0"
    child = "r3child00"
    d.create_terminal(
        terminal_id=parent,
        tmux_session=session,
        tmux_window=f"win-{parent}",
        agent_profile="developer",
        provider="pi_cli",
    )
    # Row removed, NO teardown intent recorded — the metadata read is the only
    # remaining signal that the caller is gone.
    d.delete_terminal(parent)

    from cli_agent_orchestrator.services.teardown_intent_service import (
        active_teardown_scope_keys,
    )

    keys = active_teardown_scope_keys()
    assert parent not in keys and session not in keys, (
        "this arm must NOT rely on the teardown clause; got teardown keys=" f"{sorted(keys)!r}"
    )

    monkeypatch.setattr(ts, "get_backend", lambda: MagicMock())
    monkeypatch.setattr(
        ts,
        "load_agent_profile",
        lambda _n: SimpleNamespace(
            sessionBrief=None,
            lifecycle=None,
            contextPolicy=None,
            name="developer",
            skills=None,
            allowedTools=None,
            role=None,
            mcpServers=None,
            engine=None,
            default_use_worktree=None,
        ),
    )

    with pytest.raises(RuntimeError, match=r"E-CALLER-GONE: caller 'r3parent0' missing"):
        asyncio.run(
            ts.create_terminal(
                "pi_cli",
                "developer",
                session_name=session,
                new_session=False,
                caller_id=parent,
                terminal_id=child,
            )
        )
    assert d.get_terminal_metadata(child) is None


def test_caller_row_lifecycle_change_between_check_and_publication_is_refused(
    real_sqlite_env, monkeypatch
):
    """Consistency requires caller-row liveness through publication ordering."""
    import asyncio
    from types import SimpleNamespace

    import cli_agent_orchestrator.clients.database as d
    from cli_agent_orchestrator.services import terminal_service as ts

    session = "cao-r4race"
    parent = "r4parent0"
    child = "r4child00"
    d.create_terminal(
        terminal_id=parent,
        tmux_session=session,
        tmux_window=f"win-{parent}",
        agent_profile="developer",
        provider="pi_cli",
    )

    # The production liveness read returns the caller row, and the row is then
    # deleted before control returns to the publication sequence: exactly the
    # interval between a check and a later write.
    real_get = ts.get_terminal_metadata
    deleted: dict[str, bool] = {"done": False}

    def _get_then_delete(terminal_id: str):
        meta = real_get(terminal_id)
        if terminal_id == parent and not deleted["done"]:
            deleted["done"] = True
            d.delete_terminal(parent)
        return meta

    monkeypatch.setattr(ts, "get_terminal_metadata", _get_then_delete)

    # Bounded stubs only — enough to let the normal create path complete and
    # actually REACH publication (the r2/r3 refusal arms never get this far).
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.create_window.side_effect = lambda _s, window, *a, **k: window
    backend.supports_event_inbox.return_value = False
    backend.set_window_parent = None
    monkeypatch.setattr(ts, "get_backend", lambda: backend)
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)
    _provider = AsyncMock()
    _provider.initialize.return_value = True
    _provider.shell_baseline = None
    monkeypatch.setattr(
        ts, "provider_manager", MagicMock(create_provider=MagicMock(return_value=_provider))
    )
    monkeypatch.setattr(ts, "fifo_manager", MagicMock())
    monkeypatch.setattr(ts, "_schedule_deferred_init", MagicMock())
    monkeypatch.setattr(
        ts,
        "load_agent_profile",
        lambda _n: SimpleNamespace(
            sessionBrief=None,
            lifecycle=None,
            contextPolicy=None,
            name="developer",
            skills=None,
            allowedTools=None,
            role=None,
            mcpServers=None,
            engine=None,
            default_use_worktree=None,
        ),
    )

    with pytest.raises(RuntimeError, match="E-CALLER-GONE"):
        asyncio.run(
            ts.create_terminal(
                "pi_cli",
                "developer",
                session_name=session,
                new_session=False,
                caller_id=parent,
                terminal_id=child,
            )
        )
    assert deleted["done"], "the caller row must have been deleted inside the interval"
    assert d.get_terminal_metadata(child) is None, "no orphan child row may survive the refusal"


def test_r4_caller_check_is_ordered_after_the_child_insert_in_both_writers():
    """F867 r4 ORDERING SENTINEL — the atomic step must not be split back apart.

    ``test_caller_row_lifecycle_change_between_check_and_publication_is_refused``
    proves the check exists inside the publication transaction, but it cannot
    tell a check placed BEFORE the child ``INSERT`` from one placed after: in
    that test the caller is already gone by either point. The placement is the
    whole invariant, though — only after the insert does this transaction hold
    the write lock, and only then is a concurrent delete unable to commit
    between the check and our commit. Pin the order structurally in both
    publication writers.
    """
    import ast
    import inspect

    import cli_agent_orchestrator.clients.database as d

    source = inspect.getsource(d)
    tree = ast.parse(source)
    writers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"create_terminal", "create_terminal_with_warm_intent"}
    }
    assert set(writers) == {"create_terminal", "create_terminal_with_warm_intent"}

    for name, fn in writers.items():
        flush_lines = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "flush"
        ]
        check_lines = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_assert_caller_live_in_publication_txn"
        ]
        assert flush_lines, f"{name}: no db.flush() found — writer shape changed"
        assert check_lines, (
            f"{name}: the atomic caller check is gone; the caller-liveness read and the "
            "child insert must stay one consistency boundary"
        )
        assert min(check_lines) > min(flush_lines), (
            f"{name}: caller check at line {min(check_lines)} runs BEFORE the child insert "
            f"flush at line {min(flush_lines)} — that is check-then-write again, and it "
            "reopens the window an orphan child row slips through"
        )
