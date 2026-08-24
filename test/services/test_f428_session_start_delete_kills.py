"""F428 (#283) mutation kill tests — session_service start_session / delete_session.

The f408 session shard was LOST (cursor-3 down). These tests pin the TOP-10
mutation-prone surfaces of start/delete: equality guards, bootstrap FSM
transitions, and cleanup ordering. Each test is mapped to a mutant in
``orchestrator/tmp/orch/f428-tests-report.md`` and is required to go red under
that mutant (apply → red → restore → green, hash-verified).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.services import rebind_lease as rebind_lease_mod
from cli_agent_orchestrator.services import session_lifecycle_lease as lifecycle_lease_mod
from cli_agent_orchestrator.services import session_service
from cli_agent_orchestrator.services.session_service import delete_session, start_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ProviderClass:
    def __init__(self, seed_flag):
        self.supports_seed_resume_identity = seed_flag


def _terminal(
    *,
    session_name: str = "cao-f428",
    provider: str = "codex",
    provider_session_id: str = "seed-uuid-1",
) -> Terminal:
    return Terminal(
        id="abcd1234",
        name="supervisor",
        provider=provider,
        session_name=session_name,
        agent_profile="developer",
        provider_session_id=provider_session_id,
    )


async def _run_start(
    *,
    kwargs: dict,
    seed_flag=False,
    resolve_to: str = "kiro_cli",
    terminal: Terminal | None = None,
    manifest="manifest-ok",
    manifest_exc: Exception | None = None,
    admit=None,
):
    """Drive start_session with the create/manifest/provider seams stubbed."""
    term = terminal or _terminal()
    create = AsyncMock(return_value=term)
    resolve = MagicMock(return_value=resolve_to)
    get_cls = MagicMock(return_value=_ProviderClass(seed_flag))
    if manifest_exc is not None:
        build = MagicMock(side_effect=manifest_exc)
    else:
        build = MagicMock(return_value=manifest)
    admit_fn = admit if admit is not None else MagicMock()

    with (
        patch.object(session_service, "create_session", create),
        patch.object(session_service, "resolve_provider", resolve),
        patch.object(session_service, "require_provider_admitted", admit_fn),
        patch(
            "cli_agent_orchestrator.providers.manager.get_provider_class",
            get_cls,
        ),
        patch(
            "cli_agent_orchestrator.services.session_manifest_service.build_session_manifest",
            build,
        ),
    ):
        result = await start_session(**kwargs)
    return result, {
        "create": create,
        "resolve": resolve,
        "admit": admit_fn,
        "get_cls": get_cls,
        "build": build,
        "terminal": term,
    }


def _patch_delete_seams(
    *,
    terminals: list[dict],
    delete_side_effect=None,
    delete_return=None,
    session_exists: bool = True,
):
    """Patch delete_session's DB/guard/teardown seams; leases stay real."""
    backend = MagicMock()
    backend.session_exists.return_value = session_exists
    delete_mock = MagicMock(return_value=delete_return)
    if delete_side_effect is not None:
        delete_mock.side_effect = delete_side_effect
    finalized: list[tuple] = []

    def _finalize(name, registry=None, backend=None):
        finalized.append((name, registry))

    patches = [
        patch.object(session_service, "list_terminals_by_session", return_value=terminals),
        patch.object(session_service, "get_backend", return_value=backend),
        patch.object(session_service, "finalize_session", side_effect=_finalize),
        patch(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.preflight_session_teardown",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.quiesce_deferred_session_sync",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease",
            delete_mock,
        ),
    ]
    return patches, backend, delete_mock, finalized


# ===========================================================================
# start_session — equality + bootstrap FSM
# ===========================================================================


@pytest.mark.asyncio
async def test_start_explicit_provider_skips_resolve_and_admits_that_provider():
    """M1: `provider or resolve_provider` → `and` would call resolve and admit its result."""
    result, mocks = await _run_start(
        kwargs={"provider": "codex", "agent_profile": "developer"},
        seed_flag=True,
        resolve_to="kiro_cli",
        terminal=_terminal(provider="codex"),
    )
    mocks["resolve"].assert_not_called()
    mocks["admit"].assert_called_once_with("codex")
    mocks["get_cls"].assert_called_once_with("codex")
    mocks["create"].assert_awaited_once()
    assert result["bootstrap"]["mode"] == "seed_resume"


@pytest.mark.asyncio
async def test_start_omitted_provider_resolves_with_kiro_fallback():
    """M1: `or` → `and` short-circuits on provider=None and never resolves."""
    result, mocks = await _run_start(
        kwargs={"provider": None, "agent_profile": "my_agent"},
        seed_flag=False,
        resolve_to="claude_code",
        terminal=_terminal(provider="claude_code", provider_session_id=None),
    )
    mocks["resolve"].assert_called_once_with("my_agent", fallback_provider="kiro_cli")
    mocks["admit"].assert_called_once_with("claude_code")
    mocks["get_cls"].assert_called_once_with("claude_code")
    assert result["bootstrap"]["mode"] == "not_applicable"
    assert result["schema_version"] == "cao.session-start/v1"


@pytest.mark.asyncio
async def test_start_seed_true_bootstrap_is_seeded_with_uuid():
    """M2/M3: `is True` → `is not True` (or inverted splat) drops seed_resume + uuid."""
    result, _ = await _run_start(
        kwargs={"provider": "codex", "agent_profile": "developer"},
        seed_flag=True,
        terminal=_terminal(provider_session_id="uuid-seeded"),
    )
    assert result["bootstrap"] == {
        "mode": "seed_resume",
        "status": "seeded",
        "session_uuid": "uuid-seeded",
    }
    assert result["session"] == {"name": "cao-f428"}
    assert result["manifest_error"] is None


@pytest.mark.asyncio
async def test_start_seed_false_bootstrap_omits_session_uuid():
    """M2/M3: inverted `if seed_mode` splat would leak session_uuid on non-seed providers."""
    result, _ = await _run_start(
        kwargs={"provider": "kiro_cli", "agent_profile": "developer"},
        seed_flag=False,
        terminal=_terminal(provider="kiro_cli", provider_session_id="should-not-leak"),
    )
    assert result["bootstrap"] == {"mode": "not_applicable", "status": "not_required"}
    assert "session_uuid" not in result["bootstrap"]


@pytest.mark.asyncio
async def test_start_truthy_non_true_seed_flag_is_not_seed_mode():
    """M2: `is True` → truthy/`== True`. `1 is True` is False; `1 == True` is True."""
    result, _ = await _run_start(
        kwargs={"provider": "codex", "agent_profile": "developer"},
        seed_flag=1,
        terminal=_terminal(provider_session_id="must-not-appear"),
    )
    assert result["bootstrap"]["mode"] == "not_applicable"
    assert result["bootstrap"]["status"] == "not_required"
    assert "session_uuid" not in result["bootstrap"]


@pytest.mark.asyncio
async def test_start_manifest_failure_sets_build_failed_and_does_not_raise():
    """M4: dropping the except (or swapping the sentinel) leaks/mis-labels the failure."""
    result, mocks = await _run_start(
        kwargs={"provider": "codex", "agent_profile": "developer"},
        seed_flag=False,
        manifest_exc=RuntimeError("manifest boom"),
    )
    assert result["manifest"] is None
    assert result["manifest_error"] == "build_failed"
    mocks["build"].assert_called_once_with("cao-f428")


@pytest.mark.asyncio
async def test_start_manifest_success_keeps_manifest_error_none():
    """M4: success path must not write build_failed (mutant that always sets it)."""
    result, _ = await _run_start(
        kwargs={"provider": "codex", "agent_profile": "developer"},
        seed_flag=True,
        manifest={"ok": True},
    )
    assert result["manifest"] == {"ok": True}
    assert result["manifest_error"] is None


@pytest.mark.asyncio
async def test_start_admission_gate_uses_resolved_and_blocks_create():
    """M5: deleting require_provider_admitted lets create_session run."""
    admit = MagicMock(side_effect=RuntimeError("not_admitted"))
    with pytest.raises(RuntimeError, match="not_admitted"):
        await _run_start(
            kwargs={"provider": None, "agent_profile": "developer"},
            resolve_to="codex",
            admit=admit,
        )
    # _run_start only returns on success; re-drive with inspectable create mock.
    create = AsyncMock()
    resolve = MagicMock(return_value="codex")
    with (
        patch.object(session_service, "create_session", create),
        patch.object(session_service, "resolve_provider", resolve),
        patch.object(
            session_service, "require_provider_admitted", admit
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.get_provider_class",
            MagicMock(return_value=_ProviderClass(False)),
        ),
    ):
        with pytest.raises(RuntimeError, match="not_admitted"):
            await start_session(provider=None, agent_profile="developer")
    admit.assert_called_with("codex")
    create.assert_not_awaited()


# ===========================================================================
# delete_session — lease FSM + cleanup ordering
# ===========================================================================


def test_delete_denied_lifecycle_lease_raises_before_teardown():
    """M6: dropping `if lifecycle_lease is None: raise` proceeds into teardown."""
    session_name = "cao-f428-m6"
    held = lifecycle_lease_mod.acquire_session_lifecycle_exclusive(session_name)
    assert held is not None
    patches, _backend, delete_mock, finalized = _patch_delete_seams(
        terminals=[{"id": "t-victim"}]
    )
    try:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            with pytest.raises(RuntimeError, match="resume_in_progress"):
                delete_session(session_name)
        delete_mock.assert_not_called()
        assert finalized == []
    finally:
        lifecycle_lease_mod.release_session_lifecycle_lease(held)


def test_delete_rebind_conflict_releases_prior_leases_in_reverse():
    """M7: skip reverse-release (or drop reversed()) leaks earlier rebind leases.

    Unsorted input [t-c, t-a, t-b]; hold t-c. Sorted acquire is t-a, t-b, t-c
    (None). Original reverse-releases t-b then t-a and raises; no teardown.
    """
    session_name = "cao-f428-m7"
    terminals = [{"id": "t-c"}, {"id": "t-a"}, {"id": "t-b"}]
    blocker = rebind_lease_mod.acquire_rebind_lease("t-c")
    assert blocker is not None
    released: list[str] = []
    real_release = rebind_lease_mod.release_rebind_lease

    def _release(token):
        real_release(token)
        released.append(token.terminal_id)

    patches, _backend, delete_mock, finalized = _patch_delete_seams(terminals=terminals)
    try:
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch.object(rebind_lease_mod, "release_rebind_lease", side_effect=_release),
        ):
            with pytest.raises(RuntimeError, match="rebind_in_progress"):
                delete_session(session_name)
        assert released == ["t-b", "t-a"]
        assert not rebind_lease_mod.rebind_lease_held("t-a")
        assert not rebind_lease_mod.rebind_lease_held("t-b")
        assert rebind_lease_mod.rebind_lease_held("t-c")
        delete_mock.assert_not_called()
        assert finalized == []
        # exception path must also drop the session exclusive
        retry = lifecycle_lease_mod.acquire_session_lifecycle_exclusive(session_name)
        assert retry is not None
        lifecycle_lease_mod.release_session_lifecycle_lease(retry)
    finally:
        if rebind_lease_mod.rebind_lease_held("t-c"):
            rebind_lease_mod.release_rebind_lease(blocker)
        for tid in ("t-a", "t-b"):
            # belt-and-suspenders if a mutant leaked them
            if rebind_lease_mod.rebind_lease_held(tid):
                # cannot release without the token; best-effort via private map
                with rebind_lease_mod._guard:
                    rebind_lease_mod._owners.pop(tid, None)


def test_delete_acquires_rebind_leases_in_sorted_id_order():
    """M7-order: dropping sorted() acquires in list order, not id order."""
    session_name = "cao-f428-sorted"
    terminals = [{"id": "t-z"}, {"id": "t-a"}]
    order: list[str] = []
    real_acquire = rebind_lease_mod.acquire_rebind_lease

    def _acquire(tid):
        order.append(tid)
        return real_acquire(tid)

    patches, _backend, delete_mock, finalized = _patch_delete_seams(
        terminals=terminals, delete_return={"terminal_deleted": True}
    )
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patch.object(rebind_lease_mod, "acquire_rebind_lease", side_effect=_acquire),
    ):
        result = delete_session(session_name)
    assert order == ["t-a", "t-z"]
    assert result == {"deleted": [session_name], "errors": []}
    assert finalized == [(session_name, None)]
    assert delete_mock.call_count == 2


def test_delete_dict_terminal_deleted_false_is_deferred():
    """M8: dropping `not` on terminal_deleted treats False as success (finalize+deleted)."""
    session_name = "cao-f428-m8-false"
    patches, backend, _delete_mock, finalized = _patch_delete_seams(
        terminals=[{"id": "t-dfr"}],
        delete_return={"terminal_deleted": False},
        session_exists=True,
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        result = delete_session(session_name)
    assert result["deleted"] == []
    assert result["errors"] == [
        {"terminal_id": "t-dfr", "error": "cleanup deferred; retry delete_session"}
    ]
    assert finalized == []
    backend.kill_session.assert_called_once_with(session_name)


def test_delete_dict_terminal_deleted_true_finalizes():
    """M8: inverted `not` would classify a completed dict teardown as deferred."""
    session_name = "cao-f428-m8-true"
    patches, backend, _delete_mock, finalized = _patch_delete_seams(
        terminals=[{"id": "t-ok"}],
        delete_return={"terminal_deleted": True},
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        result = delete_session(session_name)
    assert result == {"deleted": [session_name], "errors": []}
    assert finalized == [(session_name, None)]
    backend.kill_session.assert_not_called()


def test_delete_dict_missing_terminal_deleted_defaults_to_complete():
    """M8-default: `.get(..., True)` → `False` would defer a dict without the key."""
    session_name = "cao-f428-m8-default"
    patches, _backend, _delete_mock, finalized = _patch_delete_seams(
        terminals=[{"id": "t-miss"}],
        delete_return={"other": 1},
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        result = delete_session(session_name)
    assert result == {"deleted": [session_name], "errors": []}
    assert finalized == [(session_name, None)]


def test_delete_success_finalizes_not_bare_kill():
    """M9: invert `if not result["errors"]` (finalize branch) kills via else kill_session."""
    session_name = "cao-f428-m9-ok"
    patches, backend, _delete_mock, finalized = _patch_delete_seams(
        terminals=[{"id": "t-1"}],
        delete_return=None,
        session_exists=True,
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        result = delete_session(session_name)
    assert result["deleted"] == [session_name]
    assert finalized == [(session_name, None)]
    backend.kill_session.assert_not_called()


def test_delete_deferred_skips_finalize_and_kills_session():
    """M9: invert finalize/kill branch would finalize a deferred session."""
    session_name = "cao-f428-m9-dfr"
    patches, backend, _delete_mock, finalized = _patch_delete_seams(
        terminals=[{"id": "t-1"}],
        delete_return=False,
        session_exists=True,
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        result = delete_session(session_name)
    assert result["deleted"] == []
    assert finalized == []
    backend.kill_session.assert_called_once_with(session_name)


def test_delete_resume_in_progress_from_terminal_reraises_and_releases_leases():
    """M10: `str(e) == "resume_in_progress"` → `!=` swallows the FSM raise.

    Exception path must reverse-release rebind + lifecycle so a retry can run.
    """
    session_name = "cao-f428-m10"
    tid = "t-m10"
    patches, _backend, delete_mock, finalized = _patch_delete_seams(
        terminals=[{"id": tid}],
        delete_side_effect=RuntimeError("resume_in_progress"),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        with pytest.raises(RuntimeError, match="resume_in_progress"):
            delete_session(session_name)
    delete_mock.assert_called_once()
    assert finalized == []
    assert not rebind_lease_mod.rebind_lease_held(tid)
    retry = lifecycle_lease_mod.acquire_session_lifecycle_exclusive(session_name)
    assert retry is not None
    lifecycle_lease_mod.release_session_lifecycle_lease(retry)


def test_delete_forwards_force_to_require_delete_allowed():
    """Force flag equality: `force=force` → `force=False` would ignore the caller."""
    session_name = "cao-f428-force"
    patches, _backend, _delete_mock, _finalized = _patch_delete_seams(
        terminals=[{"id": "t-force"}],
        delete_return={"terminal_deleted": True},
    )
    require = MagicMock(return_value=None)
    with (
        patches[0],
        patches[1],
        patches[2],
        patch(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
            require,
        ),
        patches[4],
        patches[5],
        patches[6],
    ):
        delete_session(session_name, force=True)
    require.assert_called_once_with("t-force", force=True)


def test_delete_quiesce_runs_before_lifecycle_lease():
    """Cleanup ordering: quiesce → exclusive lease. Swap would acquire first."""
    session_name = "cao-f428-order"
    order: list[str] = []
    real_acq = lifecycle_lease_mod.acquire_session_lifecycle_exclusive

    def _acq(name):
        order.append("lease")
        return real_acq(name)

    def _quiesce(_name):
        order.append("quiesce")

    patches, _backend, _delete_mock, _finalized = _patch_delete_seams(
        terminals=[],
        delete_return=None,
    )
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patch(
            "cli_agent_orchestrator.services.terminal_service.quiesce_deferred_session_sync",
            side_effect=_quiesce,
        ),
        patches[6],
        patch.object(
            lifecycle_lease_mod, "acquire_session_lifecycle_exclusive", side_effect=_acq
        ),
    ):
        result = delete_session(session_name)
    assert order[:2] == ["quiesce", "lease"]
    assert result["deleted"] == [session_name]
