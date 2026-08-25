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

    def _counting(session_name, supervisor_id):
        if count_hook is not None:
            count_hook()
        return orig_count(session_name, supervisor_id)

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
