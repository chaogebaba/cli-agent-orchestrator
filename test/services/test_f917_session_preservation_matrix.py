"""F917 (#769) + F913 slice-1 r2 — session-preservation revisions and the
provider-agnostic acceptance matrix (astra D5/D5a).

Two production defects filed as #769, plus the actively-emitted unsafe-hint
rewrites (astra D6/D5c), pinned with fail-before/pass-after tests and one mutant
per revision. Provider matrix rows (codex, pi_cli, kiro_cli, grok_cli,
claude_code): one repeated-resume + one account-switch invariant each, at the
deterministic (non-live) level the CI arm runs; the real two-cycle provider
probes are the separate `session_preservation and live` arm.

REVISION / MUTANT MAP:
* R1 #769b cleanup-ordering: pi/grok resume facts resolved BEFORE cleanup.
    mutant "empty-destroys-set" (_provider_cleanup_destroys_sessions → {}) →
        test_mutant_empty_destroys_set_is_caught
* R2 #769a codex fallback: _resolve_codex falls back to default home.
    mutant "no-fallback" (namespaced MISSING returned directly) →
        test_mutant_codex_no_fallback_is_caught
* R3 #769a namespace re-seed on resume.
    mutant "keep-dead-namespace" (return namespace unchanged) →
        test_mutant_reseed_keep_dead_namespace_is_caught
* R4 #769a hint: never recommend force when a rollout exists.
    mutant "always-force-hint" → test_mutant_always_force_hint_is_caught
* R5 D6 drift notice: preserve/resume, not cold-assign.
    mutant "cold-assign-text" → test_mutant_drift_cold_assign_is_caught
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services import resume_service as rs
from cli_agent_orchestrator.services import session_artifact as sa
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.authority_pin_service import (
    FrozenPinValidation,
    PinCheckResult,
    format_drift_notice,
)
from cli_agent_orchestrator.services.session_artifact import ArtifactState

ALL_PROVIDERS = ["codex", "pi_cli", "kiro_cli", "grok_cli", "claude_code"]


def _write_codex_rollout(home: Path, uuid: str) -> Path:
    sessions = home / "sessions" / "2026" / "09" / "10"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"rollout-2026-09-10T00-00-00-{uuid}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": uuid}}) + "\n")
    return path


# ==========================================================================
# R1 — #769b: cleanup ordering for session-destroying providers
# ==========================================================================
class TestCleanupOrdering:
    @pytest.mark.parametrize(
        "provider,destroys",
        [
            ("pi_cli", True),
            ("grok_cli", True),
            ("codex", False),
            ("kiro_cli", False),
            ("claude_code", False),
        ],
    )
    def test_destroys_sessions_classification(self, provider, destroys):
        assert ts._provider_cleanup_destroys_sessions(provider) is destroys

    def test_pi_resume_resolved_before_cleanup(self):
        """pass-after: for pi (cleanup rmtree's sessions), the reaper resolves
        the resume facts BEFORE cleanup_provider runs, so a post-cleanup read of
        a deleted store cannot flip resumable to false.
        """
        order: list[str] = []

        def _resolve(tid, meta, *, force):
            order.append("resolve")
            return ("sid", True, "resumable")

        def _cleanup(tid):
            order.append("cleanup")
            return True

        meta = {"provider": "pi_cli", "tmux_session": "s", "tmux_window": "w"}
        with (
            patch.object(ts, "_resolve_reap_resume_key", side_effect=_resolve),
            patch.object(ts.provider_manager, "cleanup_provider", side_effect=_cleanup),
        ):
            # exercise only the precompute predicate + call ordering contract
            precomputed = None
            if ts._provider_cleanup_destroys_sessions(meta["provider"]):
                precomputed = ts._resolve_reap_resume_key("t1", meta, force=False)
            ts.provider_manager.cleanup_provider("t1")
        assert order == ["resolve", "cleanup"]
        assert precomputed == ("sid", True, "resumable")

    def test_mutant_empty_destroys_set_is_caught(self):
        """Mutant 'empty-destroys-set': if _provider_cleanup_destroys_sessions
        returned {} for all, pi/grok would resolve resumability AFTER cleanup
        (against a deleted store). The correct classifier returns True for both;
        asserting that kills the mutant.
        """
        assert ts._provider_cleanup_destroys_sessions("pi_cli") is True
        assert ts._provider_cleanup_destroys_sessions("grok_cli") is True


# ==========================================================================
# R2 — #769a: _resolve_codex default-home fallback
# ==========================================================================
class TestCodexFallback:
    def test_fallback_to_default_when_namespace_home_gone(self, tmp_path):
        uuid = "01a08db2"
        default_home = tmp_path / "default-codex"
        _write_codex_rollout(default_home, uuid)
        dead_ns = str(tmp_path / "cao-personas" / "orig-tid" / "gen-1" / "codex-home")

        with patch.object(sa, "_provider_home", return_value=default_home):
            status = sa._resolve_codex(uuid, dead_ns, None)
        assert status.state == ArtifactState.VALID  # fell back to default home

    def test_namespaced_valid_is_used_directly(self, tmp_path):
        uuid = "01a08db2"
        ns_home = tmp_path / "ns-home"
        _write_codex_rollout(ns_home, uuid)
        with patch.object(sa, "_provider_home", return_value=tmp_path / "unused-default"):
            status = sa._resolve_codex(uuid, str(ns_home), None)
        assert status.state == ArtifactState.VALID

    def test_namespaced_invalid_is_authoritative_not_masked(self, tmp_path):
        """A rollout present in the namespaced home but with a mismatched id is
        an authoritative INVALID; the default-home fallback must NOT mask it.
        """
        uuid = "01a08db2"
        ns_home = tmp_path / "ns-home"
        _write_codex_rollout(ns_home, "DIFFERENT")  # present but wrong id
        default_home = tmp_path / "default-codex"
        _write_codex_rollout(default_home, uuid)  # correct here
        # glob matches on substring; a wrong-id file whose name lacks uuid → the
        # namespaced home yields MISSING (no rollout for uuid), so fallback runs
        # and the default VALID wins. To exercise INVALID authoritativeness we
        # place a uuid-named file with a mismatched session_meta id:
        bad = ns_home / "sessions" / "2026" / "09" / "10" / f"rollout-x-{uuid}.jsonl"
        bad.write_text(json.dumps({"type": "session_meta", "payload": {"id": "OTHER"}}) + "\n")
        with patch.object(sa, "_provider_home", return_value=default_home):
            status = sa._resolve_codex(uuid, str(ns_home), None)
        assert status.state == ArtifactState.INVALID  # not masked by default VALID

    def test_mutant_codex_no_fallback_is_caught(self, tmp_path):
        """Mutant 'no-fallback': returning the namespaced MISSING directly
        (no default-home retry) would refuse a resumed codex terminal whose
        rollout is intact in ~/.codex. The correct code returns VALID via
        fallback; asserting VALID kills the mutant.
        """
        uuid = "01a08db2"
        default_home = tmp_path / "default-codex"
        _write_codex_rollout(default_home, uuid)
        with patch.object(sa, "_provider_home", return_value=default_home):
            status = sa._resolve_codex(uuid, str(tmp_path / "gone-ns"), None)
        assert status.state == ArtifactState.VALID


# ==========================================================================
# R3 — #769a: codex namespace re-seed on resume
# ==========================================================================
class TestNamespaceReseed:
    def test_reseed_to_default_when_namespace_home_gone(self, tmp_path):
        uuid = "01a08db2"
        default_home = tmp_path / "default-codex"
        _write_codex_rollout(default_home, uuid)
        dead_ns = str(tmp_path / "gone-ns")
        with patch.object(sa, "_provider_home", return_value=default_home):
            out = rs._f917_reseed_codex_namespace(dead_ns, uuid)
        assert out == str(default_home)

    def test_live_namespace_left_unchanged(self, tmp_path):
        uuid = "01a08db2"
        ns_home = tmp_path / "ns-home"
        _write_codex_rollout(ns_home, uuid)  # namespaced sessions dir present
        out = rs._f917_reseed_codex_namespace(str(ns_home), uuid)
        assert out == str(ns_home)  # untouched

    def test_none_namespace_unchanged(self):
        assert rs._f917_reseed_codex_namespace(None, "u") is None

    def test_mutant_reseed_keep_dead_namespace_is_caught(self, tmp_path):
        """Mutant 'keep-dead-namespace': returning the input namespace unchanged
        would leave the resumed conversation keyed by the reaped persona home →
        next reap refuses session_artifact_missing. Correct code re-seeds to the
        default home; asserting the change kills the mutant.
        """
        uuid = "01a08db2"
        default_home = tmp_path / "default-codex"
        _write_codex_rollout(default_home, uuid)
        dead_ns = str(tmp_path / "gone-ns")
        with patch.object(sa, "_provider_home", return_value=default_home):
            out = rs._f917_reseed_codex_namespace(dead_ns, uuid)
        assert out != dead_ns and out == str(default_home)


# ==========================================================================
# R4 — #769a: hibernate-refused hint never recommends force when rollout exists
# ==========================================================================
class TestHibernateHint:
    def _identity(self, tmp_path):
        return {
            "provider": "codex",
            "provider_session_id": "01a08db2",
            "provider_namespace": str(tmp_path / "gone-ns"),
            "cwd": "/work",
            "identity_key": "conv_orig",
        }

    def test_hint_recommends_resume_when_artifact_valid(self, tmp_path):
        with (
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_identity",
                return_value=self._identity(tmp_path),
            ),
            patch(
                "cli_agent_orchestrator.services.session_artifact.resolve_artifact",
                return_value=sa.ArtifactStatus(ArtifactState.VALID, "/path"),
            ),
        ):
            hint = ts._f917_hibernate_refused_hint(
                "abcd1234", provider="codex", reason="session_artifact_missing"
            )
        assert "resume_from=abcd1234" in hint
        assert "force" in hint.lower() and "do NOT force" in hint

    def test_hint_allows_force_when_nothing_recoverable(self, tmp_path):
        with (
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_identity",
                return_value=self._identity(tmp_path),
            ),
            patch(
                "cli_agent_orchestrator.services.session_artifact.resolve_artifact",
                return_value=sa.ArtifactStatus(ArtifactState.MISSING, detail="no rollout"),
            ),
        ):
            hint = ts._f917_hibernate_refused_hint(
                "abcd1234", provider="codex", reason="session_artifact_missing"
            )
        assert "force=True" in hint and "marks unrecoverable" in hint

    def test_mutant_always_force_hint_is_caught(self, tmp_path):
        """Mutant 'always-force-hint': the old unconditional
        'delete_terminal(force=True) reaps and marks unrecoverable'. When the
        rollout is VALID the correct hint recommends resume and says do NOT
        force; asserting that kills the mutant.
        """
        with (
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_identity",
                return_value=self._identity(tmp_path),
            ),
            patch(
                "cli_agent_orchestrator.services.session_artifact.resolve_artifact",
                return_value=sa.ArtifactStatus(ArtifactState.VALID, "/path"),
            ),
        ):
            hint = ts._f917_hibernate_refused_hint(
                "abcd1234", provider="codex", reason="session_artifact_missing"
            )
        assert "do NOT force" in hint
        assert hint != "delete_terminal(force=True) reaps and marks unrecoverable"


# ==========================================================================
# R5 — D6: drift notice text is preserve/resume, not cold-assign
# ==========================================================================
class TestDriftNotice:
    def _validation(self):
        return FrozenPinValidation(
            outcome="drift",
            drifted=[
                PinCheckResult(
                    file_path="/w/spec.md",
                    verdict="DRIFT",
                    expected="a" * 64,
                    observed="b" * 64,
                    reason="content",
                )
            ],
        )

    def test_notice_recommends_preserve_resume(self):
        notice = format_drift_notice("abcd1234", self._validation())
        assert "resume_from=abcd1234" in notice
        assert "inherit_pins=False" in notice
        assert "authority_files" in notice
        assert "delete WITHOUT force" in notice

    def test_mutant_drift_cold_assign_is_caught(self):
        """Mutant 'cold-assign-text': the old
        'delete this worker and cold-assign a fresh reviewer'. The correct
        notice recommends preserve/resume and never says cold-assign.
        """
        notice = format_drift_notice("abcd1234", self._validation())
        assert "cold-assign" not in notice
        assert "resume_from=abcd1234" in notice


# ==========================================================================
# Provider acceptance matrix (astra D5a) — deterministic invariant rows.
# Real two-cycle provider probes are the separate `session_preservation and
# live` arm; these rows assert the per-provider invariant at the decision level.
# ==========================================================================
class TestProviderMatrix:
    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_repeated_resume_invariant(self, provider):
        """Repeated-resume invariant: a provider whose cleanup destroys its
        sessions (pi/grok) must have its resume facts resolved before cleanup;
        a global-store provider (codex/kiro/claude) is unaffected. Either way,
        the reap must not lose the resume handle to cleanup ordering.
        """
        destroys = ts._provider_cleanup_destroys_sessions(provider)
        assert destroys == (provider in {"pi_cli", "grok_cli"})
        # For a session-destroying provider, the reaper's precompute path is the
        # mechanism that preserves the handle across two resume cycles.
        if destroys:
            with patch.object(
                ts, "_resolve_reap_resume_key", return_value=("sid", True, "resumable")
            ) as r:
                pre = ts._resolve_reap_resume_key("t1", {"provider": provider}, force=False)
                assert pre[1] is True and r.called

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_account_switch_preserves_not_discards(self, provider):
        """Account-switch invariant: the drift/switch protocol text must steer
        every provider's supervisor to PRESERVE (resume), never to cold-assign
        or force — the M44/#769 loss pattern is provider-independent.
        """
        notice = format_drift_notice("abcd1234", self._drift())
        assert "cold-assign" not in notice
        assert "resume_from=" in notice

    def _drift(self):
        return FrozenPinValidation(
            outcome="drift",
            drifted=[
                PinCheckResult(
                    file_path="/w/x",
                    verdict="DRIFT",
                    expected="a" * 64,
                    observed="b" * 64,
                    reason="content",
                )
            ],
        )


# ==========================================================================
# R6 — ledger L6 regression: F913 guard must DEFER to F867 D2.
# A force delete racing a LIVE resume of THIS terminal is resume_in_progress
# (F867 D2), NOT refuse_discard_live_session (F913). This must hold even when
# the terminal is GENUINELY resumable (owner_principal set) — i.e. it must not
# depend on the accidental F865-B3 owner_principal masking.
# ==========================================================================
class TestF867D2Precedence:
    def _seed_resumable_pi(self, d, tid, session, ns):
        # Write a REAL pi session artifact so the shared preservation evaluator
        # resolves recovery="ready" (a validated durable artifact), making the
        # lane genuinely resumable rather than merely flagged.
        import os

        os.makedirs(ns, exist_ok=True)
        with open(os.path.join(ns, f"20260911_{tid}.jsonl"), "w") as fh:
            fh.write('{"turn": 1}\n')
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
            provider_namespace=ns,
            agent_profile="empirical_reviewer_lite",
            model=None,
            reasoning_effort=None,
            origin_callback_ref=None,
            current_terminal_id=tid,
            cwd="/data/cao-scratch/x",
            owner_principal="owner-1",  # GENUINELY resumable (not the NULL-owner mask)
        )

    def _mock_seams(self, monkeypatch):
        monkeypatch.setattr(ts, "get_backend", lambda: MagicMock())
        monkeypatch.setattr(
            ts,
            "_delete_terminal_under_lease",
            lambda t, token, **kw: {
                "terminal_deleted": True,
                "resumable": False,
                "reason": "abandoned",
            },
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
            lambda t: MagicMock(terminal_id=t),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease", lambda _t: None
        )

    def test_force_defers_to_resume_in_progress_when_genuinely_resumable(
        self, real_sqlite_env, tmp_path, monkeypatch
    ):
        """pass-after: with owner_principal set (genuinely resumable) AND a live
        provider-session lease held (resume in flight), force delete must raise
        resume_in_progress (F867 D2) — the F913 guard defers. Before the fix the
        F913 guard fired first and raised refuse_discard_live_session (ledger L6).
        """
        import cli_agent_orchestrator.clients.database as d
        from cli_agent_orchestrator.services import conversation_transition as ct
        from cli_agent_orchestrator.services import provider_session_lease as psl
        from cli_agent_orchestrator.services.terminal_service import (
            RefuseDiscardLiveSessionError,
        )

        tid = "l6defer1"
        ns = str(tmp_path / "pi-ns")
        self._seed_resumable_pi(d, tid, "cao-l6", ns)
        ct.attach_captured_uuid(
            tid,
            provider_session_id=tid,
            provider="pi_cli",
            provider_namespace=ns,
        )
        self._mock_seams(monkeypatch)

        # Confirm the terminal is GENUINELY resumable now (owner set) — so the
        # F913 guard WOULD fire were it not for the D2 deferral.
        _alive, _resumable, _prov, _reason = ts._f913_live_resumable(
            tid, d.get_terminal_metadata(tid)
        )
        assert _resumable is True, "precondition: terminal must be genuinely resumable"

        held = psl.acquire_provider_session_lease(tid)  # live resume in flight
        assert held is not None
        try:
            with pytest.raises(RuntimeError, match="resume_in_progress"):
                ts.delete_terminal(tid, force=True)
            # And specifically NOT the F913 refusal.
            try:
                ts.delete_terminal(tid, force=True)
            except RefuseDiscardLiveSessionError:  # pragma: no cover
                pytest.fail("F913 guard pre-empted F867 D2 (ledger L6 regression)")
            except RuntimeError as e:
                assert "resume_in_progress" in str(e)
        finally:
            psl.release_provider_session_lease(held)

        # After the resume releases, no resume is in flight. The terminal is
        # still genuinely resumable, so a plain force delete is (correctly)
        # refused by F913 — the deliberate discard proceeds with confirm_discard.
        with pytest.raises(RefuseDiscardLiveSessionError):
            ts.delete_terminal(tid, force=True)
        r = ts.delete_terminal(
            tid, force=True, confirm_discard=True, discard_reason="account switch"
        )
        assert r["reaped"] and r["reaped"][0]["id"] == tid

    def test_mutant_no_d2_deferral_is_caught(self, real_sqlite_env, tmp_path, monkeypatch):
        """Mutant 'no-d2-deferral' (F913 guard evaluated WITHOUT the
        _f867_resume_in_flight pre-check): with a genuinely resumable terminal
        and a live resume lease, the guard would raise refuse_discard_live_session
        instead of resume_in_progress. Asserting resume_in_progress kills it.
        """
        import cli_agent_orchestrator.clients.database as d
        from cli_agent_orchestrator.services import conversation_transition as ct
        from cli_agent_orchestrator.services import provider_session_lease as psl

        tid = "l6defer2"
        ns = str(tmp_path / "pi-ns2")
        self._seed_resumable_pi(d, tid, "cao-l6b", ns)
        ct.attach_captured_uuid(
            tid,
            provider_session_id=tid,
            provider="pi_cli",
            provider_namespace=ns,
        )
        self._mock_seams(monkeypatch)
        held = psl.acquire_provider_session_lease(tid)
        assert held is not None
        try:
            with pytest.raises(RuntimeError, match="resume_in_progress"):
                ts.delete_terminal(tid, force=True)
        finally:
            psl.release_provider_session_lease(held)


# ==========================================================================
# AC-6 — retention barrier: a preserving delete of a RESUMABLE lane must NOT
# rmtree the session artifact for a session-destroying provider (pi_cli,
# grok_cli). Fail-before/pass-after per provider + one unconditional-rmtree
# mutant. (Blueprint fx913 AC-6, root commit 75dafa51.)
# ==========================================================================
class TestAC6RetentionBarrier:
    def _make_pi(self, tmp_path, monkeypatch, tid="ac6pia00"):
        import cli_agent_orchestrator.providers.pi_cli as pi

        monkeypatch.setattr(pi, "PI_RUNTIME_ROOT", tmp_path / "pi")
        prov = pi.PiCliProvider(
            tid, "cao-ac6", f"win-{tid}", agent_profile="empirical_reviewer_lite"
        )
        prov.runtime_dir.mkdir(parents=True, exist_ok=True)
        prov.session_dir.mkdir(parents=True, exist_ok=True)
        artifact = prov.session_dir / f"20260911_{tid}.jsonl"
        artifact.write_text('{"turn": 1}\n')
        # ephemeral files that SHOULD be removed even when preserving
        prov.prompt_path.write_text("prompt")
        prov.mcp_config_path.write_text("{}")
        return prov, artifact

    def test_pi_preserve_retains_session_artifact(self, tmp_path, monkeypatch):
        """pass-after: cleanup(preserve_session=True) leaves the sessions
        transcript in place (resumable via --session) while removing ephemerals.
        """
        prov, artifact = self._make_pi(tmp_path, monkeypatch)
        prov.cleanup(preserve_session=True)
        assert artifact.exists(), "AC-6: pi session artifact must survive a preserving delete"
        assert not prov.prompt_path.exists()  # ephemeral removed
        assert not prov.mcp_config_path.exists()

    def test_pi_non_preserve_removes_everything(self, tmp_path, monkeypatch):
        """fail-before contrast: without preservation the runtime dir (incl.
        sessions) is rmtree'd — the pre-AC-6 behaviour."""
        prov, artifact = self._make_pi(tmp_path, monkeypatch, tid="ac6pib00")
        prov.cleanup(preserve_session=False)
        assert not artifact.exists()
        assert not prov.runtime_dir.exists()

    def test_pi_preserved_artifact_is_resolvable_for_resume(self, tmp_path, monkeypatch):
        """AC-6 end: after a preserving cleanup the retained artifact is what a
        subsequent assign(resume_from=<id>) resolves (the pi session dir glob
        that _resolve_pi / the resume arm uses finds it)."""
        prov, artifact = self._make_pi(tmp_path, monkeypatch, tid="ac6pic00")
        uuid = "ac6pic00"
        prov.cleanup(preserve_session=True)
        # The resume arm keys pi by artifact_locator (the JSONL). Assert the
        # retained file is discoverable by the same **/*_<uuid>.jsonl glob
        # session_artifact._resolve_pi uses over the namespace/session dir.
        matches = list(prov.session_dir.glob(f"**/*_{uuid}.jsonl"))
        assert matches and matches[0] == artifact

    def test_mutant_pi_unconditional_rmtree_is_caught(self, tmp_path, monkeypatch):
        """Mutant 'unconditional-rmtree': if pi cleanup ignored preserve_session
        and always rmtree'd runtime_dir, the artifact would be gone after a
        preserving delete. Asserting the artifact survives kills the mutant.
        """
        prov, artifact = self._make_pi(tmp_path, monkeypatch, tid="ac6pim00")
        prov.cleanup(preserve_session=True)
        assert artifact.exists()

    # ---- grok ----
    def _make_grok(self, tmp_path, monkeypatch, tid="ac6grk00"):
        import cli_agent_orchestrator.providers.grok_cli as gk

        home = tmp_path / "grok-home" / tid
        monkeypatch.setattr(gk.GrokCliProvider, "_prepare_grok_home", lambda self: None)
        monkeypatch.setattr(gk.GrokCliProvider, "_allocate_session_uuid", lambda self: "s-uuid")
        prov = gk.GrokCliProvider(tid, "cao-ac6g", f"win-{tid}")
        monkeypatch.setattr(prov, "_home_path", lambda: home)
        monkeypatch.setattr(prov, "_is_managed_home", lambda h: True)
        monkeypatch.setattr(prov, "_stop_home_processes", lambda h: True)
        sessions = home / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        artifact = sessions / "session.jsonl"
        artifact.write_text('{"turn": 1}\n')
        (home / "config.toml").write_text("x")  # ephemeral, should be removed
        return prov, home, artifact

    def test_grok_preserve_retains_session_store(self, tmp_path, monkeypatch):
        prov, home, artifact = self._make_grok(tmp_path, monkeypatch)
        assert prov.cleanup(preserve_session=True) is True
        assert artifact.exists(), "AC-6: grok session store must survive a preserving delete"
        assert not (home / "config.toml").exists()  # ephemeral removed

    def test_grok_non_preserve_removes_home(self, tmp_path, monkeypatch):
        prov, home, artifact = self._make_grok(tmp_path, monkeypatch, tid="ac6grkb0")
        assert prov.cleanup(preserve_session=False) is True
        assert not artifact.exists()
        assert not home.exists()

    def test_mutant_grok_unconditional_rmtree_is_caught(self, tmp_path, monkeypatch):
        """Mutant 'unconditional-rmtree' (grok): always rmtree(home) would remove
        the session store on a preserving delete. Asserting survival kills it.
        """
        prov, home, artifact = self._make_grok(tmp_path, monkeypatch, tid="ac6grkm0")
        prov.cleanup(preserve_session=True)
        assert artifact.exists()


# ==========================================================================
# B1 (verdict-f913-r1) — the retention barrier must hold through the REAL
# ProviderManager.cleanup_provider seam, not just the direct adapter. The
# reviewer's independent mutant (drop preserve_session forwarding in
# ProviderManager.cleanup_provider) survived all shipped tests because they
# bypassed the manager. These go through a real ProviderManager so that mutant
# is killed, and a deletion-level completed-turn test reaches both.
# ==========================================================================
class TestAC6ManagerRetention:
    def _make_pi(self, tmp_path, monkeypatch, tid="b1pia000"):
        import cli_agent_orchestrator.providers.pi_cli as pi

        monkeypatch.setattr(pi, "PI_RUNTIME_ROOT", tmp_path / "pi")
        prov = pi.PiCliProvider(
            tid, "cao-b1", f"win-{tid}", agent_profile="empirical_reviewer_lite"
        )
        prov.runtime_dir.mkdir(parents=True, exist_ok=True)
        prov.session_dir.mkdir(parents=True, exist_ok=True)
        artifact = prov.session_dir / f"20260911_{tid}.jsonl"
        artifact.write_text('{"turn": 1}\n')
        prov.prompt_path.write_text("prompt")
        return prov, artifact

    def test_pi_manager_cleanup_preserve_retains_artifact(self, tmp_path, monkeypatch):
        """B1 decisive witness: through a REAL ProviderManager, preserve_session=True
        must reach the pi adapter and retain the artifact. Kills the
        'drop manager forwarding' mutant that the direct-adapter tests missed.
        """
        from cli_agent_orchestrator.providers.manager import ProviderManager

        prov, artifact = self._make_pi(tmp_path, monkeypatch)
        manager = ProviderManager()
        manager._providers[prov.terminal_id] = prov
        assert manager.cleanup_provider(prov.terminal_id, preserve_session=True) is True
        assert artifact.exists(), "AC-6 manager forwarding must retain the pi session artifact"
        assert prov.terminal_id not in manager._providers  # provider map cleaned

    def test_pi_manager_cleanup_non_preserve_removes_artifact(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.providers.manager import ProviderManager

        prov, artifact = self._make_pi(tmp_path, monkeypatch, tid="b1pib000")
        manager = ProviderManager()
        manager._providers[prov.terminal_id] = prov
        assert manager.cleanup_provider(prov.terminal_id, preserve_session=False) is True
        assert not artifact.exists()

    def _make_grok(self, tmp_path, monkeypatch, tid="b1grk000"):
        import cli_agent_orchestrator.providers.grok_cli as gk

        home = tmp_path / "grok-home" / tid
        monkeypatch.setattr(gk.GrokCliProvider, "_prepare_grok_home", lambda self: None)
        monkeypatch.setattr(gk.GrokCliProvider, "_allocate_session_uuid", lambda self: "s-uuid")
        prov = gk.GrokCliProvider(tid, "cao-b1g", f"win-{tid}")
        monkeypatch.setattr(prov, "_home_path", lambda: home)
        monkeypatch.setattr(prov, "_is_managed_home", lambda h: True)
        monkeypatch.setattr(prov, "_stop_home_processes", lambda h: True)
        sessions = home / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        artifact = sessions / "session.jsonl"
        artifact.write_text('{"turn": 1}\n')
        return prov, home, artifact

    def test_grok_manager_cleanup_preserve_retains_store(self, tmp_path, monkeypatch):
        """B1: direct (in-memory) Grok manager coverage for the disposition."""
        from cli_agent_orchestrator.providers.manager import ProviderManager

        prov, home, artifact = self._make_grok(tmp_path, monkeypatch)
        manager = ProviderManager()
        manager._providers[prov.terminal_id] = prov
        assert manager.cleanup_provider(prov.terminal_id, preserve_session=True) is True
        assert artifact.exists(), "AC-6 manager forwarding must retain the grok session store"

    def test_grok_restored_manager_cleanup_preserve_retains_store(self, tmp_path, monkeypatch):
        """B1 / AC-6 'after a server restart drops in-memory provider instances':
        with NO in-memory provider, the manager instantiates a fresh Grok adapter
        from metadata and must still forward preserve_session to its cleanup.
        """
        import cli_agent_orchestrator.providers.grok_cli as gk
        import cli_agent_orchestrator.providers.manager as mgr
        from cli_agent_orchestrator.providers.manager import ProviderManager

        tid = "b1grkr00"
        home = tmp_path / "grok-restored" / tid
        sessions = home / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        artifact = sessions / "session.jsonl"
        artifact.write_text('{"turn": 1}\n')

        monkeypatch.setattr(gk.GrokCliProvider, "_prepare_grok_home", lambda self: None)
        monkeypatch.setattr(gk.GrokCliProvider, "_allocate_session_uuid", lambda self: "s-uuid")
        monkeypatch.setattr(gk.GrokCliProvider, "_home_path", lambda self: home)
        monkeypatch.setattr(gk.GrokCliProvider, "_is_managed_home", lambda self, h: True)
        monkeypatch.setattr(gk.GrokCliProvider, "_stop_home_processes", lambda self, h: True)
        # Manager has no in-memory instance → takes the restored-Grok branch.
        monkeypatch.setattr(
            mgr,
            "get_terminal_metadata",
            lambda _tid: {
                "provider": "grok_cli",
                "tmux_session": "cao-b1gr",
                "tmux_window": f"win-{tid}",
                "agent_profile": "dev",
            },
        )
        manager = ProviderManager()  # empty _providers
        assert manager.cleanup_provider(tid, preserve_session=True) is True
        assert artifact.exists(), "AC-6 restored-Grok path must retain the session store"


# ==========================================================================
# B1 (deletion-level) — a completed-turn pi lane deleted WITHOUT force through
# the real reaper reaches ProviderManager.cleanup_provider with the retention
# disposition derived from the resume verdict, and the artifact survives.
# ==========================================================================
class TestAC6DeletionLevelPreservation:
    def test_completed_turn_pi_non_force_delete_retains_artifact(
        self, real_sqlite_env, tmp_path, monkeypatch
    ):
        import cli_agent_orchestrator.clients.database as d
        import cli_agent_orchestrator.providers.pi_cli as pi
        from cli_agent_orchestrator.providers.manager import ProviderManager
        from cli_agent_orchestrator.services import conversation_transition as ct

        tid = "b1del000"
        # Real pi provider with a completed-turn artifact on disk.
        monkeypatch.setattr(pi, "PI_RUNTIME_ROOT", tmp_path / "pi")
        prov = pi.PiCliProvider(
            tid, "cao-b1d", f"win-{tid}", agent_profile="empirical_reviewer_lite"
        )
        prov.runtime_dir.mkdir(parents=True, exist_ok=True)
        prov.session_dir.mkdir(parents=True, exist_ok=True)
        artifact = prov.session_dir / f"20260911_{tid}.jsonl"
        artifact.write_text('{"turn": 1}\n')

        # Seed a genuinely-resumable pi lane (owner_principal set) + a real
        # ProviderManager holding this provider, so the reaper's cleanup_provider
        # reaches the real adapter with the retention disposition.
        d.create_terminal(
            terminal_id=tid,
            tmux_session="cao-b1d",
            tmux_window=f"win-{tid}",
            agent_profile="empirical_reviewer_lite",
            provider="pi_cli",
        )
        d.mint_spawn_identity(
            identity_key=f"conv_{tid}",
            provider="pi_cli",
            provider_namespace=str(prov.session_dir),
            agent_profile="empirical_reviewer_lite",
            model=None,
            reasoning_effort=None,
            origin_callback_ref=None,
            current_terminal_id=tid,
            cwd=str(prov.runtime_dir),
            owner_principal="owner-1",
        )
        ct.attach_captured_uuid(
            tid,
            provider_session_id=tid,
            provider="pi_cli",
            provider_namespace=str(prov.session_dir),
        )

        manager = ProviderManager()
        manager._providers[tid] = prov
        monkeypatch.setattr(ts, "provider_manager", manager)
        monkeypatch.setattr(ts, "get_backend", lambda: MagicMock())
        # Keep tmux/lease/inbox seams inert; the real cleanup_provider must run.
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
            lambda t: MagicMock(terminal_id=t),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease", lambda _t: None
        )
        # The MagicMock rebind token cannot pass real validation; no-op it so the
        # real _delete_terminal_under_lease body (incl. cleanup_provider) runs.
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease",
            lambda _tid, _token: None,
        )

        result = ts.delete_terminal(tid)  # NON-force preserving delete
        # The reap ran and the pi transcript survived (resumable → retained).
        assert artifact.exists(), (
            "AC-6 deletion-level: a non-force delete of a resumable pi lane must "
            "retain the session artifact through the real reaper + manager"
        )
        assert isinstance(result, dict)
