# status_truth — byte-exact pane-capture fixture corpus

Corpus for the status-truth hardening WP. Every `<state>-<n>.txt` is a
byte-exact pane capture (never synthesised, never edited); each has a
sidecar `<state>-<n>.json` with `{provider, state, source, captured_at,
notes}`. The `state` is what the corpus builder **observed in the pane
bytes** at collection time — no guessing; genuinely ambiguous panes are
filed under `unknown-*.txt` with the ambiguity documented in `notes`.

States: `idle`, `working`, `delegating` (subagent/child running while seat
idle), `waiting_user_answer` (dialog), `error`. Providers: `codex`,
`kiro_cli`, `claude_code`, `cline_cli`, `grok_cli`.

## Coverage matrix (provider × state)

| provider      | idle | working | delegating | waiting_user_answer | error | unknown |
|---------------|:----:|:-------:|:----------:|:-------------------:|:-----:|:-------:|
| codex         | 4 ✅ | 1 ✅    | 0 ❌       | 3 ✅                | 3 ✅  | 1       |
| kiro_cli      | 2 ✅ | 2 ✅    | 0 ❌*      | 2 ✅                | 5 ✅  | 1       |
| claude_code   | 4 ✅ | 5 ✅    | 2 ✅       | 2 ✅                | 0 ❌  | 0       |
| cline_cli     | 1 ✅ | 2 ✅    | 0 ❌       | 0 ❌                | 0 ❌  | 0       |
| grok_cli      | 3 ✅ | 1 ✅    | 0 ❌       | 3 ✅                | 1 ✅  | 0       |

**21/25 cells filled.** Missing cells (no real capture exists as of
2026-08-29):

- `codex/delegating`, `grok_cli/delegating`, `cline_cli/delegating` — no
  pane with a child running under an idle seat was ever captured for these
  providers.
- `claude_code/error` — no Claude Code error pane exists in any source.
- `cline_cli/waiting_user_answer`, `cline_cli/error` — no Cline dialog or
  error pane existed in any source or live seat at capture time.
- `kiro_cli/delegating` (*) — no child-running pane exists;
  `kiro_cli/unknown-1.txt` (handoff transcript ending back at the prompt)
  is the closest artefact and is flagged in its sidecar as the
  delegating-cell candidate once the WP fixes the semantics.

`unknown` fixtures are extra (not a target state): panes whose status
could not be honestly classified — `codex/unknown-1` (F435 stuck-paste
wedge) and `kiro_cli/unknown-1` (completed-handoff transcript).

## Sources

1. Pre-existing fixtures under `test/providers/fixtures/` (flat dir,
   `wpq1_claude_2_1_211/`) — copied byte-exact; `captured_at` in sidecars
   is the source file's last git-commit date (original capture date not
   recorded upstream).
2. `f568/` fixtures — originally in worker 4fd45dca's worktree (branch
   `cao/4fd45dca`, tip `801598d7`); the worktree was discarded when that
   worker's terminal was deleted mid-collection, so the files were
   recovered byte-exact via `git show cao/4fd45dca:...`. Live captures
   made 2026-08-29 ~04:27–04:40 UTC per the f568 README.
3. `/data/cao-scratch/*.txt` — reviewed; `f435-recurrence-61ef8f3d-pane.txt`
   duplicates `codex_f435_stuck_paste_pane.txt` (not duplicated here); the
   rest are logs/proofs, not pane captures.
4. `probes/error-pane-samples/` — contains only a proof log
   (`2026-08-09-f26-g7-live-proof.txt`), no pane bytes; nothing usable.
5. LIVE captures: `tmux capture-pane -p -S -100 -t cao-claude-orch5:<w>`
   (capture-only; no keys ever sent) of running CAO worker panes at
   **2026-08-29T05:46:57Z**:
   - `:0` chao_supervisor-b93613da (claude_code) → working-5
   - `:2` kiro_dev-f1d255f4 (kiro_cli) → error-3
   - `:3` secretary-231f1de1 (cline_cli) → idle-1
   - `:4` kiro_dev-4fd45dca (kiro_cli) → error-5
   - `:6` kiro_dev-d31db3d4 (kiro_cli) → error-4
   - `:7` cline_dev-618510c4 (cline_cli) → working-1
   - `:8` cline_dev-91d92890 (cline_cli) → working-2 (self-capture)
   Provider per fleet API at capture time; pane state observed in the
   bytes. `cline_dev-e15bef55` (window 5) vanished mid-capture — not
   collected.

## Status-truth discrepancies observed live (noted in sidecars)

- `claude_code/working-5`: fleet API said `completed` while the pane
  showed a live `✻ Shenaniganing…` spinner with a child-agent row.
- `kiro_cli/error-3/-4`: fleet API said `completed` while the panes showed
  `● Your connection was interrupted` banners over idle composers.
- `kiro_cli/error-5`: fleet API said `idle` while the pane showed
  repeated monthly-usage-limit errors.
- `codex/error-2`, `codex/error-3`: error notices (capacity / content
  refusal) sitting over composers that look idle to a sampler.

## Conventions

- `idle` includes post-turn panes (completed marker + composer/prompt).
- `waiting_user_answer` = any dialog/prompt awaiting user choice
  (permission prompts, pickers, approval modals, update/telemetry/login
  dialogs).
- `error` = pane contains an explicit error/failure notice; where the
  seat simultaneously looks idle (composer visible), that tension is the
  point of the fixture and is spelled out in `notes`.
- No test logic, no provider code, and no pre-existing fixture were
  touched by this corpus; all files here are additive.
