# F597 #454 (+ pt2 / F598 #455) — combined build report (R2)

Worker: kiro_dev `e58bc008`. All claims re-verified on the rebased tip.

- **Branch:** `cao/e58bc008`
- **Base (main):** `b600c1f8` (upstream remerge)
- **Head:** `29d1b27867397e6efcf8e9ea2f1fc6d0d6e4ce3a`
- **Commits (3, all on b600c1f8):**
  - `3f29c722` F597 #454: canonical-form matcher + codex worker-cwd pre-trust
  - `cf2d1888` F597 #454 pt2 / F598 #455: responder delivery — settle, re-arm, submit-verify
  - `29d1b278` F597 #454 R2: two canonical domains (contains=full, regex=light); NFKC + no-match-dedupe mutant tests; read-only shipped-rule corpus
- **Diff scope:** `git diff --name-only b600c1f8..HEAD` → 38 paths, ALL F597/F598 (3 source, 8 python test files, 27 fixture/sample/README). No unrelated files. (Fixes gate B1.)

## R2 disposition of the gate-NO findings

| Finding | Fix |
|---|---|
| **B1** wrong base / destructive scope | Rebased the whole branch onto `b600c1f8` (clean, no conflicts). `git diff b600c1f8..HEAD` is now only my files. |
| **B2** shipped regex rules regress | Introduced TWO canonical domains (below). `contains`→full, `regex`→LIGHT (punctuation preserved) + IGNORECASE. Shipped rules NOT rewritten. Added read-only corpus test. |
| **B3** NFKC mutant survived | Added `test_nfkc_folds_fullwidth_and_circled_digits` + `test_fullwidth_trust_card_matches_only_with_nfkc` (drop-NFKC mutant → fixture no longer matches). |
| **B4** no-match dedupe mutant survived | Added `test_log_no_match_region_dedupes_per_terminal_region` (+ remove-dedupe mutant → two records). |
| **S1** false targeted counts | Re-measured; this report states the observed numbers (below). |

## Part 1 — canonical-form matcher (services/auto_responder.py)

Pivoted from the original glyph-strip to a canonical-form matcher (user directive).

- `canonicalize(text)` — FULL domain: NFKC → lower → every non-`[a-z0-9]`→space → collapse. Used by `contains` rules and by all digests/diagnostics.
- `canonicalize_light(text)` — LIGHT domain (B2): NFKC → lower → whitespace-collapse ONLY, **punctuation preserved**. Used by `regex` rules (compiled `re.IGNORECASE`).
- `normalize_screen` / `normalize_screen_light` produce the two domains from screen rows.
- `DialogRegion` carries `normalized` (full) + `normalized_light` (light), populated in `dialog_region`.
- `Rule.__post_init__` precomputes `_canon_question`/`_canon_options` (full) and, for regex, compiles `_regex` (IGNORECASE). `Rule.matches` / `reject_reason` take a `DialogRegion` (str fallback) and select the domain: contains→full substring, regex→light search; options always full. Reject reasons name the AUTHORED text.
- Chrome heuristics ported to the canonical domain: `_NUMBERED_OPTION_PATTERN` `\b[1-3]\s+\S` (dot folded), `_CANONICAL_APPROVAL_PATTERN` (^-anchored) for the codex branch of `_looks_like_dialog`.
- Diagnostics: `_log_no_match_region` dumps the FULL canonical region (2 KB cap) on the FIRST no_match per distinct region hash per terminal, deduped; purged in `clear_terminal`.
- `SEED_RULES` codex.yaml header documents the two domains.

**B2 proof (re-verified):** `codex-update-available` regex `Update available! [\d.]+ -> [\d.]+` against its sample:
- vs FULL canonical `update available 0 151 0 0 152 0 …` → **False** (the old single-domain bug)
- vs LIGHT canonical `update available! 0.151.0 -> 0.152.0 …` → **True** (fixed)

**Tests:**
- `test/services/test_f597_canonical_matcher.py` (10): three trust-prompt fixtures (0.150 plain, 0.151 bordered card wrapped across a `│` wall, "Yes, continue anyway…") all match the plain `codex-trust-dir` rule; regex-rule-survives; **mutant** identity-canonicalize → card fixture fails; **B3** NFKC folds `ＡＢＣ①`→`abc1` + fullwidth card matches only with NFKC (drop-NFKC mutant fails); **B4** dedupe writes one record / fresh after clear_terminal (+ remove-dedupe mutant → two).
- `test/services/test_f597_auto_answers_corpus.py` (22): loads EVERY enabled rule from `~/.aws/cli-agent-orchestrator/auto-answers/*.yaml` **read-only** and asserts each matches a representative rendered sample in `test/fixtures/auto_answers_samples/<rule>.txt` (20 enabled rules + 2 guards). Explicit guard for the 3 gate-flagged regex rules.

## Part 2 — codex worker-cwd pre-trust (providers/codex.py)

`-c` override verified NOT usable first (openai/codex#34261: dotted-path `-c` has no quoted-key support; unquoted breaks on the `.` in `.cao/worktrees`). Fallback: `_pretrust_cwd_in_codex_home(cwd, codex_home)` idempotently writes `[projects."<abs cwd>"] trust_level = "trusted"` into the launch CODEX_HOME (additive, never clobbers, refuses invalid TOML, absolutizes cwd). Wired into `initialize()` before `_build_codex_command` via `get_pane_working_directory` + `_resolved_codex_home(self.terminal_id)`; `_handle_trust_prompt` + yaml rule remain belt-and-braces.

**Live-verified codex-cli 0.151.0:** negative control (auth, no trust entry) shows the dialog interactively; positive (config-file trust table) launches straight to the composer, no dialog, no auth prompt. `exec` does not show the dialog (interactive-only) — verified via tmux interactive capture. Doc: https://github.com/openai/codex/issues/34261 (and #18475: `-c` trust_level is non-ephemeral).

**Tests:** `test/providers/test_f597_codex_pretrust.py` (5): writes table / idempotent-no-dup / preserves-existing-content / relative-cwd-absolutized / invalid-TOML-not-modified.

## Part 3 — hygiene

- black `--line-length 100` + isort `--profile black`: clean (`--check` passes on all touched files).
- mypy `--strict`, gate §6 scope (auto_responder.py + codex.py): **base 10, after 10 — UNCHANGED (≤10)**. Per-file after: auto_responder 9, codex 1. tmux.py: base 2 / after 2.

## Part 4 — responder delivery (settle, re-arm) + F598 #455

Root cause (incident 55e84b8a, journal-confirmed): the trust dialog matched and FIRED 22× in ~28s (decisions log 22:00:43→22:01:11) yet never cleared — Enters landed before codex's TUI input handler was armed — then the terminal latched retry-exhausted with no re-arm on the silent pane. Separately, F435's submit-verify ran ONCE, hit its F491 pre-check while the dialog was up (`journalctl`: "F435/F491 submit-verify: terminal 55e84b8a has active dialog … cannot recover with Enter"), raised and gave up; it never re-verified after the dialog cleared, so the `[Pasted Content 3340 chars]` chip stayed drafted.

- **(a) settle** (auto_responder.py): `_settle_before_first_send` requires the matched region byte-stable across two `_settle_capture()`s `SETTLE_INTERVAL_S=0.5s` apart (via `_clock_sleep` seam) before the FIRST send of an episode; once-per-signature; logs `settled`/`settle_unstable`/`settle_lost_frame`; withholds + re-arms a tick if unstable.
- **(b)** tmux.py `send_special_key`: `send-keys -X cancel` gated on `#{pane_in_mode}` (new `_pane_in_mode`, conservative False on read failure); key sent via `pane.cmd("send-keys", key)`. libtmux 0.51.0 `send_keys(key, enter=False)` already emits `send-keys <key>` (no leading space, no `-l`).
- **(c) re-arm** (auto_responder.py): retry-exhaustion seeds `_RearmState` (signature; backoff 5/15/45s then 60s, cap 10min). `_rearm_gate` → fire/hold/None; a latched terminal re-fires on schedule (logged `rearm_fire`) and keeps a tick armed while holding, instead of latching off forever. State cleared on no-match, on leaving WAITING, and in `clear_terminal`.
- **(d) F598 #455** (codex.py `verify_submission_after_send`): the F491 branch now RE-ARMS across the dialog window (`CODEX_SUBMIT_VERIFY_DIALOG_REARM_ATTEMPTS=8` × 1.5s, logs `submit_verify_rearm`) waiting for the trust→chooser sequence to clear, then falls through to the composer stuck-chip recovery Enter. Raises `CodexSubmitStuckError` (→ defer/redeliver) only if the dialog never clears. Minimal change inside F435; no second mechanism.

**Tests:**
- `test/services/test_f597_pt2_settle_rearm.py` (6): settle delays first send to ≥`SETTLE_INTERVAL_S`; unstable frame withholds; **mutant** remove-settle → first send at t=0; re-arm fires after exhaustion once backoff elapses (holds before); re-arm stops when the dialog clears; **mutant** remove-rearm → no send after exhaustion.
- `test/providers/test_f598_submit_verify_dialog_rearm.py` (2): the exact 55e84b8a frame sequence (chooser-while-WAITING → clears → composer+chip) recovers via re-arm + composer Enter; never-clearing dialog still raises (defer, not false success).

## Changed pre-existing tests (for the gate to diff)

All reflect the NEW precondition; no xfail/skip/delete-to-green.

- `test/services/test_auto_responder.py`: assertions moved to the canonical domain; autouse `_reset_engine` stubs `_settle_capture` (echo last on_screen frame) + no-op `_clock_sleep`.
- `test/services/test_auto_responder_seed_rules.py`: `.matches()` args wrapped in `canonicalize(...)`.
- `test/services/test_auto_responder_f516_d6.py`: `_wire` stubs `status_monitor.get_rendered_screen` (feed settle) + no-op `_clock_sleep`.
- `test/services/test_f55_auto_responder_hardening.py`: `_wire` feeds settle via `get_rendered_screen` + no-op `_clock_sleep`; the `.matches()` AST-invariant test updated (matches now takes the region object carrying BOTH domains; every call passes a region Name or `*.normalized`, never `*.rows`; count 8).
- `test/clients/test_tmux_client.py`: `send_special_key` test updated to conditional-cancel + a new in-copy-mode test.

## Verification summary (re-run on head 29d1b278)

- **Targeted set** (`test/services/test_auto_responder*.py test/services/test_f55*.py test/services/test_f597_canonical_matcher.py test/services/test_f597_pt2_settle_rearm.py test/services/test_f597_auto_answers_corpus.py test/providers/test_codex_*.py test/providers/test_f597_codex_pretrust.py test/providers/test_f598_submit_verify_dialog_rearm.py test/services/test_f435_send_seam_verify.py test/clients/test_tmux_client.py test/auto_answers/`): **787 passed, 3 skipped, 8 xfailed**.
- mypy `--strict` (auto_responder + codex): base 10 → after 10.
- black `--check` + isort `--check-only`: clean.
- No full laptop suite (per instruction). Bounded search: fork tree + read-only auto-answers yaml only.

## Mutant ledger (this build)

| Mutant | Test | Result |
|---|---|---|
| `canonicalize = identity` | test_f597_canonical_matcher | KILLED (card fixtures fail) |
| drop NFKC (lower-only full fold) | test_fullwidth_trust_card_matches_only_with_nfkc (B3) | KILLED (fullwidth card fails) |
| regex vs FULL instead of LIGHT | corpus + B2 probe | KILLED (codex-update-available False vs full) |
| drop regex IGNORECASE | test_f597_canonical_matcher | KILLED |
| remove `_log_no_match_region` dedupe | test_mutant_remove_dedupe_writes_two_records (B4) | KILLED (two records) |
| remove settle | test_mutant_remove_settle_sends_at_t0 | KILLED (send at t=0) |
| remove re-arm | test_mutant_remove_rearm_no_send_after_exhaustion | KILLED (no send after exhaustion) |
| skip pretrust idempotency | test_f597_codex_pretrust | KILLED |
| remove invalid-TOML refusal | test_f597_codex_pretrust | KILLED |
