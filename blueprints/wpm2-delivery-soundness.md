# WPM2 — delivery soundness for busy receivers + alarm hygiene + seed stderr parse

Status: DRAFT r1 (2026-07-13). Micro-WP, three independent slices sharing one gate
train. Builds directly on WPM1 (`8afb758`, FROZEN r9 law) and WP2S3 (`7651dc1`).
Origin: two live post-activation findings from the 2026-07-13 drains, both
evidence-pinned:

- **Incident-1858** (`tmp/orch/wpm2-incident-1858.md`, outer repo): a worker
  callback to the busy long-turn claude_code supervisor was injected 3×, every
  confirmation window (~13s) expired `transcript_absent`, and the message settled
  `delivery_failed` — on a message that was delivered three times. WPM1 drain was
  6/6 PASS the same hour (`tmp/orch/drain-wpm1.md`); its probes never held a
  receiver busy past the confirmation window, so this class was invisible to the
  gate and to the drain.
- **WP2S3 drain C1 FAIL** (`tmp/orch/drain-wp2s3.md`): seed-and-resume bootstrap
  engages but fails `seed_uuid_unparseable` — codex launched with profile
  `--model`/`-c` overrides emits `session id:` on **stderr**; the seed capture
  parses stdout only. Plain codex creates remain broken.
- **Alarm spam**: the "[watchdog] worker idle 120s without callback" notice
  re-fired 3× in ~6 min for a worker that was provably mid-task (long codex-exec
  subprocesses) every time it fired. No episode dedup, and idle detection called
  a busy pane idle.

## S1 — busy-receiver loss-proof soundness (claude_code)

**Law (extends WPM1 FROZEN r9; narrows, does not alter, the proof-only
settlement law):** a `confirmation_timeout` may be counted as a PROVEN boundary
loss ONLY if the receiver has been observed at a turn boundary (idle) at least
once strictly AFTER the injection completed. Absence of the payload from the
transcript while the receiver has not idled since injection is **no evidence
either way** — the attempt stays open/ambiguous-pending, and NO re-injection may
occur in that state.

Rationale: claude_code queues mid-turn injections in the harness and writes them
to the transcript only at turn boundaries. `transcript_absent` before a
post-injection idle observation is the expected state of a SUCCESSFUL delivery
to a busy receiver (incident-1858 evidence JSON: transcript growing, same inode,
payload absent, all three copies later surfaced).

Design pins (builder-facing; gate lanes to tighten):
- The wake/retry scheduler gates on "receiver idle observed since injection",
  not wall clock. Existing substrate: `pre_input_gen` / `pre_status_gen` /
  `settled_status_gen` on `inbox_delivery_attempt` + terminal status
  transitions. Pin the exact predicate at gate time.
- On the first post-injection idle observation: run D2 late-confirmation FIRST
  (transcript lookup). Hit → confirmed/DELIVERED (WPM1 D2 path unchanged).
  Miss AT an observed post-injection boundary → that, and only that, is one
  proven boundary loss; WPM1's proof-ordered cap then applies unchanged.
- Liveness ("what advances the clock"): the clock is the receiver's own idle
  transition. A receiver that never idles keeps the message pending-ambiguous
  forever — that is correct (D9 retention + watchdog notices cover longevity);
  it must NEVER convert to delivery_failed on wall-clock alone.
- Non-claude providers: unchanged unless the same queued-injection semantics
  are proven for them; scope S1 to claude_code receiver paths.
- Backfill/repair: no retroactive rewriting of historical rows. Optional
  (gate to decide): a one-shot corrective notice for post-WPM1
  `delivery_failed` rows whose payload later appeared in the transcript is OUT
  of scope for S1 unless a lane shows it's cheap and safe.

Evidence bar (minimum; tests derive from THIS text):
- Busy-hold test: receiver held busy across ≥3 confirmation windows after one
  real injection → exactly 1 injection, 0 loss proofs, 0 delivery_failed; then
  receiver idles, transcript write appears, D2 late-confirm → DELIVERED,
  attempt chain length 1. (Direct incident-1858 inversion.)
- True-loss test: receiver idles post-injection, payload genuinely absent at
  that boundary → exactly one proven loss recorded; WPM1 cap algebra proceeds
  unchanged.
- Never-idle test: receiver never idles within the horizon → message still
  pending, no delivery_failed, watchdog/D8 notice behavior per WPM1.
- Wiring-level test through the real scheduler/service path (WPM1 D4 lesson:
  wiring-removal mutant must die).

## S2 — assignment-watchdog alarm hygiene

**Law:** the "worker idle without callback" alarm fires at most once per
(worker terminal, assignment episode, idle episode). Re-fire requires a state
change: the worker was observed active after the alarm and then idled again, or
an explicit escalation threshold is crossed. Additionally, a worker whose
provider status/pane shows an in-flight operation (running subprocess, TUI
busy/spinner state per provider idle-detection) is NOT idle for this alarm.

Evidence bar:
- Repeat-suppression test: continuous idle-looking state across 3 poll windows
  → exactly 1 alarm.
- State-change test: idle→active→idle → second alarm allowed.
- Busy-suppression test: provider status busy/in-flight → 0 alarms regardless
  of duration.
- Builder must first LOCATE the emitting subsystem and record it in the report
  (the alarm's origin was not identified during the incident); if idle
  detection routes through provider status already fixed by pyte F0–F4, say so
  with evidence rather than re-fixing.

## S3 — WP2S3 C1: seed UUID capture reads stderr

**Law:** seed-and-resume bootstrap (`seed_resume_identity`) parses the codex
session id from the merged stdout+stderr of the seed invocation (or runs the
seed with stderr merged into stdout). Parse strictness otherwise unchanged.

Evidence bar:
- Unit fixture: session id emitted on stderr only (profile `--model`/`-c`
  shape, per drain C1 raw captures in `tmp/orch/drain-wp2s3/c1/`) → captured.
- Stdout-only fixture (legacy shape) → still captured (no regression).
- Both-streams / garbage-interleaved fixture → exactly one id captured;
  ambiguity (two DIFFERENT ids) fail-closes with the existing
  `seed_uuid_unparseable`-class error, never guesses.
- On activation, drain re-runs WP2S3 C1 (plain codex create end-to-end);
  memory `codex-plain-spawn-broken` retires only on that PASS.

## Out of scope

- Any change to WPM1's cap algebra, D5–D9 arms, or evidence fence.
- Provider-generic rework of confirmation for non-claude providers.
- MSGTRACE RESIDUAL-2 (compacted-session no-oracle carve-out) stays as-is;
  S1 operates on idle-observation + existing transcript lookup, and must not
  widen the oracle's authority.
- Upstream v2.3.0 merge content (`7148c58`) — rides the same next activation
  train but is not gated here beyond the standard full suite.

## Gate plan

Dual-lane as standard: codex empirical MAIN (fork_from=wpm1rev — it holds full
WPM1 r1–r3 context), grok structural double-check. Blueprint gate → freeze →
build (codex_dev fork_from=codex) → diff gate. Evidence-only rounds hash-pinned
per GOLDEN-TIPS 2026-07-13. Full suite + focused WPM1/watchdog/seed files.
