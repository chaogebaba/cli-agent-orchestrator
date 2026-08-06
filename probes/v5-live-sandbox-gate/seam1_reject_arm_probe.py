#!/usr/bin/env python3
"""V5 seam1 route (a): prove external-CLAUDE.md import-reject arm is reached
and the handler CONTINUES past it (merge kept `continue`; upstream `return`
would stop the multi-prompt loop).

Does NOT fight the live Claude dialog trigger. Feeds a synthetic pane capture
sequence into the real ClaudeCodeProvider._handle_startup_prompts under
sandbox classification (shared-auth-read-only → reject_external_imports=True).

=== FINDINGS ===
(filled by run; see sibling findings file / stdout JSON)
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Worktree import path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cli_agent_orchestrator.providers.claude_code import (  # noqa: E402
    ClaudeCodeProvider,
    EXTERNAL_IMPORT_PROMPT_PATTERN,
    TRUST_PROMPT_PATTERN,
)

ART = Path(
    __import__("os").environ.get(
        "CAO_ARTIFACTS_DIR", str(ROOT / "tmp" / "orch")
    )
) / "v5-gate" / "evidence"
ART.mkdir(parents=True, exist_ok=True)

TRUST_PANE = (
    "❯ 1. Yes, I trust this folder\n"
    "  2. No, don't trust this folder\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)
IMPORT_PANE = (
    "Yes, I trust this folder\n"
    "Allow external CLAUDE.md file imports?\n"
    "❯ 1. Yes, allow external imports\n"
    "  2. No, disable external imports\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)
WELCOME_PANE = "Welcome to Claude Code v2.1.223\n❯ \n"


def _sandbox_plane():
    return SimpleNamespace(classification="shared-auth-read-only")


async def run_sequence(label: str, panes: list[str]) -> dict:
    """Drive real handler with synthetic get_history sequence; record key sends."""
    mock_backend = MagicMock()
    # Record every get_history index so we can prove post-reject polls.
    history_calls: list[int] = []
    panes_iter = list(panes)
    call_idx = {"n": 0}

    def get_history(_session, _window):
        i = call_idx["n"]
        call_idx["n"] += 1
        history_calls.append(i)
        if i < len(panes_iter):
            return panes_iter[i]
        # After sequence exhausted, keep returning last (stable welcome)
        return panes_iter[-1] if panes_iter else ""

    mock_backend.get_history.side_effect = get_history
    mock_backend.send_keys = MagicMock()
    mock_backend.send_special_key = MagicMock()

    send_keys_log: list[tuple] = []
    special_log: list[tuple] = []

    def track_keys(*a, **k):
        send_keys_log.append((a, k))
        return None

    def track_special(*a, **k):
        special_log.append((a, k))
        return None

    mock_backend.send_keys.side_effect = track_keys
    mock_backend.send_special_key.side_effect = track_special

    # status_monitor.notify_input_sent is imported inside the handler
    with (
        patch(
            "cli_agent_orchestrator.providers.claude_code.provider_home",
            lambda _p: _sandbox_plane(),
        ),
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=mock_backend,
        ),
        patch(
            "cli_agent_orchestrator.backends.registry._backend",
            mock_backend,
        ),
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor"
        ) as mock_sm,
    ):
        mock_sm.notify_input_sent = MagicMock()
        # Also patch get_backend import site used as get_backend() in module
        with patch(
            "cli_agent_orchestrator.providers.claude_code.get_backend",
            return_value=mock_backend,
        ):
            provider = ClaudeCodeProvider("tid-seam1", "sess", "win0")
            t0 = time.monotonic()
            await provider._handle_startup_prompts(idle_gap=2.0, outer_timeout=15.0)
            elapsed = time.monotonic() - t0

    # Classify actions
    down_arrows = [
        e
        for e in send_keys_log
        if (e[0] and len(e[0]) >= 3 and e[0][2] == "\x1b[B")
        or e[1].get("enter_count") == 0
        and any(a == "\x1b[B" for a in e[0])
    ]
    # More robust: any send_keys with Down arrow payload
    down_arrows = [
        e for e in send_keys_log if any(isinstance(x, str) and x == "\x1b[B" for x in e[0])
    ]
    enters = special_log  # all special keys should be Enter for these arms

    # Find which history call index first saw import prompt
    import_at = None
    for i, pane in enumerate(panes):
        if "Allow external CLAUDE.md file imports?" in pane:
            import_at = i
            break

    # CONTINUE proof: history was polled AFTER the import pane index
    # (if reject arm used `return`, handler would exit without further polls
    # once import was handled — specifically without consuming a later welcome pane).
    polls_after_import = 0
    if import_at is not None:
        # handler may re-read same index region; count history calls with n > import_at
        # After handling import at poll import_at, continue causes next poll import_at+1...
        polls_after_import = sum(1 for n in history_calls if n > import_at)

    reject_reached = len(down_arrows) >= 1 and len(enters) >= 1
    # For trust→import→welcome: expect ≥2 Enter (trust + import reject) and ≥1 Down
    continued = polls_after_import >= 1

    result = {
        "label": label,
        "elapsed_s": round(elapsed, 3),
        "history_calls": history_calls,
        "n_history": len(history_calls),
        "import_pane_index": import_at,
        "polls_after_import": polls_after_import,
        "n_down_arrow": len(down_arrows),
        "n_enter": len(enters),
        "send_keys_log": [list(a) + [k] for a, k in send_keys_log],
        "special_log": [list(a) + [k] for a, k in special_log],
        "reject_arm_reached": reject_reached,
        "continued_past_reject": continued,
        "pass": reject_reached and continued,
    }
    return result


async def main() -> int:
    # Sequence A: trust → import → welcome (canonical multi-prompt order)
    # Proves: trust continue → import reject arm → continue → welcome return
    seq_a = await run_sequence(
        "trust_then_import_then_welcome",
        [TRUST_PANE, IMPORT_PANE, WELCOME_PANE],
    )
    # Sequence B: import first then welcome (import is 3rd arm code path alone)
    seq_b = await run_sequence(
        "import_then_welcome",
        [IMPORT_PANE, WELCOME_PANE],
    )
    # Sequence C: import then trust then welcome — proves continue after reject
    # still reaches trust arm (would be killed if reject used `return`)
    seq_c = await run_sequence(
        "import_then_trust_then_welcome",
        [IMPORT_PANE, TRUST_PANE, WELCOME_PANE],
    )

    # seq_c is the load-bearing one: if reject used return, trust would never
    # get Enter after import was handled.
    seq_c_trust_enter_after_reject = seq_c["n_enter"] >= 2  # reject Enter + trust Enter

    overall = (
        seq_a["pass"]
        and seq_b["pass"]
        and seq_c["pass"]
        and seq_c_trust_enter_after_reject
    )

    report = {
        "seam": "seam1_import_reject_route_a",
        "method": "synthetic_pane_into_real__handle_startup_prompts",
        "classification": "shared-auth-read-only",
        "pattern": EXTERNAL_IMPORT_PROMPT_PATTERN,
        "trust_pattern": TRUST_PROMPT_PATTERN,
        "sequences": [seq_a, seq_b, seq_c],
        "load_bearing": {
            "seq_c_import_then_trust": seq_c_trust_enter_after_reject,
            "meaning": (
                "After import-reject, handler must continue to accept trust "
                "(Enter). Upstream return after reject would leave trust unhandled."
            ),
        },
        "pass": overall,
    }

    out = ART / "seam1-route-a-reject-arm.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    findings = ART.parent / "SEAM1_FINDINGS.md"
    findings.write_text(
        "# === FINDINGS ===\n\n"
        f"seam1 route (a) overall: **{'PASS' if overall else 'FAIL'}**\n\n"
        f"- reject arm reached (Down+Enter): "
        f"A={seq_a['reject_arm_reached']} B={seq_b['reject_arm_reached']} "
        f"C={seq_c['reject_arm_reached']}\n"
        f"- continued past reject (further get_history): "
        f"A={seq_a['continued_past_reject']} B={seq_b['continued_past_reject']} "
        f"C={seq_c['continued_past_reject']}\n"
        f"- load-bearing seq C (import→trust still gets Enter): "
        f"{seq_c_trust_enter_after_reject}\n"
        f"- evidence: `{out}`\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2))
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
