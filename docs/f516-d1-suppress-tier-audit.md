# F516 D1 — Suppress-tier audit (dialogs that never render can't be missed)

Per-provider spawn-config audit of the levers that make a blocking dialog
*never render* (so the responder never has to answer it). Blueprint D1 SCOPE
(r3-S6): **adoption in this train is codex-only**; for every other provider the
deliverable is **audit-and-table ONLY** — adopted non-codex levers are filed as
follow-on issues, never edited here (their launch-arg sites live in provider
modules outside the §6 wall). Human-gated dialogs (`answer: wait` rules) are
NOT suppressed.

## Table — dialog → lever → adopted

| Provider | Dialog / prompt | Suppression lever | Adopted here? | Where |
|----------|-----------------|-------------------|---------------|-------|
| codex | "Do you trust the contents of this directory?" (workspace trust) | `--yolo` = `--dangerously-bypass-approvals-and-sandbox` (implies `approval_policy="never"` + trusted workspace) | **YES** (already at launch) | `providers/codex.py` build-command: `command_parts = [codex, "--yolo"]` |
| codex | Command-approval modal ("Would you like to run…") | `approval_policy="never"` (subsumed by `--yolo`) | **YES** (via `--yolo`) | same launch flag |
| codex | Startup update dialog ("Update available!") | `-c check_for_update_on_startup=false` | **YES** (pre-existing) | `providers/codex.py`: `-c check_for_update_on_startup=false` |
| codex | resume-working-directory chooser | (no pre-render lever; classified WAITING + answered by `codex-resume-working-directory` rule — F516 D2) | n/a (answered, not suppressed) | seed rule + D2 classifier |
| kiro | trust-all / interactive prompts | `--no-interactive --trust-all-tools` | audit-only (follow-on) | kiro launch args (out of §6 wall) |
| cline | tool-approval prompts | `-y` | audit-only (follow-on) | cline launch args (out of §6 wall) |
| grok | yolo-mode approvals | grok yolo flag | audit-only (follow-on) | grok launch args (out of §6 wall) |
| claude | permission prompts | permission-mode (`--dangerously-skip-permissions` / accept-edits) | audit-only (follow-on) | claude launch args (out of §6 wall) |

## Codex adoption note (the only adoption in this train)

Codex is launched with `--yolo` (alias for
`--dangerously-bypass-approvals-and-sandbox`) by default in
`CodexProvider`'s build-command path — see the `command_parts = [codex,
"--yolo"]` branch. That single flag is the adopted D1 lever: it sets
`approval_policy="never"` and treats the workspace as trusted, so the workspace-
trust prompt and the command-approval modal **never render** for a CAO-launched
codex worker. Profiles may opt out via `codexProfile` (a named
`[profiles.<name>]` block), unless unrestricted allowed-tools (`*`) force yolo.
The startup update dialog is separately suppressed at the source with
`-c check_for_update_on_startup=false`.

No *new* config override is added on top of `--yolo` here: an explicit
`-c approval_policy=never` would be redundant with, and could conflict with,
the flag codex already derives that setting from. The adopted lever is the
existing launch flag; this commit's code change is the audit doc plus the D2
resume-cwd classifier (commit 3) that lets the one non-suppressible codex
chooser be *answered* rather than suppressed.

## AC8 (live-spawn proof) — LIVE-ONLY

AC8 requires every adopted suppression to be proven by a live spawn with the
dialog absent. That is a live-fleet check (a real codex worker spawn observing
zero trust/approval dialog), recorded in the run ledger — it is not reproducible
in the offline unit suite. See the build report's AC status table; AC8 is marked
LIVE-ONLY alongside AC12.

## Follow-on issues (non-codex adoption, filed not edited)

- kiro: adopt `--no-interactive --trust-all-tools` in the kiro launch path.
- cline: adopt `-y` in the cline launch path.
- grok: adopt the yolo flag in the grok launch path.
- claude: adopt an appropriate permission-mode in the claude launch path.

Each is a launch-arg edit in a provider module outside the F516 §6 wall and is
deliberately deferred to its own change.
