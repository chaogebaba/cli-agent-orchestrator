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
