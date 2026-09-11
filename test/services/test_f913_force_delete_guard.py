"""F913 (#765) — refuse force-delete of a LIVE, RESUMABLE worker; resume is
the drift path.

The M44 incident: a supervisor cold-spawned a codex reviewer on pin drift and
force-deleted the live one mid-review. The reap abandoned a still-resumable
provider session (status=killed_while_busy resumable=false
reason=abandoned_force_delete) and the codex quota already spent was lost, then
re-spent on the fresh spawn. Root cause: ``force=true`` discarded a live,
resumable session with no guard.

This suite pins the four rules of the fix at the level where the guard actually
lives (``terminal_service.delete_terminal`` + its decision helper
``_f913_live_resumable``), plus one NAMED mutant per rule shown killed.

RULE / MUTANT MAP (each mutant killed by the paired NAMED test):
* RULE-1 refuse (alive AND resumable, force, no confirm) → 409 typed
    mutant M1 "drop-the-guard" (never raise) →
        test_mutant_m1_drop_guard_is_caught
* RULE-2 confirm_discard=True bypasses the refusal → deleted
    mutant M2 "ignore-confirm" (refuse even with confirm) →
        test_mutant_m2_ignore_confirm_is_caught
* RULE-3 dead / non-resumable session with force → deleted (no false refusal —
    the existing pi hibernate_refused / session_artifact_missing recovery path)
    mutant M3 "guard-on-resumable-alone" (refuse ignoring liveness) →
        test_mutant_m3_dead_session_false_refusal_is_caught
* RULE-4 non-force delete of a busy terminal → resumable=true in the reap result
    (already the resume contract; a regression here silently loses the handle)
    mutant M4 "force-decides-resumable" (resolve with force=True) →
        test_mutant_m4_nonforce_busy_reports_resumable_is_caught
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.terminal_service import (
    RefuseDiscardLiveSessionError,
    _f913_live_resumable,
    delete_terminal,
)

TID = "abcd1234"
_ROOT = {"tmux_session": "s", "provider": "codex", "provider_session_id": None}


def _obs(status: TerminalStatus) -> MagicMock:
    o = MagicMock()
    o.status = status
    return o


# ==========================================================================
# _f913_live_resumable — thin ADAPTER over the shared evaluator (AC-1). These
# assert the tri-state → (alive, resumable) mapping; the shared evaluator's own
# fact derivation is exercised in TestSharedPreservationEvaluator below.
# ==========================================================================
def _decision(alive, recovery, provider="codex", reason="resumable", resume_from=None):
    from cli_agent_orchestrator.services.conversation_transition import (
        SessionPreservationDecision,
    )

    return SessionPreservationDecision(
        alive=alive,
        recovery=recovery,
        protected=(alive != "no" or recovery != "absent"),
        provider=provider,
        reason=reason,
        resume_from=resume_from,
    )


class TestLiveResumableDecision:
    def test_alive_and_resumable_when_ready(self):
        with patch(
            "cli_agent_orchestrator.services.conversation_transition.evaluate_session_preservation",
            return_value=_decision("yes", "ready"),
        ):
            alive, resumable, provider, reason = _f913_live_resumable(TID, _ROOT)
        assert alive is True and resumable is True
        assert provider == "codex" and reason == "resumable"

    def test_dead_when_alive_no(self):
        with patch(
            "cli_agent_orchestrator.services.conversation_transition.evaluate_session_preservation",
            return_value=_decision("no", "ready"),
        ):
            alive, resumable, _prov, _reason = _f913_live_resumable(TID, _ROOT)
        assert alive is False  # alive == "no" → not alive
        assert resumable is True  # recovery ready → resumable

    def test_liveness_unknown_fails_open_as_alive(self):
        with patch(
            "cli_agent_orchestrator.services.conversation_transition.evaluate_session_preservation",
            return_value=_decision("unknown", "ready"),
        ):
            alive, resumable, _prov, _reason = _f913_live_resumable(TID, _ROOT)
        assert alive is True  # unknown → alive (fail open)
        assert resumable is True

    def test_nonresumable_when_recovery_not_ready(self):
        with patch(
            "cli_agent_orchestrator.services.conversation_transition.evaluate_session_preservation",
            return_value=_decision("yes", "absent", reason="provider_session_id_never_captured"),
        ):
            alive, resumable, _prov, reason = _f913_live_resumable(TID, _ROOT)
        assert alive is True and resumable is False
        assert reason == "provider_session_id_never_captured"

    def test_adapter_degrades_permissively_on_error(self):
        """A failure in the shared evaluator degrades to (alive, non-resumable)
        so a genuinely dead/non-resumable session is never falsely refused."""
        with patch(
            "cli_agent_orchestrator.services.conversation_transition.evaluate_session_preservation",
            side_effect=RuntimeError("boom"),
        ):
            alive, resumable, provider, reason = _f913_live_resumable(TID, _ROOT)
        assert alive is True and resumable is False
        assert provider == "codex" and reason == "capture_error"


# ==========================================================================
# delete_terminal end-to-end guard behaviour (RULES 1-3)
# ==========================================================================
class TestDeleteTerminalGuard:
    def _patched(self, *, alive, resumable, provider="codex", reason="resumable"):
        """Patch the guard's decision + everything past it so a pass-through
        never touches tmux/leases/DB (returns a sentinel from the inner delete).
        """
        return (
            patch.object(ts, "get_terminal_metadata", return_value=dict(_ROOT)),
            patch.object(
                ts,
                "_f913_live_resumable",
                return_value=(alive, resumable, provider, reason),
            ),
            patch.object(ts, "_delete_terminal_inner", return_value={"reaped": ["passed-through"]}),
        )

    def test_rule1_refuse_when_alive_and_resumable(self):
        p_meta, p_dec, p_inner = self._patched(alive=True, resumable=True)
        with p_meta, p_dec, p_inner as inner:
            with pytest.raises(RefuseDiscardLiveSessionError) as ei:
                delete_terminal(TID, force=True)
        inner.assert_not_called()  # refused BEFORE any teardown work
        detail = ei.value.detail()
        assert detail["error"] == "refuse_discard_live_session"
        assert f"assign(resume_from={TID})" in detail["how"]
        assert detail["provider"] == "codex"
        assert detail["reason"] == "resumable"

    def test_rule2_confirm_discard_bypasses_refusal(self):
        p_meta, p_dec, p_inner = self._patched(alive=True, resumable=True)
        with p_meta, p_dec, p_inner as inner:
            # AC-4: confirm_discard now requires a non-blank discard_reason.
            result = delete_terminal(
                TID, force=True, confirm_discard=True, discard_reason="account switch"
            )
        assert result == {"reaped": ["passed-through"]}
        inner.assert_called_once()

    def test_rule3_dead_session_force_deletes_no_false_refusal(self):
        # Dead / unreachable session: resolver says non-resumable — the pi
        # hibernate_refused / session_artifact_missing recovery path.
        p_meta, p_dec, p_inner = self._patched(
            alive=False, resumable=False, reason="session_artifact_missing"
        )
        with p_meta, p_dec, p_inner as inner:
            result = delete_terminal(TID, force=True)
        assert result == {"reaped": ["passed-through"]}
        inner.assert_called_once()

    def test_alive_but_nonresumable_force_deletes(self):
        # Alive but nothing to resume (e.g. never captured a session id): the
        # guard must NOT refuse — there is no resume to protect.
        p_meta, p_dec, p_inner = self._patched(
            alive=True, resumable=False, reason="provider_session_id_never_captured"
        )
        with p_meta, p_dec, p_inner as inner:
            result = delete_terminal(TID, force=True)
        assert result == {"reaped": ["passed-through"]}
        inner.assert_called_once()

    def test_nonforce_never_reaches_guard(self):
        # A non-force delete already interrupts-and-preserves (the resume
        # contract). The guard is force-only: with force=False the decision
        # helper must never be consulted. Assert that without running the full
        # non-force teardown by short-circuiting the F829 hibernate gate to a
        # typed refusal (returns before any intent/tmux work).
        hib = MagicMock()
        hib.allowed = False
        hib.identity_key = None
        hib.provider = "codex"
        hib.reason = "no_artifact"
        hib.detail = None
        with (
            patch.object(ts, "get_terminal_metadata", return_value=dict(_ROOT)),
            patch.object(ts, "_f913_live_resumable") as dec,
            patch(
                "cli_agent_orchestrator.services.conversation_transition."
                "evaluate_planned_hibernate",
                return_value=hib,
            ),
        ):
            result = delete_terminal(TID, force=False)
        assert result["skipped"][0]["kind"] == "hibernate_refused"
        dec.assert_not_called()  # guard is force-only


# ==========================================================================
# RULE-4 — non-force delete of a busy terminal reports resumable=true.
# Exercises the real reap-result assembly path in _resolve_reap_resume_key:
# a resumable provider with a known session id, force=False → resumable True.
# ==========================================================================
class TestNonForceBusyReportsResumable:
    def _identity(self):
        return {
            "provider": "codex",
            "cwd": "/work",
            "provider_session_id": "sess-xyz",
            "identity_key": "ik-1",
        }

    def test_nonforce_resolves_resumable_true(self):
        with (
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_identity",
                return_value=self._identity(),
            ),
            patch(
                "cli_agent_orchestrator.services.resume_service.provider_supports_resume",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.get_conversation_identity",
                return_value={"provider_session_id": "sess-xyz", "owner_principal": "mb_owner"},
            ),
        ):
            cap, resumable, reason = ts._resolve_reap_resume_key(TID, dict(_ROOT), force=False)
        assert resumable is True and reason == "resumable"
        assert cap is None  # id already known; no capture needed

    # ------------------------------------------------------------------
    # MUTANTS
    # ------------------------------------------------------------------
    def test_mutant_m4_nonforce_busy_reports_resumable_is_caught(self):
        """M4 'force-decides-resumable': resolving with force=True on the
        non-force busy path returns abandoned_force_delete (resumable False),
        silently losing the handle the operator needs. The correct force=False
        call returns resumable=true; asserting that difference kills the mutant.
        """
        ident = self._identity()
        with (
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_identity",
                return_value=ident,
            ),
            patch(
                "cli_agent_orchestrator.services.resume_service.provider_supports_resume",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.get_conversation_identity",
                return_value={"provider_session_id": "sess-xyz", "owner_principal": "mb_owner"},
            ),
        ):
            # correct (force=False)
            _c, resumable_ok, reason_ok = ts._resolve_reap_resume_key(TID, dict(_ROOT), force=False)
            # mutant (force=True)
            _c2, resumable_mut, reason_mut = ts._resolve_reap_resume_key(
                TID, dict(_ROOT), force=True
            )
        assert resumable_ok is True and reason_ok == "resumable"
        assert resumable_mut is False and reason_mut == "abandoned_force_delete"


# ==========================================================================
# RULE-1 / RULE-2 / RULE-3 mutants at the guard level
# ==========================================================================
class TestGuardMutants:
    def _run(self, *, alive, resumable, force=True, confirm_discard=False, discard_reason="switch"):
        with (
            patch.object(ts, "get_terminal_metadata", return_value=dict(_ROOT)),
            patch.object(
                ts,
                "_f913_live_resumable",
                return_value=(alive, resumable, "codex", "resumable"),
            ),
            patch.object(ts, "_delete_terminal_inner", return_value={"reaped": ["ok"]}),
        ):
            return delete_terminal(
                TID, force=force, confirm_discard=confirm_discard, discard_reason=discard_reason
            )

    def test_mutant_m1_drop_guard_is_caught(self):
        """M1 'drop-the-guard' (never raise): a live+resumable force delete
        would silently proceed. The correct code raises; this asserts the raise.
        """
        with pytest.raises(RefuseDiscardLiveSessionError):
            self._run(alive=True, resumable=True)

    def test_mutant_m2_ignore_confirm_is_caught(self):
        """M2 'ignore-confirm' (refuse even with confirm): the deliberate
        discard would be impossible. The correct code lets confirm through.
        """
        result = self._run(alive=True, resumable=True, confirm_discard=True)
        assert result == {"reaped": ["ok"]}

    def test_mutant_m3_dead_session_false_refusal_is_caught(self):
        """M3 'guard-on-resumable-alone' (refuse ignoring liveness): a dead but
        still-'resumable'-flagged session would be falsely refused, breaking the
        existing recovery reap. The correct code requires alive AND resumable,
        so a dead session (alive=False) proceeds.
        """
        result = self._run(alive=False, resumable=True)
        assert result == {"reaped": ["ok"]}


# ==========================================================================
# AC-4 (blueprint fx913) — explicit discard requires LITERAL true confirmation
# + a non-blank discard_reason, and records an audit tombstone.
# ==========================================================================
class TestAC4DiscardConfirmation:
    def _patched(self, tombstone):
        from cli_agent_orchestrator.services.terminal_service import DiscardConfirmationError

        return DiscardConfirmationError, (
            patch.object(ts, "get_terminal_metadata", return_value=dict(_ROOT)),
            patch.object(
                ts, "_f913_live_resumable", return_value=(True, True, "codex", "resumable")
            ),
            patch.object(ts, "_delete_terminal_inner", return_value={"reaped": ["ok"]}),
            patch.object(ts, "_f913_record_discard_tombstone", tombstone),
        )

    def test_confirm_true_with_reason_proceeds_and_tombstones(self):
        calls = []
        tomb = MagicMock(side_effect=lambda *a, **k: calls.append((a, k)))
        _err, (p_meta, p_dec, p_inner, p_tomb) = self._patched(tomb)
        with p_meta, p_dec, p_inner, p_tomb:
            result = ts.delete_terminal(
                TID, force=True, confirm_discard=True, discard_reason="account switch"
            )
        assert result == {"reaped": ["ok"]}
        assert tomb.called  # AC-4 tombstone recorded
        assert tomb.call_args.kwargs.get("reason") == "account switch"

    def test_confirm_true_blank_reason_is_refused(self):
        err, (p_meta, p_dec, p_inner, p_tomb) = self._patched(MagicMock())
        with p_meta, p_dec, p_inner, p_tomb:
            with pytest.raises(err):
                ts.delete_terminal(TID, force=True, confirm_discard=True, discard_reason="   ")

    def test_confirm_true_no_reason_is_refused(self):
        err, (p_meta, p_dec, p_inner, p_tomb) = self._patched(MagicMock())
        with p_meta, p_dec, p_inner, p_tomb:
            with pytest.raises(err):
                ts.delete_terminal(TID, force=True, confirm_discard=True)

    def test_mutant_truthy_confirm_accepted_is_caught(self):
        """Mutant 'truthy-confirm' (confirm_discard treated as truthy, reason
        unchecked): a discard with a blank reason would proceed. The correct
        contract requires literal True + non-blank reason; asserting the blank
        reason is refused kills the mutant.
        """
        err, (p_meta, p_dec, p_inner, p_tomb) = self._patched(MagicMock())
        with p_meta, p_dec, p_inner, p_tomb:
            with pytest.raises(err):
                ts.delete_terminal(TID, force=True, confirm_discard=True, discard_reason="")


# ==========================================================================
# AC-1 / AC-7 — the shared evaluate_session_preservation tri-state decision,
# force-independent. Exercises the fact derivation directly.
# ==========================================================================
class TestSharedPreservationEvaluator:
    def _patch(self, *, obs_status, identity, root, supports, artifact_state):
        from cli_agent_orchestrator.services.session_artifact import ArtifactState, ArtifactStatus

        obs = MagicMock()
        obs.status = obs_status
        sm = MagicMock()
        sm.get_boundary_observation.return_value = obs
        return (
            patch("cli_agent_orchestrator.services.status_monitor.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_identity",
                return_value=identity,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.get_conversation_identity",
                return_value=root,
            ),
            patch(
                "cli_agent_orchestrator.services.resume_service.provider_supports_resume",
                return_value=supports,
            ),
            patch(
                "cli_agent_orchestrator.services.session_artifact.resolve_artifact",
                return_value=ArtifactStatus(artifact_state, "/p"),
            ),
        )

    def test_ready_when_supported_owned_and_artifact_valid(self):
        from cli_agent_orchestrator.services.conversation_transition import (
            evaluate_session_preservation,
        )
        from cli_agent_orchestrator.services.session_artifact import ArtifactState

        ident = {"provider": "codex", "identity_key": "conv_x", "provider_session_id": "sid"}
        root = {"owner_principal": "mb_o", "provider_session_id": "sid"}
        p1, p2, p3, p4, p5 = self._patch(
            obs_status=TerminalStatus.IDLE,
            identity=ident,
            root=root,
            supports=True,
            artifact_state=ArtifactState.VALID,
        )
        with p1, p2, p3, p4, p5:
            d = evaluate_session_preservation(TID)
        assert d.alive == "yes" and d.recovery == "ready" and d.protected is True
        assert d.resume_from == TID

    def test_error_status_is_unknown_not_dead(self):
        from cli_agent_orchestrator.services.conversation_transition import (
            evaluate_session_preservation,
        )
        from cli_agent_orchestrator.services.session_artifact import ArtifactState

        ident = {"provider": "codex", "identity_key": "conv_x", "provider_session_id": "sid"}
        root = {"owner_principal": "mb_o", "provider_session_id": "sid"}
        p1, p2, p3, p4, p5 = self._patch(
            obs_status=TerminalStatus.ERROR,
            identity=ident,
            root=root,
            supports=True,
            artifact_state=ArtifactState.VALID,
        )
        with p1, p2, p3, p4, p5:
            d = evaluate_session_preservation(TID)
        assert d.alive == "unknown"  # ERROR is not authoritative death
        assert d.protected is True

    def test_absent_recovery_when_missing_artifact_and_dead(self):
        from cli_agent_orchestrator.services.conversation_transition import (
            evaluate_session_preservation,
        )
        from cli_agent_orchestrator.services.session_artifact import ArtifactState

        ident = {"provider": "codex", "identity_key": "conv_x", "provider_session_id": "sid"}
        root = {"owner_principal": "mb_o", "provider_session_id": "sid"}
        p1, p2, p3, p4, p5 = self._patch(
            obs_status=TerminalStatus.ERROR,
            identity=ident,
            root=root,
            supports=True,
            artifact_state=ArtifactState.MISSING,
        )
        with p1, p2, p3, p4, p5:
            d = evaluate_session_preservation(TID)
        # alive unknown (ERROR) so still protected on the alive axis, but recovery
        # is a positively-established absent.
        assert d.recovery == "absent"

    def test_force_independent_same_verdict(self):
        """AC-7: the decision does not take force as input; the SAME facts yield
        the SAME verdict regardless of any caller force intent (there is no force
        parameter on the evaluator at all)."""
        import inspect

        from cli_agent_orchestrator.services.conversation_transition import (
            evaluate_session_preservation,
        )

        sig = inspect.signature(evaluate_session_preservation)
        assert "force" not in sig.parameters


# ==========================================================================
# AC-3 (structural) — the shared decision is the source the public delete guard
# consults; _f913_live_resumable delegates to evaluate_session_preservation so
# no destructive guard path re-implements the conjunction.
# ==========================================================================
class TestAC3SharedDecisionSourced:
    def test_guard_helper_delegates_to_shared_evaluator(self):
        called = {"n": 0}

        def _fake(_tid):
            called["n"] += 1
            from cli_agent_orchestrator.services.conversation_transition import (
                SessionPreservationDecision,
            )

            return SessionPreservationDecision(
                alive="yes",
                recovery="ready",
                protected=True,
                provider="codex",
                reason="resumable",
                resume_from=_tid,
            )

        with patch(
            "cli_agent_orchestrator.services.conversation_transition.evaluate_session_preservation",
            side_effect=_fake,
        ):
            alive, resumable, _p, _r = _f913_live_resumable(TID, _ROOT)
        assert called["n"] == 1  # the guard helper consulted the shared evaluator
        assert alive is True and resumable is True
