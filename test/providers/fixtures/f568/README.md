# F568 #425 D12d fixtures — claude_code spinner busy-marker veto

Byte-exact **plain** pane snapshots (never synthesised) for the D12d
spinner-veto tests. Captured LIVE on this laptop from real Claude Code seats
with `tmux capture-pane -p -S -45 -t <pane>` (the same escape-free view the
liveness sampler holds via `get_history(..., strip_escapes=True)`).

- **Capture time:** 2026-08-29, ~04:27–04:40 UTC
- **Claude Code version:** 2.1.251 (Claude Code)
- **Seats:** `cao-claude-orch5:chao_supervisor-b93613da`,
  `cao-claude-orch1:chao_supervisor-4a8f3b42` (captured read-only)

## Files and the `new_tui_box_spinner_live()` verdict each exercises

| File | Helper verdict | What it captures |
|---|---|---|
| `spinner-cascading.txt` | `True` | `✻ Cascading… (…)` spinner live above the composer box (tuple present) |
| `spinner-roosting.txt` | `True` | `Roosting…` spinner verb, distinct gerund |
| `spinner-ebbing-bare.txt` | `True` | bare `✶ Ebbing…` — spinner glyph + gerund, **no** `(elapsed · ↓ tokens)` tuple |
| `supervisor-pane-working-033435.txt` | `True` | `· Concocting… (…)` spinner sitting five rows above the top rail behind a `⎿` hint AND a `›` teammate-push row — the window-6 + `›`-skip positive. The historical 4-row/no-`›` walk MISSED this (a latent false-COMPLETED in `get_status` the same helper fixes). |
| `idle-subagent-churn-a.txt` | `False` | seat idle at an EMPTY composer box, turn ended (`✻ Crunched … done`), a background Agent still running (`1 monitor still running` / `← 1 agent`); NO spinner above the box top rail |
| `idle-subagent-churn-b.txt` | `False` | same seat, a later churn frame (content above the composer changed — proving live churn — while the composer stayed empty and spinner-free) |

Distinct spinner gerunds across the positive set: **Cascading, Roosting,
Ebbing (bare), Concocting** — ≥3 as required, one of them bare.

The `idle-subagent-churn-*` pair is the AC-F568-6 negative fixture: replayed as
a churning pane with `children_count == 0`, every eligible sample must yield
`(IDLE, "pane_delta_vetoed")` — never `pane_delta`/`pane_delta_expired`.

## Not in this directory (reused, asserted by the D12d tests)

- `../wpq1_claude_2_1_211/completed-composer.txt` → `False`
- `../wpq1_claude_2_1_211/initial-empty-composer.txt` → `False`
- codex / kiro / grok fixtures under `../` → `None` (no claude box; base-provider
  `rule3a_busy_marker` returns `None`, so their rule-3a path is byte-identical)
