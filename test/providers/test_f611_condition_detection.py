"""F611 (#467) — provider condition detection acceptance arms (AC1–AC15).

Each arm replays a BYTE-EXACT corpus fixture (test/providers/fixtures/conditions/
or test/providers/fixtures/status_truth/) through the classifier and asserts the
blueprint's decision-wall contract. The arm brings its own loader — the condition
corpus has no other test consumer.

Falsifier→arm map (blueprint §5/§8): D1→AC2, D2→AC5, D3→AC7, D4→AC8, D5→AC3,
D6→AC9, D7→AC10, D8→AC11.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.condition import (
    ConditionDelivery,
    ConditionKind,
    Confidence,
    PolicyAction,
    banner_rows,
    classify_condition,
    policy_for_condition,
    should_deliver,
)

_FIX = Path(__file__).parent / "fixtures"
_COND = _FIX / "conditions"
_ST = _FIX / "status_truth"


def _load(name: str) -> str:
    """Load a condition-corpus .txt fixture byte-exact (no stripping)."""
    return (_COND / f"{name}.txt").read_text(encoding="utf-8")


def _sidecar(name: str) -> dict:
    return json.loads((_COND / f"{name}.json").read_text(encoding="utf-8"))


def _load_st(provider: str, name: str) -> str:
    return (_ST / provider / f"{name}.txt").read_text(encoding="utf-8")


# ── AC1 (D1, taxonomy) ★ — every §2.1 fixture classifies to its INDEX kind ─────
# (fixture name, provider, expected ConditionKind, expected subtype)
_AC1_CASES = [
    ("codex-capped-1", "codex", ConditionKind.CAPPED, "usage_limit_hard"),
    ("codex-capped-2", "codex", ConditionKind.CAPPED, "usage_limit_hard"),
    ("kiro-cli-capped-1", "kiro_cli", ConditionKind.CAPPED, "monthly_usage_limit"),
    ("grok-cli-capped-1", "grok_cli", ConditionKind.CAPPED, "weekly_limit_choice"),
    ("cline_cli-CAPPED-1", "cline_cli", ConditionKind.CAPPED, "usage_limit_monthly"),
    ("codex-auth-expired-1", "codex", ConditionKind.AUTH_EXPIRED, "token_refresh_failed"),
    (
        "claude-code-auth-expired-1",
        "claude_code",
        ConditionKind.AUTH_EXPIRED,
        "oauth_expired",
    ),
    (
        "codex-context-exhausted-1",
        "codex",
        # footer 77% left is HEALTHY → NOT CONTEXT_EXHAUSTED; the same pane has a
        # BUSY working-marker (precedence 7). See AC-note in blueprint §2.1.
        ConditionKind.BUSY,
        "working_marker",
    ),
    (
        "kiro-cli-context-exhausted-1",
        "kiro_cli",
        ConditionKind.CONTEXT_EXHAUSTED,
        "low_context_tip",
    ),
    ("codex-dialog-blocked-1", "codex", ConditionKind.DIALOG_BLOCKED, "trust_dir_dialog"),
    (
        "grok-cli-dialog-blocked-1",
        "grok_cli",
        ConditionKind.DIALOG_BLOCKED,
        "trust_dir_dialog",
    ),
    (
        "claude-code-dialog-blocked-1",
        "claude_code",
        ConditionKind.DIALOG_BLOCKED,
        "login_wizard",
    ),
    ("codex-busy-1", "codex", ConditionKind.BUSY, "working_marker"),
    ("kiro-cli-busy-1", "kiro_cli", ConditionKind.BUSY, "thinking_spinner"),
    ("claude-code-busy-1", "claude_code", ConditionKind.BUSY, "asterisk_spinner"),
    (
        "kiro_cli-TRANSIENT_OVERLOAD-1",
        "kiro_cli",
        ConditionKind.TRANSIENT_OVERLOAD,
        "model_high_traffic",
    ),
    ("cline-cli-proc-exited-1", "cline_cli", ConditionKind.PROC_EXITED, "command_exit_code"),
]


@pytest.mark.parametrize("name,provider,kind,subtype", _AC1_CASES)
def test_ac1_taxonomy(name: str, provider: str, kind: ConditionKind, subtype: str) -> None:
    cond = classify_condition(_load(name), provider)
    assert cond is not None, f"{name}: expected a condition, got None"
    assert cond.kind is kind, f"{name}: kind {cond.kind} != {kind}"
    assert cond.subtype == subtype, f"{name}: subtype {cond.subtype!r} != {subtype!r}"


def test_ac1_status_truth_fixtures() -> None:
    """AC1 also covers the in-tree status_truth captures the blueprint names."""
    # grok BUSY (working-1), cline BUSY (working-1), codex TRANSIENT (error-2),
    # kiro NET (error-3), kiro CAPPED (error-5).
    grok = classify_condition(_load_st("grok_cli", "working-1"), "grok_cli")
    assert grok is not None and grok.kind is ConditionKind.BUSY
    cline = classify_condition(_load_st("cline_cli", "working-1"), "cline_cli")
    assert cline is not None and cline.kind is ConditionKind.BUSY
    codex = classify_condition(_load_st("codex", "error-2"), "codex")
    assert codex is not None and codex.kind is ConditionKind.TRANSIENT_OVERLOAD
    assert codex.subtype == "model_at_capacity"
    kiro_net = classify_condition(_load_st("kiro_cli", "error-3"), "kiro_cli")
    assert kiro_net is not None and kiro_net.kind is ConditionKind.NET_INTERRUPTED
    kiro_cap = classify_condition(_load_st("kiro_cli", "error-5"), "kiro_cli")
    assert kiro_cap is not None and kiro_cap.kind is ConditionKind.CAPPED


def test_ac1_reset_hint_present_when_in_fixture() -> None:
    """codex-capped-2 carries a reset hint 'try again at 4:39 AM' (INDEX)."""
    cond = classify_condition(_load("codex-capped-2"), "codex")
    assert cond is not None and cond.reset_hint == "try again at 4:39 AM"
    # codex-capped-1 has no reset hint ("Try again later").
    cond1 = classify_condition(_load("codex-capped-1"), "codex")
    assert cond1 is not None and cond1.reset_hint is None


# ── AC2 (D1) ★ — fusion invariance: a condition never changes fuse_status ──────
def test_ac2_fusion_invariance() -> None:
    """Blueprint AC2: replay a CAPPED fixture and prove StatusMonitor.fuse_status
    returns the SAME (status, reason) with and without a condition present. The
    condition lives on a SEPARATE field and never feeds fusion (D1). This arm
    imports and CALLS fuse_status (not just the enum member set)."""
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    sm = StatusMonitor()
    tid = "fae12345"

    # Baseline: fuse a known status with NO condition recorded.
    before = sm.fuse_status(tid, TerminalStatus.IDLE)

    # Replay the CAPPED fixture through the classifier and record its condition
    # on the SEPARATE field (exactly what the runtime seam does).
    cond = classify_condition(_load("codex-capped-1"), "codex")
    assert cond is not None and cond.kind is ConditionKind.CAPPED
    sm._condition_fleet_sink(tid, "CAPPED")
    assert sm.get_condition(tid) == "CAPPED"

    # After a condition is present, fuse_status returns the IDENTICAL tuple.
    after = sm.fuse_status(tid, TerminalStatus.IDLE)
    assert after == before, f"fusion changed by a condition: {before} -> {after}"

    # And the same holds for a CAPPED-fixture terminal fused as COMPLETED.
    b2 = sm.fuse_status(tid, TerminalStatus.COMPLETED)
    sm._condition_fleet_sink(tid, "CAPPED")
    a2 = sm.fuse_status(tid, TerminalStatus.COMPLETED)
    assert a2 == b2

    # CAPPED is NOT a TerminalStatus member (the D1 mutant would add it here).
    assert "CAPPED" not in TerminalStatus.__members__
    assert {m.value for m in TerminalStatus} == {
        "unknown",
        "idle",
        "processing",
        "completed",
        "waiting_user_answer",
        "render_uncertain",
        "error",
    }


# ── AC3 (D5) ★ — BUSY, never CAPPED, under transcript scrollback ───────────────
def test_ac3_busy_guard() -> None:
    codex = classify_condition(_load("codex-busy-1"), "codex")
    assert codex is not None and codex.kind is ConditionKind.BUSY
    kiro = classify_condition(_load("kiro-cli-busy-1"), "kiro_cli")
    assert kiro is not None and kiro.kind is ConditionKind.BUSY


def test_ac3_cap_outranks_busy() -> None:
    """D5 precedence witness: a pane holding BOTH a cap banner AND a live busy
    marker classifies CAPPED (rank 4), never BUSY (rank 7). This is the arm the
    D5 mutant (rank BUSY above CAPPED) turns red — the pure-busy fixtures above
    cannot catch a precedence swap because nothing else matches them."""
    cap = "■ You've hit your usage limit. Try again later."
    busy = "• Working (28s • esc to interrupt)"
    pane = cap + "\n\n" + busy
    cond = classify_condition(pane, "codex")
    assert (
        cond is not None and cond.kind is ConditionKind.CAPPED
    ), f"cap must outrank busy, got {cond}"
    # And with the cap removed the same pane is BUSY (control).
    busy_only = classify_condition(busy, "codex")
    assert busy_only is not None and busy_only.kind is ConditionKind.BUSY


# ── AC4 (precedence) — cap banner + context footer → CAPPED (4 > 5) ────────────
def test_ac4_precedence_cap_over_context() -> None:
    # Compose a capped banner (codex-capped-2 has a low context footer already:
    # "Context 80% left") — a synthetic pane holding BOTH must classify CAPPED.
    capped = _load("codex-capped-2")
    ctx_footer = "  ~/x · gpt-5.6-sol high · Context 12% left · 5h 0% left"
    pane = capped + "\n" + ctx_footer
    cond = classify_condition(pane, "codex")
    assert cond is not None and cond.kind is ConditionKind.CAPPED


# ── AC5 (D2, quoted-text guard) ★ — one CAPPED from the banner, not the › block ─
def test_ac5_quoted_text_guard() -> None:
    pane = _load("codex-capped-1")
    brows = banner_rows(pane)
    # The suppressed › supervisor-message region must NOT appear in banner rows.
    joined = "\n".join(brows)
    assert "Fixture ruling for AC1" not in joined
    assert "collaboration.send_message" not in joined
    # The real cap banner IS present.
    assert any("You've hit your usage limit" in r for r in brows)
    # Exactly ONE CAPPED condition (the classifier returns a single winner).
    cond = classify_condition(pane, "codex")
    assert cond is not None and cond.kind is ConditionKind.CAPPED
    assert cond.evidence.startswith("■") or "You've hit your usage limit" in cond.evidence
    # D4 de-dup: replaying the two identical banners produces ONE inbox push.
    delivery = ConditionDelivery()
    r1 = delivery.deliver("aaaaaaaa", cond, epoch=1)
    r2 = delivery.deliver("aaaaaaaa", classify_condition(pane, "codex"), epoch=1)
    assert r1.inbox_pushes == 1 and r2.inbox_pushes == 0


def test_ac5_mutant_raw_tail_would_leak_quoted_region() -> None:
    """MUTANT witness: scanning the RAW tail (no banner-only D2 guard) keeps the
    › supervisor region, proving the guard is load-bearing."""
    pane = _load("codex-capped-1")
    raw_rows = pane.splitlines()
    brows = banner_rows(pane)
    assert any("Fixture ruling for AC1" in r for r in raw_rows)
    assert not any("Fixture ruling for AC1" in r for r in brows)


# ── AC6 (D2, reset ≠ cap) — resets-available NOTICE is NOT CAPPED ──────────────
def test_ac6_reset_notice_is_not_cap() -> None:
    notice = "• You have 3 usage limit resets available. Run /usage to use one."
    cond = classify_condition(notice, "codex")
    # The resets-available bullet is not a cap (and not a hard-limit line).
    assert cond is None or cond.kind is not ConditionKind.CAPPED
    # The real hard-limit line IS a cap.
    hard = classify_condition("■ You've hit your usage limit. Try again later.", "codex")
    assert hard is not None and hard.kind is ConditionKind.CAPPED


# ── AC7 (D3, confidence gate) — proc-state-only is low; text exit-code delivers ─
def test_ac7_confidence_gate() -> None:
    # PROC_EXITED inferred from pane_current_command == shell_baseline, no
    # exit-code line → confidence=low → NO delivery.
    inferred = classify_condition("", "cline_cli", proc_exited=True)
    assert inferred is not None and inferred.kind is ConditionKind.PROC_EXITED
    assert inferred.confidence is Confidence.LOW
    assert should_deliver(inferred) is False
    delivery = ConditionDelivery()
    assert delivery.deliver("bbbbbbbb", inferred, epoch=1).delivered is False

    # The negative idle control (DISPATCHER_IDLE_CMD "cat") is NOT process death:
    # the caller passes proc_exited=False, so no PROC_EXITED is raised.
    idle = classify_condition("", "cline_cli", proc_exited=False)
    assert idle is None

    # cline-cli-proc-exited-1 HAS the exit-code line → medium/high, delivers.
    real = classify_condition(_load("cline-cli-proc-exited-1"), "cline_cli")
    assert real is not None and real.kind is ConditionKind.PROC_EXITED
    assert real.confidence in (Confidence.HIGH, Confidence.MEDIUM)
    assert should_deliver(real) is True


# ── AC8 (D4, one-event de-dup) ★ — same pane twice in one epoch → one push ─────
def test_ac8_one_event_dedup() -> None:
    pane = _load("codex-capped-1")
    delivery = ConditionDelivery()
    first = delivery.deliver("cccccccc", classify_condition(pane, "codex"), epoch=7)
    second = delivery.deliver("cccccccc", classify_condition(pane, "codex"), epoch=7)
    assert first.delivered is True and first.inbox_pushes == 1
    assert second.delivered is False and second.inbox_pushes == 0
    assert first.fleet_field == "CAPPED"
    # A NEW epoch (dispatch) re-arms.
    third = delivery.deliver("cccccccc", classify_condition(pane, "codex"), epoch=8)
    assert third.delivered is True and third.inbox_pushes == 1


# ── AC9 (D6, M36 credential plane) ★ — box cap is advisory, never rebinds ──────
def test_ac9_credential_plane_advisory() -> None:
    pane = _load("cline_cli-CAPPED-1")
    cond = classify_condition(pane, "cline_cli", host="grok-box-006", credential_plane="box")
    assert cond is not None and cond.kind is ConditionKind.CAPPED
    assert cond.host == "grok-box-006"
    assert cond.credential_plane == "box"
    assert cond.scope == "credential_plane"
    # Policy: advisory only — does NOT rebind the laptop position.
    assert policy_for_condition(cond, position="dev") is PolicyAction.ADVISORY_ONLY


def test_ac9_mutant_no_scope_would_rebind() -> None:
    """MUTANT witness: a laptop-plane cap (no credential_plane) is a rebind
    candidate (FALLBACK_KIRO), so the scope field is what makes the box cap
    advisory — dropping it flips the policy."""
    pane = _load("cline_cli-CAPPED-1")
    laptop = classify_condition(pane, "cline_cli")  # no host/credential_plane
    assert laptop is not None and laptop.scope == "provider"
    assert policy_for_condition(laptop, position="dev") is PolicyAction.FALLBACK_KIRO


# ── AC10 (D7) — routing table with a capped-today provider still LOADS ─────────
def test_ac10_capped_is_not_a_routing_refusal(tmp_path: Path) -> None:
    """CAPPED is a RUNTIME condition, not a load_routing_table validation error;
    F611 adds no E-PROVIDER-CAPPED (D7). A structurally-valid routing.toml whose
    bound provider is capped-today loads WITHOUT error."""
    from cli_agent_orchestrator.utils.routing import load_routing_table

    toml = tmp_path / "routing.toml"
    toml.write_text(
        '[[binding]]\nposition = "dev"\nkind = "cao"\nprovider = "codex"\n',
        encoding="utf-8",
    )
    table = load_routing_table(toml)  # must not raise
    assert table is not None
    # The F611 policy vocabulary has no routing refusal code for a cap.
    assert not hasattr(PolicyAction, "E_PROVIDER_CAPPED")
    assert "E-PROVIDER-CAPPED" not in {a.value for a in PolicyAction}


# ── AC11 (D8) ★ — AUTH_EXPIRED stops and asks, never silent rebind ────────────
def test_ac11_auth_stops_and_asks() -> None:
    cond = classify_condition(_load("codex-auth-expired-1"), "codex")
    assert cond is not None and cond.kind is ConditionKind.AUTH_EXPIRED
    assert policy_for_condition(cond, position="dev") is PolicyAction.STOP_AND_ASK
    # Even if kiro were available, auth never rebinds (D8) — still STOP.
    assert (
        policy_for_condition(cond, position="dev", kiro_capped=False) is PolicyAction.STOP_AND_ASK
    )


# ── AC12 (policy) — laptop CAPPED → kiro; kiro also capped → stop ──────────────
def test_ac12_capped_kiro_then_stop() -> None:
    cond = classify_condition(_load("codex-capped-1"), "codex")
    assert cond is not None and cond.kind is ConditionKind.CAPPED
    assert policy_for_condition(cond, position="dev") is PolicyAction.FALLBACK_KIRO
    assert policy_for_condition(cond, position="dev", kiro_capped=True) is PolicyAction.STOP_AND_ASK


# ── AC13 (D1, NET_INTERRUPTED) — kiro connection banner at precedence 3.5 ──────
def test_ac13_net_interrupted() -> None:
    cond = classify_condition(_load_st("kiro_cli", "error-3"), "kiro_cli")
    assert cond is not None
    assert cond.kind is ConditionKind.NET_INTERRUPTED
    assert cond.subtype == "connection_interrupted"


# ── AC14 (D5, BUSY grok/cline) — spinner/tool-churn, never CAPPED ──────────────
def test_ac14_busy_grok_cline() -> None:
    grok = classify_condition(_load_st("grok_cli", "working-1"), "grok_cli")
    assert grok is not None and grok.kind is ConditionKind.BUSY
    cline = classify_condition(_load_st("cline_cli", "working-1"), "cline_cli")
    assert cline is not None and cline.kind is ConditionKind.BUSY


# ── AC15 (D1/precedence, TRANSIENT_OVERLOAD codex) — not CAPPED ────────────────
def test_ac15_transient_overload_codex() -> None:
    cond = classify_condition(_load_st("codex", "error-2"), "codex")
    assert cond is not None
    assert cond.kind is ConditionKind.TRANSIENT_OVERLOAD
    assert cond.subtype == "model_at_capacity"


# ── Context threshold guard: 'Context 77% left' must NOT fire CONTEXT_EXHAUSTED ─
def test_context_threshold_healthy_footer_is_not_exhausted() -> None:
    cond = classify_condition(_load("codex-context-exhausted-1"), "codex")
    # 77% left is healthy — the pane's working-marker wins (BUSY), not CONTEXT.
    assert cond is not None and cond.kind is not ConditionKind.CONTEXT_EXHAUSTED
    # A genuinely-low footer alone (no busy marker) IS exhausted.
    low = classify_condition("  ~/x · gpt-5.6 high · Context 8% left · 5h 0% left", "codex")
    assert low is not None and low.kind is ConditionKind.CONTEXT_EXHAUSTED


# ── Event render shape (blueprint §3) ──────────────────────────────────────────
def test_event_render_shape() -> None:
    cond = classify_condition(
        _load("cline_cli-CAPPED-1"), "cline_cli", host="grok-box-006", credential_plane="box"
    )
    assert cond is not None
    line = cond.render_event("deadbeef")
    assert line.startswith("[CONDITION] terminal=deadbeef kind=CAPPED provider=cline_cli")
    assert "host=grok-box-006" in line
    assert "credential_plane=box" in line
    assert "confidence=high" in line
