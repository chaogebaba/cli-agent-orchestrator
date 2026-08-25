"""F439 (#294): server-side worker-terminal cap on assign/handoff.

These tests drive the counting + enforcement seam in ``terminal_service``
directly (fast, no tmux): the cap is DERIVED from live state on every call
(no persistent counter), so a restart cannot desync it. The AC cases:

- cap reached -> TerminalCapExceeded with structured E-TERMINAL-CAP payload
  (current_count, cap, reap_candidates) and NO side effect (nothing created);
- reap one idle worker -> the same call now succeeds (count drops below cap);
- supervisor terminal is NEVER counted;
- idle/warm workers COUNT toward the cap but ARE listed as reap candidates;
- cap <= 0 disables enforcement entirely (env=0 and negative file value).

The route-level atomicity ("no terminal row, no tmux window") is a direct
consequence of raising BEFORE any resource is created in ``create_terminal``:
the guard runs immediately after ``require_provider_admitted`` and before the
tmux/DB/worktree/provider path, so a refusal has nothing to unwind. That
ordering is asserted at the REAL seam by
``TestRealSeam::test_enforced_before_any_side_effect``, and the concurrent
admission race (F439 round 2 / BLOCKER 1) is pinned by
``TestConcurrentAdmission::test_concurrent_cap_minus_one_admits_exactly_one``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.terminal_service import (
    TerminalCapExceeded,
    _count_worker_terminals,
    _enforce_worker_terminal_cap,
    _resolve_worker_terminal_cap,
)

SUPERVISOR = "aaaaaaaa"
SESSION = "cao-test"


def _row(tid: str, profile: str = "developer", caller_id: str | None = SUPERVISOR):
    """A minimal terminal row shaped like list_terminals_by_session returns."""
    return {
        "id": tid,
        "agent_profile": profile,
        "caller_id": caller_id,
        "last_active": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


@pytest.fixture
def patch_session(monkeypatch):
    """Patch the live-state sources the counter reads: the session row list and
    per-terminal live status. Returns a setter the tests use to define the fleet.
    """
    state: dict[str, object] = {"rows": [], "status": {}}

    def _list(session_name):
        return list(state["rows"])

    def _status(tid):
        return state["status"].get(tid, TerminalStatus.PROCESSING)

    monkeypatch.setattr(ts, "list_terminals_by_session", _list)
    monkeypatch.setattr(ts.status_monitor, "get_status", _status)

    def _set(rows, status=None):
        state["rows"] = rows
        state["status"] = status or {}

    return _set


class TestResolveCap:
    """_resolve_worker_terminal_cap delegates to ConfigService precedence."""

    def test_default_ten(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda path, default=None, override=None: default),
        )
        assert _resolve_worker_terminal_cap() == 10

    def test_env_value(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda path, default=None, override=None: 3),
        )
        assert _resolve_worker_terminal_cap() == 3

    def test_malformed_falls_back_to_ten(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda path, default=None, override=None: "not-an-int"),
        )
        assert _resolve_worker_terminal_cap() == 10


class TestCounting:
    """_count_worker_terminals derives the count from live state."""

    def test_supervisor_never_counted(self, patch_session):
        patch_session(
            rows=[_row(SUPERVISOR, "supervisor", caller_id=None), _row("bbbbbbbb")],
            status={SUPERVISOR: TerminalStatus.IDLE, "bbbbbbbb": TerminalStatus.PROCESSING},
        )
        count, candidates = _count_worker_terminals(SESSION, SUPERVISOR)
        assert count == 1  # only the worker, not the supervisor
        assert [c["id"] for c in candidates] == []  # the one worker is busy

    def test_idle_workers_count_but_are_reap_candidates(self, patch_session):
        patch_session(
            rows=[
                _row(SUPERVISOR, "supervisor", caller_id=None),
                _row("bbbbbbbb"),
                _row("cccccccc"),
            ],
            status={
                "bbbbbbbb": TerminalStatus.IDLE,
                "cccccccc": TerminalStatus.PROCESSING,
            },
        )
        count, candidates = _count_worker_terminals(SESSION, SUPERVISOR)
        assert count == 2  # idle worker still counts toward RAM pressure
        ids = [c["id"] for c in candidates]
        assert ids == ["bbbbbbbb"]  # only the idle one is a reap candidate
        cand = candidates[0]
        assert cand["display_name"] == "developer-bbbbbbbb"
        assert cand["idle_since"] is not None

    def test_empty_session(self, patch_session):
        patch_session(rows=[], status={})
        count, candidates = _count_worker_terminals(SESSION, SUPERVISOR)
        assert count == 0
        assert candidates == []


class TestEnforcement:
    """_enforce_worker_terminal_cap fail-closes at/over cap; disabled at <=0."""

    def _cap(self, monkeypatch, value):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda path, default=None, override=None: value),
        )

    def test_under_cap_allows(self, patch_session, monkeypatch):
        self._cap(monkeypatch, 3)
        patch_session(rows=[_row("bbbbbbbb"), _row("cccccccc")])
        # 2 workers, cap 3 -> no raise
        _enforce_worker_terminal_cap(SESSION, SUPERVISOR)

    def test_at_cap_refuses_with_structured_payload(self, patch_session, monkeypatch):
        self._cap(monkeypatch, 2)
        patch_session(
            rows=[_row("bbbbbbbb"), _row("cccccccc")],
            status={"bbbbbbbb": TerminalStatus.IDLE, "cccccccc": TerminalStatus.PROCESSING},
        )
        with pytest.raises(TerminalCapExceeded) as ei:
            _enforce_worker_terminal_cap(SESSION, SUPERVISOR)
        exc = ei.value
        assert exc.code == "E-TERMINAL-CAP"
        assert exc.current_count == 2
        assert exc.cap == 2
        # the idle worker is offered as a reap candidate
        assert [c["id"] for c in exc.reap_candidates] == ["bbbbbbbb"]
        detail = exc.detail()
        assert detail["code"] == "E-TERMINAL-CAP"
        assert detail["current_count"] == 2
        assert detail["cap"] == 2
        assert detail["reap_candidates"][0]["display_name"] == "developer-bbbbbbbb"

    def test_reap_one_then_succeeds(self, patch_session, monkeypatch):
        """Cap reached -> refuse; reap one worker -> the same call now succeeds."""
        self._cap(monkeypatch, 2)
        patch_session(rows=[_row("bbbbbbbb"), _row("cccccccc")])
        with pytest.raises(TerminalCapExceeded):
            _enforce_worker_terminal_cap(SESSION, SUPERVISOR)
        # Supervisor reaps one idle worker -> the fleet drops to 1 worker.
        patch_session(rows=[_row("cccccccc")])
        _enforce_worker_terminal_cap(SESSION, SUPERVISOR)  # no raise

    def test_zero_disables(self, patch_session, monkeypatch):
        self._cap(monkeypatch, 0)
        patch_session(rows=[_row(f"{i:08x}") for i in range(50)])
        _enforce_worker_terminal_cap(SESSION, SUPERVISOR)  # no raise, cap disabled

    def test_negative_disables(self, patch_session, monkeypatch):
        self._cap(monkeypatch, -1)
        patch_session(rows=[_row(f"{i:08x}") for i in range(50)])
        _enforce_worker_terminal_cap(SESSION, SUPERVISOR)  # no raise

    def test_over_cap_refuses(self, patch_session, monkeypatch):
        """count strictly greater than cap still refuses (>= semantics)."""
        self._cap(monkeypatch, 1)
        patch_session(rows=[_row("bbbbbbbb"), _row("cccccccc")])
        with pytest.raises(TerminalCapExceeded) as ei:
            _enforce_worker_terminal_cap(SESSION, SUPERVISOR)
        assert ei.value.current_count == 2
        assert ei.value.cap == 1


class _CapSentinel(RuntimeError):
    """Marker raised by the patched enforcer so tests can prove it was reached."""


class TestCreateTerminalGuardPredicate:
    """create_terminal calls the enforcer ONLY for supervisor-created workers
    joining an EXISTING session (new_session=False AND caller_id set AND
    session_name set). Operator launches and the supervisor itself are exempt.
    """

    @pytest.fixture(autouse=True)
    def _patch_enforcer(self, monkeypatch):
        # F439 r3: admission now enters via the async ``_reserve_worker_slot``
        # seam (lock-free count + locked reserve) rather than the synchronous
        # ``_enforce_worker_terminal_cap``. The guard PREDICATE that decides
        # whether admission runs at all is unchanged, so patch the reserve seam
        # to prove the same predicate (new_session=False AND caller_id AND
        # session_name) still gates it.
        async def _raise(session_name, supervisor_id):
            raise _CapSentinel(f"enforced:{session_name}:{supervisor_id}")

        monkeypatch.setattr(ts, "_reserve_worker_slot", _raise)
        # Neutralize provider admission so the guard is the first thing reached.
        monkeypatch.setattr(ts, "require_provider_admitted", lambda provider: None)

    async def _call(self, **kwargs):
        return await ts.create_terminal(
            provider="mock_cli",
            agent_profile="developer",
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_assign_shape_is_enforced(self):
        """new_session=False + caller_id + session_name -> enforcer runs."""
        with pytest.raises(_CapSentinel, match="enforced:cao-test:aaaaaaaa"):
            await self._call(
                session_name="cao-test",
                new_session=False,
                caller_id=SUPERVISOR,
            )

    @pytest.mark.asyncio
    async def test_operator_new_session_is_exempt(self):
        """new_session=True (operator launch) -> enforcer NOT reached."""
        with pytest.raises(Exception) as ei:
            await self._call(
                session_name="cao-test",
                new_session=True,
                caller_id=None,
                working_directory="/nonexistent/does/not/exist/f439",
            )
        assert not isinstance(ei.value, _CapSentinel)

    @pytest.mark.asyncio
    async def test_no_caller_id_is_exempt(self):
        """new_session=False but caller_id=None (not a supervised worker)."""
        with pytest.raises(Exception) as ei:
            await self._call(
                session_name="cao-test",
                new_session=False,
                caller_id=None,
                working_directory="/nonexistent/does/not/exist/f439",
            )
        assert not isinstance(ei.value, _CapSentinel)


# ─────────────────────────────────────────────────────────────────────────────
# Real-seam tests (F439 round 2): drive the REAL async create_terminal with its
# resource dependencies stubbed, so the REAL admission lock + REAL cap count run.
# ─────────────────────────────────────────────────────────────────────────────

import contextlib
import itertools
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.session_lifecycle_lease import SessionLifecycleLeaseToken

EXISTING_WORKER = "eeee0001"


@contextlib.contextmanager
def _real_seam(published_rows, *, cap, id_counter, count_hook=None, status_map=None):
    """Patch create_terminal's resource deps while keeping the real lock + count.

    ``published_rows`` is the shared live fleet: the patched ``db_create_terminal``
    appends the new row to it, and the patched ``list_terminals_by_session``
    returns it — so the cap count reflects real publication ordering. ``count_hook``
    (optional) is awaited/called inside the count to force an interleaving that a
    lock-less implementation would lose. ``status_map`` maps terminal id -> live
    status (default PROCESSING = busy, no reap candidates).
    """
    status_map = status_map or {}

    def _list(session_name):
        return list(published_rows)

    def _status(tid):
        return status_map.get(tid, TerminalStatus.PROCESSING)

    def _db_create(terminal_id, tmux_session, tmux_window, provider, *args, **kw):
        # create_terminal calls positionally: (tid, session, window, provider,
        # agent_profile, allowed_tools, caller_id=..., ...). Read profile from
        # the positional tail and caller_id from kwargs.
        agent_profile = args[0] if args else kw.get("agent_profile")
        published_rows.append(
            {
                "id": terminal_id,
                "agent_profile": agent_profile,
                "caller_id": kw.get("caller_id"),
                "last_active": None,
            }
        )
        return {"id": terminal_id}

    orig_count = ts._count_worker_terminals
    orig_count_detailed = ts._count_worker_terminals_detailed

    def _counting(session_name, supervisor_id):
        if count_hook is not None:
            count_hook()
        return orig_count(session_name, supervisor_id)

    def _counting_detailed(session_name, supervisor_id):
        # F439 r5: the admission path counts via ``_count_worker_terminals_detailed``
        # (it needs the worker id set for the ledger-authoritative live count), so
        # the interleaving ``count_hook`` must fire here too — otherwise a
        # concurrency test's forced interleaving would be bypassed.
        if count_hook is not None:
            count_hook()
        return orig_count_detailed(session_name, supervisor_id)

    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.create_window.side_effect = lambda session, window, *a, **k: window
    backend.supports_event_inbox.return_value = False
    backend.set_window_parent = None

    provider = AsyncMock()
    provider.initialize.return_value = True
    provider.shell_baseline = None

    # F439 r3: worktree stub. When a test drives use_worktree=True, create_terminal
    # runs ``await asyncio.to_thread(worktree_service.find_repo_root/create_worktree)``
    # — a REAL loop-yielding seam BETWEEN the lock-free count and DB publication.
    # That yield lets every same-loop racer pass the count before any row is
    # published, so ONLY the reservation ledger (not a committed row) can catch
    # oversubscription — which makes the same-loop async test independently kill a
    # no-reservation / fresh-count-per-call mutant (round-2 SHOULD).
    _worktrees = MagicMock()
    _worktrees.find_repo_root.side_effect = lambda cwd: cwd
    _worktrees.create_worktree.side_effect = lambda repo, tid: tempfile.mkdtemp(prefix=f"wt-{tid}-")
    _worktrees.branch_for.side_effect = lambda tid: f"cao/{tid}"

    def _resolve_cap(*a, **k):
        return cap

    def _gen_id():
        return next(id_counter)

    with (
        patch.object(ts, "_resolve_worker_terminal_cap", _resolve_cap),
        patch.object(ts, "list_terminals_by_session", _list),
        patch.object(ts, "_count_worker_terminals", _counting),
        patch.object(ts, "_count_worker_terminals_detailed", _counting_detailed),
        patch.object(ts, "db_create_terminal", _db_create),
        patch.object(ts, "delete_terminals_by_session", MagicMock()),
        patch.object(ts, "generate_terminal_id", _gen_id),
        patch.object(ts, "generate_window_name", lambda profile, tid: f"{profile}-{tid}"),
        patch.object(ts.status_monitor, "get_status", _status),
        patch.object(ts, "provider_manager") as pm,
        patch.object(ts, "fifo_manager", MagicMock()),
        patch.object(ts, "_schedule_deferred_init", MagicMock()),
        patch.object(
            ts,
            "load_agent_profile",
            lambda name: AgentProfile(name="developer", description="dev"),
        ),
        patch.object(
            ts,
            "get_provider_class",
            lambda name: type(
                "Cap",
                (),
                {"supports_seed_resume_identity": False, "has_process_child": False},
            ),
        ),
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease."
            "acquire_session_lifecycle_shared",
            lambda session_name: SessionLifecycleLeaseToken(
                session_name=session_name, mode="shared", nonce="t"
            ),
        ),
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease."
            "release_session_lifecycle_lease",
            lambda token: None,
        ),
        patch.object(ts, "worktree_service", _worktrees),
        patch("cli_agent_orchestrator.backends.registry._backend", backend),
    ):
        pm.create_provider.return_value = provider
        yield {"backend": backend, "provider": provider, "pm": pm}


async def _create_worker(session="cao-race", tmpdir="/tmp", use_worktree=False):
    return await ts.create_terminal(
        provider="mock_cli",
        agent_profile="developer",
        session_name=session,
        new_session=False,
        caller_id=SUPERVISOR,
        working_directory=tmpdir,
        use_worktree=use_worktree,
    )


class TestConcurrentAdmission:
    """F439 round 2 / BLOCKER 1: concurrent creates must not oversubscribe."""

    @pytest.mark.asyncio
    async def test_concurrent_cap_minus_one_admits_exactly_one(self, tmp_path):
        """cap 2, one existing worker, N concurrent SAME-LOOP real creates →
        exactly ONE success and N-1 TerminalCapExceeded; the fleet grows by one.

        Drives ``use_worktree=True`` so create_terminal runs a real
        ``await asyncio.to_thread(worktree_service...)`` BETWEEN the lock-free
        count and DB publication. That await yields the loop, so all N tasks pass
        the count before any row is published — meaning ONLY the per-session
        reservation ledger (a peer's granted-but-unpublished slot) can catch the
        oversubscription. A no-reservation / fresh-count-per-call mutant admits
        all N here, so this same-loop test independently kills that mutant (round-2
        SHOULD). ``count_hook`` still bumps a counter to assert every racer counted.
        """
        published = [
            {
                "id": EXISTING_WORKER,
                "agent_profile": "developer",
                "caller_id": SUPERVISOR,
                "last_active": None,
            },
        ]
        ids = itertools.count(1)

        def _id():
            return f"new0{next(ids):04d}"

        # itertools-style counter object exposing __next__ for generate_terminal_id
        class _C:
            def __next__(self):
                return _id()

        n = 4
        # Assert every racer's count actually ran (all N reach the count seam).
        yielded = {"n": 0}

        def _hook():
            yielded["n"] += 1

        with _real_seam(published, cap=2, id_counter=_C(), count_hook=_hook) as seam:
            results = await asyncio.gather(
                *[_create_worker(tmpdir=str(tmp_path), use_worktree=True) for _ in range(n)],
                return_exceptions=True,
            )

        successes = [r for r in results if not isinstance(r, BaseException)]
        cap_errors = [r for r in results if isinstance(r, TerminalCapExceeded)]
        others = [
            r
            for r in results
            if isinstance(r, BaseException) and not isinstance(r, TerminalCapExceeded)
        ]

        assert others == [], f"unexpected errors: {others}"
        assert yielded["n"] == n, f"expected all {n} racers to count, got {yielded['n']}"
        assert len(successes) == 1, f"expected exactly 1 admit, got {len(successes)}"
        assert len(cap_errors) == n - 1, f"expected {n - 1} refusals, got {len(cap_errors)}"
        # No orphans: the fleet grew by exactly the one admitted worker.
        assert len(published) == 2  # the pre-existing worker + exactly one new row
        # Each refusal carried the structured surface.
        for e in cap_errors:
            assert e.code == "E-TERMINAL-CAP"
            assert e.cap == 2

    @pytest.mark.asyncio
    async def test_concurrent_under_cap_all_admitted(self, tmp_path):
        """cap 10, no existing workers, 4 concurrent creates → all 4 admitted,
        fleet grows by 4 (the lock serializes but never falsely refuses)."""
        published = []
        ids = itertools.count(1)

        class _C:
            def __next__(self):
                return f"ok0{next(ids):04d}"

        with _real_seam(published, cap=10, id_counter=_C()):
            results = await asyncio.gather(
                *[_create_worker(tmpdir=str(tmp_path)) for _ in range(4)],
                return_exceptions=True,
            )
        assert all(not isinstance(r, BaseException) for r in results), results
        assert len(published) == 4

    def test_thread_based_race_admits_exactly_one(self, tmp_path):
        """Mirror the gate's harness EXACTLY: 4 THREADS, each its own event loop,
        a threading.Barrier in the count dependency so all four observe the same
        pre-create snapshot. This is the scenario an asyncio.Lock CANNOT close
        (per-loop locks don't serialize across loops) — the process-wide
        threading.Lock must. Required: exactly one success, three refusals, and
        the fleet grows by exactly one (no orphan rows).

        Patches are installed ONCE in the main thread (``unittest.mock.patch``
        mutates shared module state and is NOT thread-safe if applied per
        worker), with thread-safe fakes; the workers only call create_terminal.
        """
        import contextlib as _ctx
        import threading

        published = [
            {
                "id": EXISTING_WORKER,
                "agent_profile": "developer",
                "caller_id": SUPERVISOR,
                "last_active": None,
            },
        ]
        pub_lock = threading.Lock()
        ids = itertools.count(1)
        id_lock = threading.Lock()
        barrier = threading.Barrier(4)
        results: list = []
        res_lock = threading.Lock()

        def _list(session_name):
            with pub_lock:
                return list(published)

        def _gen_id():
            with id_lock:
                return f"thr0{next(ids):04d}"

        def _db_create(terminal_id, *a, **kw):
            with pub_lock:
                published.append(
                    {
                        "id": terminal_id,
                        "agent_profile": "developer",
                        "caller_id": SUPERVISOR,
                        "last_active": None,
                    }
                )
            return {"id": terminal_id}

        orig_count = ts._count_worker_terminals

        def _counting(session_name, supervisor_id):
            return orig_count(session_name, supervisor_id)

        backend = MagicMock()
        backend.session_exists.return_value = True
        backend.create_window.side_effect = lambda session, window, *a, **k: window
        backend.supports_event_inbox.return_value = False
        backend.set_window_parent = None

        provider = AsyncMock()
        provider.initialize.return_value = True
        provider.shell_baseline = None
        pm = MagicMock()
        pm.create_provider.return_value = provider

        def _worker():
            # Barrier OUTSIDE the create call so all four threads slam the
            # admission seam together; the process-wide lock then serializes
            # them. (A barrier inside the counted critical section would
            # deadlock, since the lock admits one thread at a time.)
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            try:
                r = asyncio.run(
                    ts.create_terminal(
                        provider="mock_cli",
                        agent_profile="developer",
                        session_name="cao-thr",
                        new_session=False,
                        caller_id=SUPERVISOR,
                        working_directory=str(tmp_path),
                    )
                )
                with res_lock:
                    results.append(("ok", getattr(r, "id", r)))
            except TerminalCapExceeded as e:
                with res_lock:
                    results.append(("cap", e.cap))
            except BaseException as e:  # noqa: BLE001
                with res_lock:
                    results.append(("err", repr(e)))

        with _ctx.ExitStack() as stack:
            for target, repl in [
                ("_resolve_worker_terminal_cap", lambda *a, **k: 2),
                ("list_terminals_by_session", _list),
                ("_count_worker_terminals", _counting),
                ("db_create_terminal", _db_create),
                ("delete_terminals_by_session", MagicMock()),
                ("generate_terminal_id", _gen_id),
                ("generate_window_name", lambda p, t: f"{p}-{t}"),
                ("provider_manager", pm),
                ("fifo_manager", MagicMock()),
                ("_schedule_deferred_init", MagicMock()),
                ("load_agent_profile", lambda n: AgentProfile(name="developer", description="d")),
                (
                    "get_provider_class",
                    lambda n: type(
                        "C",
                        (),
                        {"supports_seed_resume_identity": False, "has_process_child": False},
                    ),
                ),
            ]:
                stack.enter_context(patch.object(ts, target, repl))
            stack.enter_context(
                patch.object(ts.status_monitor, "get_status", lambda tid: TerminalStatus.PROCESSING)
            )
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.session_lifecycle_lease."
                    "acquire_session_lifecycle_shared",
                    lambda s: SessionLifecycleLeaseToken(session_name=s, mode="shared", nonce="t"),
                )
            )
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.session_lifecycle_lease."
                    "release_session_lifecycle_lease",
                    lambda t: None,
                )
            )
            stack.enter_context(patch("cli_agent_orchestrator.backends.registry._backend", backend))

            threads = [threading.Thread(target=_worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        oks = [r for r in results if r[0] == "ok"]
        caps = [r for r in results if r[0] == "cap"]
        errs = [r for r in results if r[0] == "err"]
        assert errs == [], f"unexpected errors: {errs}"
        assert len(oks) == 1, f"expected exactly 1 admit, got {len(oks)}: {results}"
        assert len(caps) == 3, f"expected 3 refusals, got {len(caps)}: {results}"
        # No orphans: exactly one new row beyond the pre-existing worker.
        assert len(published) == 2

    def test_thread_race_barrier_inside_listing_admits_exactly_one(self, tmp_path):
        """F439 round 3 / BLOCKER regression: the barrier lives INSIDE the count's
        ``list_terminals_by_session`` callout — the exact shape of the mandatory
        round-1 gate probe. All four threads MUST reach that barrier concurrently,
        which is only possible if the admission lock is NOT held across the
        listing (the round-2 bug held it, so only the lock-holder reached the
        barrier and it broke). Required: exactly one admit, three
        ``TerminalCapExceeded``, and the fleet grows by exactly one — never a
        ``BrokenBarrierError``.

        This is the committed counterpart to the gate's verbatim probe; the
        sibling ``test_thread_based_race_admits_exactly_one`` keeps its barrier
        OUTSIDE the create and does NOT exercise this concurrency property.
        """
        import contextlib as _ctx
        import threading

        published = [
            {
                "id": EXISTING_WORKER,
                "agent_profile": "developer",
                "caller_id": SUPERVISOR,
                "last_active": None,
            },
        ]
        pub_lock = threading.Lock()
        ids = itertools.count(1)
        id_lock = threading.Lock()
        n = 4
        barrier = threading.Barrier(n)
        results: list = []
        res_lock = threading.Lock()

        def _list(session_name):
            # Barrier INSIDE the listing: every racer must arrive here at once.
            # If the admission lock were held across the count, only the holder
            # would reach this and the barrier would break.
            barrier.wait(timeout=10)
            with pub_lock:
                return list(published)

        def _gen_id():
            with id_lock:
                return f"bar0{next(ids):04d}"

        def _db_create(terminal_id, *a, **kw):
            with pub_lock:
                published.append(
                    {
                        "id": terminal_id,
                        "agent_profile": "developer",
                        "caller_id": SUPERVISOR,
                        "last_active": None,
                    }
                )
            return {"id": terminal_id}

        backend = MagicMock()
        backend.session_exists.return_value = True
        backend.create_window.side_effect = lambda session, window, *a, **k: window
        backend.supports_event_inbox.return_value = False
        backend.set_window_parent = None

        provider = AsyncMock()
        provider.initialize.return_value = True
        provider.shell_baseline = None
        pm = MagicMock()
        pm.create_provider.return_value = provider

        def _worker():
            try:
                r = asyncio.run(
                    ts.create_terminal(
                        provider="mock_cli",
                        agent_profile="developer",
                        session_name="cao-bar",
                        new_session=False,
                        caller_id=SUPERVISOR,
                        working_directory=str(tmp_path),
                    )
                )
                with res_lock:
                    results.append(("ok", getattr(r, "id", r)))
            except TerminalCapExceeded as e:
                with res_lock:
                    results.append(("cap", e.cap))
            except BaseException as e:  # noqa: BLE001
                with res_lock:
                    results.append(("err", repr(e)))

        with _ctx.ExitStack() as stack:
            for target, repl in [
                ("_resolve_worker_terminal_cap", lambda *a, **k: 2),
                ("list_terminals_by_session", _list),
                ("db_create_terminal", _db_create),
                ("delete_terminals_by_session", MagicMock()),
                ("generate_terminal_id", _gen_id),
                ("generate_window_name", lambda p, t: f"{p}-{t}"),
                ("provider_manager", pm),
                ("fifo_manager", MagicMock()),
                ("_schedule_deferred_init", MagicMock()),
                ("load_agent_profile", lambda n: AgentProfile(name="developer", description="d")),
                (
                    "get_provider_class",
                    lambda n: type(
                        "C",
                        (),
                        {"supports_seed_resume_identity": False, "has_process_child": False},
                    ),
                ),
            ]:
                stack.enter_context(patch.object(ts, target, repl))
            stack.enter_context(
                patch.object(ts.status_monitor, "get_status", lambda tid: TerminalStatus.PROCESSING)
            )
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.session_lifecycle_lease."
                    "acquire_session_lifecycle_shared",
                    lambda s: SessionLifecycleLeaseToken(session_name=s, mode="shared", nonce="t"),
                )
            )
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.session_lifecycle_lease."
                    "release_session_lifecycle_lease",
                    lambda t: None,
                )
            )
            stack.enter_context(patch("cli_agent_orchestrator.backends.registry._backend", backend))

            threads = [threading.Thread(target=_worker) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        oks = [r for r in results if r[0] == "ok"]
        caps = [r for r in results if r[0] == "cap"]
        errs = [r for r in results if r[0] == "err"]
        assert errs == [], f"unexpected errors (BrokenBarrierError = the r2 bug): {errs}"
        assert len(oks) == 1, f"expected exactly 1 admit, got {len(oks)}: {results}"
        assert len(caps) == n - 1, f"expected {n - 1} refusals, got {len(caps)}: {results}"
        assert len(published) == 2  # pre-existing worker + exactly one new row

    def test_admission_lock_is_shared_per_session_kills_fresh_lock_mutant(self):
        """SHOULD (F439 r2/r3): a fresh-lock-per-call mutant
        (``_cap_admission_lock = lambda _s: threading.Lock()``) must be killed by
        a direct test. The whole serialization rests on ALL racers for a session
        sharing ONE lock instance; a per-call fresh lock serializes nothing.
        Assert the lock is the SAME object across calls for a session, and a
        DIFFERENT object across sessions. The mutant returns a new lock each call,
        so ``is`` fails and this test goes red — independent of any race timing.
        """
        import threading as _thr

        a1 = ts._cap_admission_lock("cao-shared-a")
        a2 = ts._cap_admission_lock("cao-shared-a")
        b1 = ts._cap_admission_lock("cao-shared-b")
        assert a1 is a2, "same session must return the SAME lock instance (mutant: fresh lock)"
        assert a1 is not b1, "different sessions must have distinct locks"
        assert isinstance(a1, type(_thr.Lock()))


class TestRealSeam:
    """Real-seam handoff/side-effect/boundary coverage (F439 round 2 SHOULD)."""

    @pytest.mark.asyncio
    async def test_enforced_before_any_side_effect(self, tmp_path):
        """At cap, the REAL create_terminal raises TerminalCapExceeded and creates
        NO db row, NO tmux window, NO provider — nothing to unwind."""
        published = [
            {
                "id": "eeee0001",
                "agent_profile": "developer",
                "caller_id": SUPERVISOR,
                "last_active": None,
            },
            {
                "id": "eeee0002",
                "agent_profile": "developer",
                "caller_id": SUPERVISOR,
                "last_active": None,
            },
        ]

        class _C:
            def __next__(self):
                return "should-not-be-used"

        with _real_seam(published, cap=2, id_counter=_C()) as seam:
            with pytest.raises(TerminalCapExceeded) as ei:
                await _create_worker(tmpdir=str(tmp_path))

        assert ei.value.current_count == 2
        # No side effects: fleet unchanged, no window created, no provider built.
        assert len(published) == 2
        seam["backend"].create_window.assert_not_called()
        seam["pm"].create_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_handoff_run_step_reaches_cap_seam(self, tmp_path):
        """A real run_agent_step (handoff) at cap raises TerminalCapExceeded and
        creates no row/window — the handoff path shares the same seam as assign."""
        from cli_agent_orchestrator.services import agent_step

        published = [
            {
                "id": "eeee0001",
                "agent_profile": "developer",
                "caller_id": SUPERVISOR,
                "last_active": None,
            },
            {
                "id": "eeee0002",
                "agent_profile": "developer",
                "caller_id": SUPERVISOR,
                "last_active": None,
            },
        ]

        class _C:
            def __next__(self):
                return "should-not-be-used"

        with _real_seam(published, cap=2, id_counter=_C()) as seam:
            with patch.object(ts, "seed_resume_bootstrap", new=AsyncMock(return_value=None)):
                with pytest.raises(TerminalCapExceeded):
                    await agent_step.run_agent_step(
                        provider="mock_cli",
                        agent="developer",
                        prompt="do work",
                        session_name="cao-race",
                        caller_id=SUPERVISOR,
                        working_directory=str(tmp_path),
                    )
        assert len(published) == 2
        seam["backend"].create_window.assert_not_called()

    def test_missing_last_active_yields_null_idle_since(self, patch_session):
        """An IDLE worker row with last_active=None → idle_since is null."""
        patch_session(
            rows=[
                {
                    "id": "bbbbbbbb",
                    "agent_profile": "developer",
                    "caller_id": SUPERVISOR,
                    "last_active": None,
                }
            ],
            status={"bbbbbbbb": TerminalStatus.IDLE},
        )
        count, candidates = _count_worker_terminals(SESSION, SUPERVISOR)
        assert count == 1
        assert candidates[0]["idle_since"] is None

    def test_provider_not_ready_row_counts(self, patch_session):
        """A provider-not-ready (UNKNOWN) worker still counts toward the cap and
        is NOT offered as a reap candidate (only IDLE workers are)."""
        patch_session(
            rows=[
                {
                    "id": "bbbbbbbb",
                    "agent_profile": "developer",
                    "caller_id": SUPERVISOR,
                    "last_active": None,
                },
            ],
            status={"bbbbbbbb": TerminalStatus.UNKNOWN},
        )
        count, candidates = _count_worker_terminals(SESSION, SUPERVISOR)
        assert count == 1
        assert candidates == []


# ---------------------------------------------------------------------------
# F439 (#294) round 5 / BLOCKER 1 — ledger-authoritative admission regressions.
#
# These replace the round-4 epoch-internal bug-demonstration probes. The r4
# design retired a reservation only AFTER its row was visible (via a publish
# epoch), so for the instant between "row visible" and "reservation retired"
# one worker occupied TWO admission units and a concurrent peer was SPURIOUSLY
# refused at cap-minus-one. Round 5 makes the admission truth a fenced ledger
# (``reservations`` + DB rows NOT attributable to a still-held reservation id),
# so the same worker is never counted twice and never zero times.
# ---------------------------------------------------------------------------
import threading as _threading

from cli_agent_orchestrator.services.terminal_service import (
    _cap_admission_lock,
    _cap_admission_locks,
    _cap_publishing_ids,
    _cap_reservations,
    _count_worker_terminals_detailed,
    _live_worker_count,
    _mark_publishing,
    _release_worker_slot,
    _reserve_worker_slot,
)


def _reset_cap_session(session: str) -> None:
    """Clear all per-session cap ledger state so a test starts from quiescent.

    F439 r6: the admission LOCK, the GENERATION, and the token SEQUENCE are
    intentionally NEVER reclaimed by production (that reclamation was the r5 ABA).
    A TEST, though, may reset the generation/sequence directly (it holds the
    lock) to start from a known-clean slate; only production is forbidden from
    doing so mid-flight.
    """
    lock = _cap_admission_lock(session)
    with lock:
        _cap_reservations.pop(session, None)
        _cap_publishing_ids.pop(session, None)
        ts._cap_gen.pop(session, None)
        ts._cap_token_seq.pop(session, None)


def _seed_reservation(session: str, terminal_id: str | None = None) -> int:
    """Book a real reservation token under the lock; optionally mark it publishing.

    F439 r6 / BLOCKER 2: reservations are a live-token SET and every exclusion is
    bound to a token, so a test can no longer fake in-flight state with a bare
    ``_cap_reservations[session] = 1``. This issues a genuine token (as
    ``_reserve_worker_slot`` would) and, when ``terminal_id`` is given, registers
    the matching publishing exclusion — reproducing a real in-flight publisher.
    Returns the issued token.
    """
    lock = _cap_admission_lock(session)
    with lock:
        seq = ts._cap_token_seq.get(session, 0) + 1
        ts._cap_token_seq[session] = seq
        tokens = _cap_reservations.get(session)
        if tokens is None:
            _cap_reservations[session] = {seq}
        else:
            tokens.add(seq)
        ts._cap_gen[session] = ts._cap_gen.get(session, 0) + 1
    if terminal_id is not None:
        _mark_publishing(session, terminal_id, seq)
    return seq


class TestR5LedgerAuthoritativeAdmission:
    """The row-visible-before-retire window (r4 BLOCKER 1) is closed and cannot
    over-admit either (carried forward to r6 on the token-bound ledger)."""

    @pytest.mark.asyncio
    async def test_row_visible_while_reserved_admits_peer_not_spurious_refuse(self, monkeypatch):
        """r4 BLOCKER 1 INVERTED (deterministic). Reproduce the exact ledger state
        of the r4 gap — publisher A's row IS visible in the listing AND A's
        reservation IS still held (A registered in ``publishing_ids``) — and prove
        a concurrent peer B at cap-minus-one is ADMITTED, not spuriously refused.

        r4 computed ``count(2) + reservations(1) = 3`` and refused B at cap 3.
        r5/r6 compute ``reservations(1) + live(1)`` where ``live`` EXCLUDES A's row
        (its id maps to a LIVE token in ``publishing_ids``) → 2 < 3 → B admitted.
        Both directions are asserted: B admitted here, and the negative control
        below still refuses the 4th arrival at cap."""
        session = "cao-r5-invert"
        supervisor = "sup-r5-invert"
        _reset_cap_session(session)
        a_id = "aworker1"
        # DB truth during A's gap: existing worker + A's now-visible row.
        db_rows = [_row(EXISTING_WORKER, caller_id=supervisor), _row(a_id, caller_id=supervisor)]
        monkeypatch.setattr(ts, "_resolve_worker_terminal_cap", lambda: 3)
        monkeypatch.setattr(ts, "list_terminals_by_session", lambda _s: list(db_rows))
        monkeypatch.setattr(ts.status_monitor, "get_status", lambda tid: TerminalStatus.PROCESSING)

        # Reproduce A's in-flight state: reservation held AND row visible+excluded.
        _seed_reservation(session, a_id)

        # Confirm the gap really is the r4 double-count trap: 2 rows visible while
        # a reservation is held. r4 would have summed these to 3.
        _c, _r, gap_ids = _count_worker_terminals_detailed(session, supervisor)
        assert len(gap_ids) == 2 and a_id in gap_ids

        # B (peer) MUST be admitted: reservations(1) + live(existing only = 1) = 2 < 3.
        token_b = await _reserve_worker_slot(session, supervisor)
        assert token_b is not None
        # Ledger now: A + B reserved (2 live tokens), A's row still excluded from live.
        assert len(_cap_reservations[session]) == 2

        # NEGATIVE CONTROL: a 4th arrival now exceeds cap 3 and IS refused.
        # reservations(2) + live(1) = 3 >= 3.
        with pytest.raises(TerminalCapExceeded) as refused:
            await _reserve_worker_slot(session, supervisor)
        assert refused.value.cap == 3
        assert refused.value.current_count == 3
        _reset_cap_session(session)

    @pytest.mark.asyncio
    async def test_publish_transition_moves_unit_from_reservation_to_live_no_gap(self, monkeypatch):
        """The publish transition (row already visible, then
        ``_release_worker_slot(published=True)``) moves the unit from
        ``reservations`` to the DB-derived live count with the SAME admitted total
        on both sides — no double- or under-count instant."""
        session = "cao-r5-transition"
        supervisor = "sup-r5-trans"
        _reset_cap_session(session)
        a_id = "aworker9"
        db_rows = [_row(EXISTING_WORKER, caller_id=supervisor), _row(a_id, caller_id=supervisor)]
        monkeypatch.setattr(ts, "_resolve_worker_terminal_cap", lambda: 3)
        monkeypatch.setattr(ts, "list_terminals_by_session", lambda _s: list(db_rows))
        monkeypatch.setattr(ts.status_monitor, "get_status", lambda tid: TerminalStatus.PROCESSING)

        token_a = _seed_reservation(session, a_id)

        # BEFORE the transition: reservation held, A's row excluded → admitted for
        # a hypothetical peer = reservations(1) + live(1) = 2.
        lock = _cap_admission_lock(session)

        def _peek_admitted():
            with lock:
                reserved = len(_cap_reservations.get(session) or ())
                _c, _r, ids = _count_worker_terminals_detailed(session, supervisor)
                return reserved + _live_worker_count(session, ids)

        assert _peek_admitted() == 2
        # Transition: A's row is already visible; retire the reservation + drop id.
        _release_worker_slot(session, a_id, token=token_a, published=True)
        # AFTER: reservation gone, A's row now counted via live → admitted STILL 2.
        assert _peek_admitted() == 2
        _reset_cap_session(session)

    def test_reserve_then_publish_failure_releases_unit(self, tmp_path):
        """req 2: a reserve whose publication throws AFTER the row is inserted and
        AFTER _mark_publishing must not strand a double unit — the next admission
        sees exactly the rows that really exist."""
        session = "cao-r5-pubfail"
        _reset_cap_session(session)
        rows = [_row(EXISTING_WORKER)]
        ids = iter([f"new{i:05d}" for i in range(1, 6)])
        wd = str(tmp_path)

        with _real_seam(rows, cap=3, id_counter=ids) as seam:
            patched_db_create = ts.db_create_terminal
            boom = {"armed": True}

            def failing_db_create(terminal_id, *a, **kw):
                patched_db_create(terminal_id, *a, **kw)  # row inserted (visible)
                if boom["armed"]:
                    boom["armed"] = False
                    raise RuntimeError("db_publish_failed")
                return {"id": terminal_id}

            with patch.object(ts, "db_create_terminal", failing_db_create):
                with pytest.raises(Exception):
                    asyncio.run(_create_worker(session=session, tmpdir=wd))

            # The failed create left NO stranded reservation/publishing unit.
            assert not _cap_reservations.get(session)
            assert not _cap_publishing_ids.get(session)
            # A row may or may not remain depending on rollback; the point is the
            # ledger no longer double-books. Whatever rows exist, a fresh admission
            # counts each exactly once. Drain any orphan row so the count is known.
            rows[:] = [_row(EXISTING_WORKER)]
            made = asyncio.run(_create_worker(session=session, tmpdir=wd))
            assert made.id  # admitted; no phantom unit shrank the cap
        _reset_cap_session(session)


class TestR5DriftSelfHealing:
    """req 1: the in-memory ledger converges to DB truth on every decision."""

    @pytest.mark.asyncio
    async def test_poisoned_ledger_both_ways_still_correct(self, monkeypatch):
        session = "cao-r5-drift"
        _reset_cap_session(session)
        # DB truth: two live worker rows, neither reserved.
        db_rows = [_row("live0001"), _row("live0002")]
        monkeypatch.setattr(ts, "_resolve_worker_terminal_cap", lambda: 3)
        monkeypatch.setattr(ts, "list_terminals_by_session", lambda _s: list(db_rows))
        monkeypatch.setattr(ts.status_monitor, "get_status", lambda tid: TerminalStatus.PROCESSING)

        # POISON A: a stale publishing entry with NO backing DB row AND no live
        # token (an orphan). It must NOT permanently shrink the cap — it is not in
        # the listing, and r6 also PURGES it because its token is not live.
        lock = _cap_admission_lock(session)
        with lock:
            _cap_publishing_ids[session] = {"ghost999": 999}  # token 999 is not live
            _cap_reservations.pop(session, None)
            ts._cap_gen[session] = ts._cap_gen.get(session, 0) + 1

        # DB truth = 2 live rows, cap 3 → one more admission must be allowed.
        token = await _reserve_worker_slot(session, SUPERVISOR)
        assert token is not None  # 2 live + 0 reservations < 3
        # The orphan exclusion was purged during the count (its token was dead).
        assert "ghost999" not in (_cap_publishing_ids.get(session) or {})
        # Retire it as a failure so no row is added.
        _release_worker_slot(session, "newcomer", token=token, published=False)

        # POISON B: a DB row that never went through reserve/publish (restart /
        # external create). It must count EXACTLY ONCE. Add a third live row so DB
        # truth is now at cap 3 → the next admission must be REFUSED.
        db_rows.append(_row("live0003"))
        with pytest.raises(TerminalCapExceeded) as refused:
            await _reserve_worker_slot(session, SUPERVISOR)
        assert refused.value.current_count == 3 and refused.value.cap == 3
        _reset_cap_session(session)

    @pytest.mark.asyncio
    async def test_stale_publishing_id_matching_live_db_row_does_not_over_admit(self, monkeypatch):
        """F439 r6 / BLOCKER 2 (the r5 gate's second blocker), recycled-id
        direction. A stale publishing entry left behind (crash after mark, leak)
        whose terminal id ALSO matches a LIVE/recycled DB row must NOT exclude that
        row with no backing reservation and admit a second unit at cap 1.

        r5 stored publishing ids as a bare SET and excluded any listed row whose
        id was in it, regardless of whether a reservation backed it. With
        ``_cap_publishing_ids = {'recycled-id'}``, ``reservations = 0`` and a DB
        row ``recycled-id`` at cap 1, r5 computed ``live = 0`` and ADMITTED —
        two units at a one-worker cap. r6 binds the exclusion to a live token: the
        stale entry's token is dead, so it excludes nothing, the row counts, and
        the next admission is REFUSED. The stale entry is also purged.
        """
        session = "cao-r6-recycled"
        supervisor = "sup-r6-recycled"
        _reset_cap_session(session)
        recycled = "recycled-id"
        db_rows = [_row(recycled, caller_id=supervisor)]  # a LIVE row with the id
        monkeypatch.setattr(ts, "_resolve_worker_terminal_cap", lambda: 1)
        monkeypatch.setattr(ts, "list_terminals_by_session", lambda _s: list(db_rows))
        monkeypatch.setattr(ts.status_monitor, "get_status", lambda tid: TerminalStatus.PROCESSING)

        # Poison: a leaked publishing entry for the recycled id, NO live token
        # backing it (reservations empty) — exactly the r5 gate's shape.
        lock = _cap_admission_lock(session)
        with lock:
            _cap_publishing_ids[session] = {recycled: 42}  # token 42 is not live
            _cap_reservations.pop(session, None)
            ts._cap_gen[session] = ts._cap_gen.get(session, 0) + 1

        # r5 admitted here (live counted 0). r6 MUST refuse: reservations(0) +
        # live(1, the recycled row's stale exclusion is a dead orphan) = 1 >= 1.
        with pytest.raises(TerminalCapExceeded) as refused:
            await _reserve_worker_slot(session, supervisor)
        assert refused.value.current_count == 1 and refused.value.cap == 1
        # The orphaned exclusion was purged (converged to DB truth).
        assert not _cap_publishing_ids.get(session)
        _reset_cap_session(session)

    def test_live_worker_count_ignores_and_purges_orphan_exclusion(self):
        """Unit-level guard for the r6 B2 invariant: an exclusion whose token is
        NOT a live reservation neither excludes a matching row NOR survives the
        count. A held-token exclusion, by contrast, does exclude its row."""
        session = "cao-r6-orphan-unit"
        _reset_cap_session(session)
        lock = _cap_admission_lock(session)
        # One live reservation (token T) that excludes 'held'; one orphan token
        # that (wrongly, if honoured) would exclude the live 'recycled' row.
        with lock:
            _cap_reservations[session] = {7}
            _cap_publishing_ids[session] = {"held": 7, "recycled": 999}
            # held row excluded (token 7 live); recycled NOT excluded (999 dead).
            live = _live_worker_count(session, ["held", "recycled", "other"])
            # 'held' excluded → count {'recycled','other'} = 2.
            assert live == 2
            # The orphan was purged; the live-token exclusion remains.
            assert _cap_publishing_ids[session] == {"held": 7}
        _reset_cap_session(session)


class TestR6ReclamationSafety:
    """F439 r6 / BLOCKER 1: the reclamation ABA is killed by construction — the
    per-session lock is never swapped and the generation never resets."""

    @pytest.mark.asyncio
    async def test_stale_reader_aba_schedule_refuses_at_cap_one(self, monkeypatch):
        """The r5 gate's four-step ABA schedule, deterministic:

        1. A stale reader (lane L1) reads the generation and takes an EMPTY
           lock-free listing snapshot, then PAUSES before deciding.
        2. A keeper reservation is retired; r5 reclamation removed L1's lock+gen.
        3. A new lane (L2) obtains a lock, reserves, publishes row ``published``,
           retires + reclaims — under r5 the generation was back to 0.
        4. L1 resumes; under r5 ``gen_before == gen_after == 0`` falsely validated
           its stale empty snapshot and it over-admitted at cap 1.

        r6: the lock is never swapped (L1 and L2 share ONE lock object) and the
        generation is monotonic (never popped), so when L1 resumes it observes a
        STRICTLY GREATER generation, re-lists, sees ``published``, and is REFUSED
        at cap 1. Asserted here without real threads by driving the exact ledger
        transitions the schedule produces and then reserving from the post-churn
        state."""
        session = "cao-r6-aba"
        supervisor = "sup-r6-aba"
        _reset_cap_session(session)
        monkeypatch.setattr(ts, "_resolve_worker_terminal_cap", lambda: 1)

        # Step 1: capture the lock identity + generation a stale reader would hold
        # at the start, against an EMPTY fleet.
        lock_before = _cap_admission_lock(session)
        with lock_before:
            gen_l1_before = ts._cap_gen.get(session, 0)

        # Step 2+3: a keeper reserves against the empty fleet, publishes its row,
        # then retires+reclaims (the full churn r5 would have ABA-reset).
        db_rows: list = []
        monkeypatch.setattr(ts, "list_terminals_by_session", lambda _s: list(db_rows))
        monkeypatch.setattr(ts.status_monitor, "get_status", lambda tid: TerminalStatus.PROCESSING)
        keeper = await _reserve_worker_slot(session, supervisor)
        assert keeper is not None
        published_id = "published"
        _mark_publishing(session, published_id, keeper)
        db_rows.append(_row(published_id, caller_id=supervisor))  # row now visible
        _release_worker_slot(session, published_id, token=keeper, published=True)
        # The session is quiescent → mutable state reclaimed, BUT lock+gen kept.
        assert not _cap_reservations.get(session)
        assert not _cap_publishing_ids.get(session)

        # r6 invariant A: the lock object is the SAME (never swapped/recreated).
        lock_after = _cap_admission_lock(session)
        assert lock_after is lock_before, "reclamation must NOT swap the per-session lock (r5 ABA)"
        # r6 invariant B: the generation strictly advanced (never reset to 0).
        with lock_after:
            gen_now = ts._cap_gen.get(session, 0)
        assert gen_now > gen_l1_before, "generation must be monotonic across churn (r5 ABA)"

        # Step 4: the stale reader L1 resumes and decides. Its snapshot generation
        # (gen_l1_before) no longer matches → r6 re-lists, sees the published row,
        # and REFUSES at cap 1. (Under r5's 0==0 reset it would have admitted.)
        with pytest.raises(TerminalCapExceeded) as refused:
            await _reserve_worker_slot(session, supervisor)
        assert refused.value.current_count == 1 and refused.value.cap == 1
        _reset_cap_session(session)

    def test_admission_lock_identity_is_stable_across_full_lifecycle(self):
        """The per-session lock object is stable across a full reserve→release
        (quiescence) cycle — never a fresh object a stale reader could diverge
        from. This is the direct kill of the r5 lock-domain-swap."""
        session = "cao-r6-lock-stable"
        _reset_cap_session(session)
        lock_a = _cap_admission_lock(session)
        token = _seed_reservation(session)
        lock_b = _cap_admission_lock(session)
        _release_worker_slot(session, None, token=token, published=True)  # -> quiescent
        lock_c = _cap_admission_lock(session)
        assert lock_a is lock_b is lock_c
        _reset_cap_session(session)


class TestR5BoundedProgress:
    """SHOULD 1 (bounded progress under churn). SHOULD 2 (lock reclamation) is
    intentionally DROPPED in r6: the lock+gen are never reclaimed (that was the
    r5 ABA); see ``TestR6RetainedRegistryIsBoundedButTiny`` for the memory note."""

    @pytest.mark.asyncio
    async def test_reserve_completes_under_sustained_generation_churn(self, monkeypatch):
        """SHOULD 1: a concurrent writer bumps the generation continuously; the
        seqlock reader must still complete (bounded lock-free retries → a
        lock-held fallback that a racer cannot invalidate), unlike the r4
        unbounded loop that never reached a decision. The writer runs on its own
        thread and takes the admission lock properly (as a real publisher does),
        so while the fallback holds the lock the writer is simply blocked — which
        is exactly what guarantees progress."""
        session = "cao-r5-churn"
        _reset_cap_session(session)
        monkeypatch.setattr(ts, "_resolve_worker_terminal_cap", lambda: 1000)
        monkeypatch.setattr(ts, "_count_worker_terminals_detailed", lambda _s, _sup: (0, [], []))

        lock = _cap_admission_lock(session)
        stop = _threading.Event()

        def churn():
            # Sustained ledger churn: bump the generation under the lock (exactly
            # how _mark_publishing / _release_worker_slot mutate it) until stopped.
            while not stop.is_set():
                with lock:
                    ts._cap_gen[session] = ts._cap_gen.get(session, 0) + 1

        writer = _threading.Thread(target=churn, daemon=True)
        writer.start()
        try:
            # Must return (not hang): the bounded fallback eventually takes the
            # listing+decision under the lock, where the writer cannot race it.
            token = await asyncio.wait_for(_reserve_worker_slot(session, "sup"), timeout=10)
            assert token is not None
            assert len(_cap_reservations.get(session) or ()) == 1
        finally:
            stop.set()
            writer.join(timeout=5)
        _release_worker_slot(session, None, token=token, published=False)
        _reset_cap_session(session)


class TestR6RetainedRegistryIsBoundedButTiny:
    """F439 r6 / BLOCKER 1: after a full reserve→release cycle the MUTABLE ledger
    state is reclaimed (no phantom reservation/exclusion lingers), while the
    lock+gen+token-seq are intentionally RETAINED (never popped) — the deliberate,
    documented unbounded-but-tiny growth that replaces the unsafe r5 reclamation.
    """

    @pytest.mark.asyncio
    async def test_quiescent_sessions_reclaim_mutable_state_but_retain_lock(self, monkeypatch):
        monkeypatch.setattr(ts, "_resolve_worker_terminal_cap", lambda: 10)
        monkeypatch.setattr(ts, "_count_worker_terminals_detailed", lambda _s, _sup: (0, [], []))
        sessions = [f"cao-r6-reclaim-{i:04d}" for i in range(200)]
        for session in sessions:
            token = await _reserve_worker_slot(session, "sup")
            assert token is not None
            _release_worker_slot(session, f"tid-{session}", token=token, published=True)
            # MUTABLE state fully reclaimed on quiescence.
            assert session not in _cap_reservations
            assert session not in _cap_publishing_ids
            # But the lock + generation are RETAINED (r6: never reclaim → no ABA).
            assert session in _cap_admission_locks
            assert session in ts._cap_gen
        # The retained registry is bounded by DISTINCT session names (tiny), and
        # every retained gen is monotonic (>= 1 after one cycle).
        for session in sessions:
            assert ts._cap_gen[session] >= 1



# ─────────────────────────────────────────────────────────────────────────────
# F439 r8: Double-cancel cap-token ownership transfer regression



# ─────────────────────────────────────────────────────────────────────────────
# F439 r8: Double-cancel cap-token ownership transfer regression
# ─────────────────────────────────────────────────────────────────────────────


class TestR8DoubleCancelCapOwnership:
    """F439 r8 BLOCKER: a repeat cancellation must NOT release the cap token
    while the shielded create worker's backend may still exist.

    The invariant: a reservation stays live until its worker's backend resource
    is destroyed or published. The double-cancel path must transfer release
    ownership to the replacement compensator, and no peer may reserve until
    that compensator completes the rollback.
    """

    @pytest.mark.asyncio
    async def test_double_cancel_holds_token_until_backend_destroyed(self, tmp_path):
        """Deterministic double-cancel schedule: assert peer's reserve BLOCKS
        until worker 1's backend is destroyed; no ordering yields two live
        backends under cap 1."""
        session = "cao-r8-dblcancel"
        _reset_cap_session(session)
        rows: list[dict] = [_row(SUPERVISOR, profile="supervisor")]
        ids = iter([f"r8w{i:05d}" for i in range(1, 10)])
        wd = str(tmp_path)

        worker_blocked = _threading.Event()
        worker_proceed = _threading.Event()
        backend_created: list[str] = []
        backend_killed: list[str] = []

        with _real_seam(rows, cap=1, id_counter=ids) as seam:
            def blocking_create_window(session_name, window_name, *a, **k):
                backend_created.append(window_name)
                worker_blocked.set()
                worker_proceed.wait(timeout=30)
                return window_name

            seam["backend"].create_window.side_effect = blocking_create_window

            def track_kill(session_name, window_name, **k):
                backend_killed.append(window_name)
                return True

            seam["backend"].kill_window = MagicMock(side_effect=track_kill)
            seam["backend"].kill_session = MagicMock(side_effect=track_kill)

            with patch.object(ts, "db_delete_terminal", MagicMock(side_effect=lambda tid: rows.__setitem__(slice(None), [r for r in rows if r.get("id") != tid]))):
                task1 = asyncio.ensure_future(_create_worker(session=session, tmpdir=wd))
                await asyncio.to_thread(worker_blocked.wait, 10)
                assert len(backend_created) == 1

                # First cancel: CancelledError at shield(create_worker). The task
                # enters the compensator's shield await.
                task1.cancel()
                await asyncio.sleep(0)  # let task process first cancel

                # Second cancel: CancelledError at shield(compensator). This
                # triggers the ownership transfer in our r8 fix.
                task1.cancel()

                with pytest.raises(asyncio.CancelledError):
                    await task1

                assert not worker_proceed.is_set()

                # Token still held — peer refused at cap 1.
                with pytest.raises(TerminalCapExceeded) as refused:
                    await _reserve_worker_slot(session, SUPERVISOR)
                assert refused.value.current_count >= 1

                # Unblock worker → compensator completes rollback
                worker_proceed.set()
                for _ in range(50):
                    await asyncio.sleep(0.01)

                assert len(backend_killed) >= 1, "compensator should have destroyed backend"

                # Token released — peer can now reserve.
                token2 = await _reserve_worker_slot(session, SUPERVISOR)
                assert token2 is not None
                _release_worker_slot(session, None, token=token2, published=False)

        _reset_cap_session(session)

    @pytest.mark.asyncio
    async def test_double_cancel_no_two_live_backends_under_cap_1(self, tmp_path):
        """No ordering yields two live backends under cap 1. The peer's backend
        creation must not start until worker 1's is destroyed."""
        session = "cao-r8-nooverlap"
        _reset_cap_session(session)
        rows: list[dict] = [_row(SUPERVISOR, profile="supervisor")]
        ids = iter([f"r8o{i:05d}" for i in range(1, 10)])
        wd = str(tmp_path)

        worker_blocked = _threading.Event()
        worker_proceed = _threading.Event()
        backend_events: list[tuple[str, str]] = []
        max_concurrent_backends = [0]
        current_live = [0]

        with _real_seam(rows, cap=1, id_counter=ids) as seam:
            create_lock = _threading.Lock()

            def tracked_create_window(session_name, window_name, *a, **k):
                with create_lock:
                    current_live[0] += 1
                    if current_live[0] > max_concurrent_backends[0]:
                        max_concurrent_backends[0] = current_live[0]
                    backend_events.append(("create", window_name))
                if not worker_blocked.is_set():
                    worker_blocked.set()
                    worker_proceed.wait(timeout=30)
                return window_name

            def tracked_kill(session_name, window_name, **k):
                with create_lock:
                    current_live[0] -= 1
                    backend_events.append(("kill", window_name))
                return True

            seam["backend"].create_window.side_effect = tracked_create_window
            seam["backend"].kill_window = MagicMock(side_effect=tracked_kill)
            seam["backend"].kill_session = MagicMock(side_effect=tracked_kill)

            with patch.object(ts, "db_delete_terminal", MagicMock(side_effect=lambda tid: rows.__setitem__(slice(None), [r for r in rows if r.get("id") != tid]))):
                task1 = asyncio.ensure_future(_create_worker(session=session, tmpdir=wd))
                await asyncio.to_thread(worker_blocked.wait, 10)

                # Double cancel
                task1.cancel()
                await asyncio.sleep(0)
                task1.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task1

                # Peer refused while token held
                with pytest.raises(TerminalCapExceeded):
                    await _reserve_worker_slot(session, SUPERVISOR)

                # Unblock worker 1
                worker_proceed.set()
                for _ in range(50):
                    await asyncio.sleep(0.01)

                # Worker 2 now succeeds
                task2 = asyncio.ensure_future(_create_worker(session=session, tmpdir=wd))
                await task2

            assert max_concurrent_backends[0] <= 1, (
                f"Over-admission: {max_concurrent_backends[0]} concurrent backends "
                f"under cap 1. Events: {backend_events}"
            )
            create_indices = [i for i, (a, _) in enumerate(backend_events) if a == "create"]
            kill_indices = [i for i, (a, _) in enumerate(backend_events) if a == "kill"]
            assert len(create_indices) >= 2
            assert len(kill_indices) >= 1
            assert kill_indices[0] < create_indices[1], (
                f"Backend 2 created before backend 1 killed: {backend_events}"
            )

        _reset_cap_session(session)

    @pytest.mark.asyncio
    async def test_single_cancel_releases_token_once(self, tmp_path):
        """Single cancel (no double-cancel): compensator runs to completion,
        outer finally releases the token. Exactly one release."""
        session = "cao-r8-singlecancel"
        _reset_cap_session(session)
        rows: list[dict] = [_row(SUPERVISOR, profile="supervisor")]
        ids = iter([f"r8s{i:05d}" for i in range(1, 10)])
        wd = str(tmp_path)

        worker_blocked = _threading.Event()
        worker_proceed = _threading.Event()
        release_calls: list[tuple] = []
        orig_release = ts._release_worker_slot

        def tracked_release(session_name, terminal_id, *, token, published):
            release_calls.append((session_name, terminal_id, token, published))
            return orig_release(session_name, terminal_id, token=token, published=published)

        with _real_seam(rows, cap=1, id_counter=ids) as seam:
            def blocking_create(session_name, window_name, *a, **k):
                worker_blocked.set()
                worker_proceed.wait(timeout=30)
                return window_name

            seam["backend"].create_window.side_effect = blocking_create
            seam["backend"].kill_window = MagicMock(return_value=True)
            seam["backend"].kill_session = MagicMock(return_value=True)

            with patch.object(ts, "db_delete_terminal", MagicMock()):
                with patch.object(ts, "_release_worker_slot", tracked_release):
                    task1 = asyncio.ensure_future(_create_worker(session=session, tmpdir=wd))
                    await asyncio.to_thread(worker_blocked.wait, 10)

                    # Unblock immediately so compensator completes fast
                    worker_proceed.set()

                    # Single cancel only
                    task1.cancel()
                    # The task catches CancelledError, awaits shield(compensator)
                    # which completes (worker done), then the outer finally runs.
                    with pytest.raises(asyncio.CancelledError):
                        await task1

                    # Let any background tasks complete
                    for _ in range(30):
                        await asyncio.sleep(0.01)

            session_releases = [c for c in release_calls if c[0] == session]
            assert len(session_releases) == 1, (
                f"Expected exactly 1 release, got {len(session_releases)}: {session_releases}"
            )

        _reset_cap_session(session)

    @pytest.mark.asyncio
    async def test_double_cancel_releases_token_once(self, tmp_path):
        """Double cancel: token released exactly once by the replacement
        compensator, never by the outer finally."""
        session = "cao-r8-dblrelease"
        _reset_cap_session(session)
        rows: list[dict] = [_row(SUPERVISOR, profile="supervisor")]
        ids = iter([f"r8d{i:05d}" for i in range(1, 10)])
        wd = str(tmp_path)

        worker_blocked = _threading.Event()
        worker_proceed = _threading.Event()
        release_calls: list[tuple] = []
        orig_release = ts._release_worker_slot

        def tracked_release(session_name, terminal_id, *, token, published):
            release_calls.append((session_name, terminal_id, token, published))
            return orig_release(session_name, terminal_id, token=token, published=published)

        with _real_seam(rows, cap=1, id_counter=ids) as seam:
            def blocking_create(session_name, window_name, *a, **k):
                worker_blocked.set()
                worker_proceed.wait(timeout=30)
                return window_name

            seam["backend"].create_window.side_effect = blocking_create
            seam["backend"].kill_window = MagicMock(return_value=True)
            seam["backend"].kill_session = MagicMock(return_value=True)

            with patch.object(ts, "db_delete_terminal", MagicMock()):
                with patch.object(ts, "_release_worker_slot", tracked_release):
                    task1 = asyncio.ensure_future(_create_worker(session=session, tmpdir=wd))
                    await asyncio.to_thread(worker_blocked.wait, 10)

                    # Double cancel
                    task1.cancel()
                    await asyncio.sleep(0)
                    task1.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task1

                    # No release yet (worker still blocked)
                    session_releases_before = [c for c in release_calls if c[0] == session]
                    assert len(session_releases_before) == 0, (
                        f"Token released before backend destroyed: {session_releases_before}"
                    )

                    # Unblock → compensator releases
                    worker_proceed.set()
                    for _ in range(50):
                        await asyncio.sleep(0.01)

                session_releases = [c for c in release_calls if c[0] == session]
                assert len(session_releases) == 1, (
                    f"Expected exactly 1 release, got {len(session_releases)}: {session_releases}"
                )

        _reset_cap_session(session)

    @pytest.mark.asyncio
    async def test_compensator_crash_still_releases_token(self, tmp_path):
        """If the compensator's rollback crashes, the token is still released
        (via the finally in _finish_and_roll_back_cancelled_create)."""
        session = "cao-r8-compcrash"
        _reset_cap_session(session)
        rows: list[dict] = [_row(SUPERVISOR, profile="supervisor")]
        ids = iter([f"r8c{i:05d}" for i in range(1, 10)])
        wd = str(tmp_path)

        worker_blocked = _threading.Event()
        worker_proceed = _threading.Event()

        with _real_seam(rows, cap=1, id_counter=ids) as seam:
            def blocking_create(session_name, window_name, *a, **k):
                worker_blocked.set()
                worker_proceed.wait(timeout=30)
                return window_name

            seam["backend"].create_window.side_effect = blocking_create
            seam["backend"].kill_window = MagicMock(side_effect=RuntimeError("kill crashed"))
            seam["backend"].kill_session = MagicMock(side_effect=RuntimeError("kill crashed"))

            with patch.object(ts, "db_delete_terminal", MagicMock(side_effect=RuntimeError("db crashed"))):
                task1 = asyncio.ensure_future(_create_worker(session=session, tmpdir=wd))
                await asyncio.to_thread(worker_blocked.wait, 10)

                # Double cancel
                task1.cancel()
                await asyncio.sleep(0)
                task1.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task1

                # Token still held
                with pytest.raises(TerminalCapExceeded):
                    await _reserve_worker_slot(session, SUPERVISOR)

                # Unblock → compensator crashes but finally releases
                worker_proceed.set()
                for _ in range(50):
                    await asyncio.sleep(0.01)

            # Token released despite crash (verified via ledger, not via reserve
            # — the DB row may still be live, so reserve could still refuse based
            # on the live row count; what matters is the RESERVATION is gone).
            assert not _cap_reservations.get(session), (
                "reservation should be released even after compensator crash"
            )

        _reset_cap_session(session)
