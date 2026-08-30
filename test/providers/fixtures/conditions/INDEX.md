# Condition Corpus — real provider-screen fixtures (F611 #467)

Harvested from `~/.aws/cli-agent-orchestrator/logs/terminal/*.scrollback` (byte-exact
excerpts; PII redacted: emails/tokens/home paths → `<REDACTED*>`).
One `.txt` (screen/log excerpt) + one `.json` (provenance: provider, condition, subtype,
reset_hint_raw, source_path, source_line_range, captured_at, sha256) per fixture.
`captured_at` = scrollback file mtime (UTC) — the capture date of the terminal session.

Condition taxonomy: CAPPED (usage/rate limit, with reset hint when present),
AUTH_EXPIRED, NET_INTERRUPTED, CONTEXT_EXHAUSTED, DIALOG_BLOCKED, PROC_EXITED, BUSY.

## Index

| Fixture | Provider | Condition | Subtype | Reset hint | Source | sha256 (12) |
|---|---|---|---|---|---|---|
| codex-capped-1 | codex | CAPPED | usage_limit_hard | (none — "Try again later") | 16073a9e.scrollback:552-569 | 94d1a3a97946 |
| codex-capped-2 | codex | CAPPED | usage_limit_hard | try again at 4:39 AM | 7403413e.scrollback:66-80 | 6cccd0891eaf |
| codex-capped-3 | codex | CAPPED | five_hour_warning_then_hard | 4:39 AM; /status | cc6889e7.scrollback:82-93 | 0ac4684fd6d4 |
| codex-capped-4 | codex | CAPPED | usage_limit_hard | (none) | a2a97308.scrollback:151-158 | 6cb2f7c62dd8 |
| codex-capped-5 | codex | CAPPED | usage_limit_hard | (none) | f3381b38.scrollback:178-188 | a216f8011f95 |
| codex-capped-6 | codex | CAPPED | usage_limit_hard | (none) | 6c7e6621.scrollback:144-151 | 06232a917f7b |
| codex-capped-7 | codex | CAPPED | five_hour_warning_then_hard | 4:39 AM; /status | 9fc2f466.scrollback:79-92 | 26b048e5b599 |
| kiro-cli-capped-1 | kiro_cli | CAPPED | monthly_usage_limit | return next month | 08d5f3d2.scrollback:9021-9032 | d37cbcefb791 |
| kiro-cli-capped-2 | kiro_cli | CAPPED | monthly_usage_limit | return next month | 22f3e4f8.scrollback:40-50 | f01214ec40f3 |
| kiro-cli-capped-3 | kiro_cli | CAPPED | monthly_usage_limit | return next month | 4fd45dca.scrollback:8383-8394 | 2eb0b15f2434 |
| kiro-cli-capped-4 | kiro_cli | CAPPED | monthly_usage_limit | return next month | d31db3d4.scrollback:7599-7603 | d339e28c53ab |
| kiro-cli-capped-5 | kiro_cli | CAPPED | monthly_usage_limit | return next month | f1d255f4.scrollback:8496-8501 | a2fc2ce7f89c |
| grok-cli-capped-1 | grok_cli | CAPPED | weekly_limit_choice | "Try Again … once you have usage again" | fcbb00dc.scrollback:89-96 | 171314f85772 |
| codex-auth-expired-1 | codex | AUTH_EXPIRED | token_refresh_failed | sign in again | b343aee0.scrollback:203-221 | fb91dbb2e5b9 |
| codex-auth-expired-2 | codex | AUTH_EXPIRED | token_refresh_failed | sign in again | e734bd7c.scrollback:233-236 | c4ac4c1cf123 |
| claude-code-auth-expired-1 | claude_code | AUTH_EXPIRED | oauth_expired | — | d0c39c30.scrollback:1460-1466 | 3bb056fa1b3b |
| claude-code-auth-expired-2 | claude_code | AUTH_EXPIRED | login_wizard | — | e3441b17.scrollback:29-37 | a00013d206a4 |
| kiro-cli-context-exhausted-1 | kiro_cli | CONTEXT_EXHAUSTED | low_context_tip | /compact | a6da8e75.scrollback:13-20 | 6baa0d0a5b76 |
| kiro-cli-context-exhausted-2 | kiro_cli | CONTEXT_EXHAUSTED | low_context_tip | /compact | d950d891.scrollback:13-20 | 6baa0d0a5b76 (identical banner text) |
| codex-context-exhausted-1 | codex | CONTEXT_EXHAUSTED | footer_percent_status | — | 16073a9e.scrollback:1396-1403 | 08c21768f32b |
| codex-dialog-blocked-1 | codex | DIALOG_BLOCKED | trust_dir_dialog | — | 15a6fa21.scrollback:16-33 | 8332f9d8116e |
| grok-cli-dialog-blocked-1 | grok_cli | DIALOG_BLOCKED | trust_dir_dialog | Enter/y trust · n/Esc quit | fcbb00dc.scrollback:51-60 | f3479955a120 |
| claude-code-dialog-blocked-1 | claude_code | DIALOG_BLOCKED | login_wizard | — | e3441b17.scrollback:31-37 | 822be4c2ff78 |
| codex-busy-1 | codex | BUSY | working_marker | — | 16073a9e.scrollback:1396-1403 | 08c21768f32b |
| codex-busy-2 | codex | BUSY | working_marker | — | c24be4dc.scrollback:533-543 | c631bce2be98 |
| kiro-cli-busy-1 | kiro_cli | BUSY | thinking_spinner | — | 28b50b67.scrollback:531-539 | d33d0b9d0467 |
| claude-code-busy-1 | claude_code | BUSY | asterisk_spinner | — | 82b743ab.scrollback:19-27 | 6b29b25c3549 |
| cline-cli-proc-exited-1 | cline_cli | PROC_EXITED | command_exit_code | — | 0b970e6c.scrollback:72-75 | baedd3768dbc |

Notes:
- codex-context-exhausted-1 and codex-busy-1 are the same source excerpt (footer with
  "Context 77% left" + "• Working (28s • esc to interrupt)") — evidences both conditions.
- kiro-cli-context-exhausted-1/-2 are byte-identical banner texts from two separate
  sessions (kiro welcome tip "Running low on context? Type /compact").
- claude-code-auth-expired-1 was relayed through a grok worker pane via box-run, but the
  screen text is verbatim `claude -p` output: "Failed to authenticate: OAuth session
  expired and could not be refreshed".


## Classifier precedent checked (read-only)

- `cli-agent-orchestrator/src/**/providers/codex.py`: SYSTEM_NOTICE_PATTERN (~:153),
  TRANSIENT_ERROR_EXCLUSIONS (~:196), quota_or_auth (~:2701).
- kiro_cli.py: TUI_PROCESSING_PATTERN "Kiro is working|Thinking..." (~:105).
- claude_code.py: spinner "✽ Cooking… (6s · ↓ N tokens · thinking)" (~:187);
  PROCESSING_PATTERN `[✶✢✽✻✳·*].*…`.

## GAPS — condition × provider cells with NO real sample found

| | codex | kiro_cli | grok_cli | cline_cli | claude_code |
|---|---|---|---|---|---|
| CAPPED | ✅ (7) | ✅ (5) | ✅ (1) | ❌ GAP | ❌ GAP |
| AUTH_EXPIRED | ✅ (2) | ❌ GAP | ❌ GAP | ❌ GAP | ✅ (2) |
| NET_INTERRUPTED | ❌ GAP | ❌ GAP | ❌ GAP | ❌ GAP | ❌ GAP |
| CONTEXT_EXHAUSTED | ✅ (1, footer % only) | ✅ (2, tip only) | ❌ GAP | ❌ GAP | ❌ GAP |
| DIALOG_BLOCKED | ✅ (1) | ❌ GAP* | ✅ (1) | ❌ GAP | ✅ (1) |
| PROC_EXITED | ❌ GAP | ❌ GAP | ❌ GAP | ✅ (1) | ❌ GAP |
| BUSY | ✅ (2 — "• Working (… esc to interrupt)"; glyph-spin variants still a gap) | ✅ (1 — "⠹ Thinking... (esc to cancel)") | ❌ GAP | ❌ GAP* | ✅ (1 — ✻) |

\* kiro DIALOG_BLOCKED: no live kiro dialog screen in scrollbacks — only classifier
source lines and supervisor prose describing dialogs (e.g. "Yes, I accept" text at
d31db3d4:6130 is source code, not a screen). cline BUSY: "• Working (2s • esc to
interrupt)" appears only inside a test-log reproduction (1e150b28:112), not a live
cline screen.

Gap detail:
- NET_INTERRUPTED (all providers): only command-level net failures found (ssh
  `Connection closed` ef3a0f25:310; `Command timed out` rows) or supervisor prose —
  no provider-TUI connection-error screen in any captured scrollback. journalctl
  cao-server and cao_*.log greps for stream/ECONNRESET/overloaded/quota-401: no hits.
- codex BUSY glyph-spinner variants ("Boogieing/Ebbing" gerund frames) referenced in
  4fd45dca prose but no glyph-spinner screen captured.
- PROC_EXITED (codex/kiro/grok/claude): process death kills the pane; scrollbacks end
  at the shell prompt (e.g. e734bd7c ^C% row) — no in-TUI proc-exited screen exists.
