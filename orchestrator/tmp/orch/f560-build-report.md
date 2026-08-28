# F560 part 1 — Kiro CLI session resume (id minting) — build report

- **Worker terminal:** 87b2c2bf  ·  **Branch:** `cao/87b2c2bf`  ·  **Tip:** `efd9bb98`
- **Commits:** `243b9a4b` (impl) → `efd9bb98` (e2e)
- **Verified against:** `kiro-cli 2.20.1` (`/home/chao/.local/bin/kiro-cli`)

## Docs finding (3 lines)

1. Kiro CLI resumes a chat session with `kiro-cli [--v3] chat --resume-id <SESSION_ID>` (also `--resume` = most recent for cwd, `--resume-picker`, `--list-sessions --format json`, `--delete-session`); sessions auto-save per-directory to one SQLite DB `~/.local/share/kiro-cli/data.sqlite3` (`conversations`=v1, `conversations_v2`=v3/KAS), each with a UUID. Docs were already cached — no firecrawl needed.
2. **Design pivot (user, via supervisor msg 1805):** kiro accepts a NEVER-USED `--resume-id sess_<uuid>` on the FIRST launch and starts a fresh session that ADOPTS that id — so the provider MINTS the id at spawn (claude_code `--session-id` pattern), not codex-style /proc capture.
3. Live-proven: minted id adopted verbatim (both `sess_`-prefixed and bare uuid), and the ZQX7 token round-trips across a quit+resume.

## Design implemented (mint pattern)

- `KiroCliProvider.__init__` mints `self.allocated_session_uuid = f"sess_{uuid4}"` on a fresh spawn; on a resume `ForkContext` it reuses the prior id verbatim.
- Launch ALWAYS passes `--resume-id <allocated_session_uuid>` (fresh → adopts; re-used → resumes). `build_kiro_command(..., resume_session_id=...)` places `--resume-id` after `chat`/trust flags, before `--model`/`--agent` (both `--v3`/KAS and `--agent-engine v2`).
- `terminals.provider_session_id` is written **at create time** by the existing seam `provider_session_id = resume_uuid or allocated_uuid` (terminal_service.py:2794). No sqlite/pane capture on the hot path.
- `resume_session_uuid()` returns the resume seed (drives settlement_form="resume" superseding on wake).
- `capture_session_uuid()` returns the minted id; a `--list-sessions --format json` capture (`fork_context_service.capture_kiro_uuid`) is retained ONLY as a legacy fallback for a terminal with no minted id.
- **NOT** `supports_reauth_rebind`: a freshly minted id has no session artifact until the first turn, so the capture/validate seam is both unnecessary and would spuriously fail. Mirrors claude_code.
- `manager.py` now passes `fork_context` + `skill_prompt` into `KiroCliProvider`.
- Defensive `E-KIRO-SESSION-LOCKED`: if a resume's ready-wait fails and the pane shows a "session active in another process (PID …)" banner, init raises a clear error instead of a bare timeout. (No hard lock was observed live on 2.20.1 — see caveats — so this is a guard, not a verified path.)

## Live probe evidence (laptop, throwaway tmux; panes quoted)

**Mint + adopt (fresh id):** launched `kiro-cli --v3 chat --trust-all-tools --resume-id sess_9b061ada-…` in a fresh cwd; `/session-id` printed:

```
● Session ID: sess_9b061ada-e12e-4166-9f93-cfc0815e0744
  Resume with: kiro-cli --resume-id sess_9b061ada-e12e-4166-9f93-cfc0815e0744
```

**Round-trip:** "Remember this token: ZQX7." → `ZQX7 — got it.` ; `/quit`; `--list-sessions --format json` showed the id (title "Acknowledge Token ZQX7"); relaunch same id → "What token…?" →

```
  What token did I ask you to remember? Reply with only the token.
  ZQX7
▸ Credits: 0.05 • Time: 1s
```

**Caveats recorded (msg 1795/1805 items):**
- (5) **id format:** BOTH `sess_<uuid>` and bare `<uuid>` are adopted verbatim (`● Session ID: 52442c81-…` for a bare-uuid launch). We mint the `sess_` form to match kiro's native v3 format.
- (6) `--trust-all-tools` and `--agent <profile>` are honored with `--resume-id` on both first launch and resume (verified). `--model` is passed in the same argv and unaffected.
- (2) **session lock:** resuming the SAME id while the first process was still alive did NOT raise a lock error on 2.20.1 — the second process attached and replied (rc=0). So no hard single-process lock manifests here; in normal F444 wake the supervisor reaps the old worker first anyway. `E-KIRO-SESSION-LOCKED` remains as a defensive guard.
- **`--no-interactive` + `--resume-id` is unreliable** under `--v3`: it spawned NEW sessions and once hit `HTTP 429 ThrottlingException`, and did not recall. CAO drives kiro INTERACTIVELY in tmux (never `--no-interactive`), and the interactive path recalls correctly, so this does not affect the provider.

## Tests + mutation notes

Unit (local `-n0` AND on grok box) — 218 passed:
- `test/providers/test_kiro_capabilities.py` — **mutation:** `build_kiro_command` gained `resume_session_id`; new tests assert `--resume-id` placement on KAS and v2 (before `--model`), and omission when None.
- `test/providers/test_kiro_cli_unit.py` — **mutation:** launch now always includes `--resume-id <minted>`; updated the 5 existing exact-command assertions to interpolate `provider.allocated_session_uuid`. New `TestKiroCliSessionResumeMint` (14 tests): mint shape/uniqueness, resume reuse, launch arg on KAS + resume, `capture_session_uuid` returns minted (no subprocess) + legacy fallback, `supports_reauth_rebind is False`, and `E-KIRO-SESSION-LOCKED`.
- `test/services/test_fork_context_service.py` — **mutation:** added `capture_kiro_uuid`/`_select_kiro_session_id`/`_kiro_list_sessions_argv`; new tests cover argv shape, newest-updatedAt selection, unparseable/none error codes, capture success + retry-then-raise.

E2E (`test/e2e/test_kiro_session_resume.py`, marked `e2e`): encodes the full provider mint→resume→recall ZQX7 round-trip. **Skipped by default** (`CAO_F560_E2E` gate) because `provider.initialize()` is coupled to CAO's StatusMonitor/FIFO pipeline (a *registered* terminal) via `wait_for_shell`, so it is NOT runnable provider-direct without the full server harness. The provider's flag behaviour is instead proven by the unit suite + the manual live tmux round-trip above. **Open item for supervisor:** a true live e2e needs the MCP `assign(resume=True, fork_from=<id>)` path against a running cao-server with a registered base session; not built here.

Lint: `black`/`isort` clean on all changed files. `mypy --strict` on the three src files reports only PRE-EXISTING errors (fork_context_service.py:204/260, kiro_cli.py:219 `Optional[list]` sig) — none in the F560-added ranges; left untouched per scope discipline.

## Box-actions ledger

- Box: **grok-box-2** (slot-locked via `scripts/box-run.sh`).
  - `box-run.sh f560-units` — `git fetch origin cao/87b2c2bf && git checkout -B cao/87b2c2bf origin/cao/87b2c2bf && uv run pytest test/providers/test_kiro_cli_unit.py test/providers/test_kiro_capabilities.py test/services/test_fork_context_service.py -n0` → **218 passed**.
  - `box-run.sh f560-headcheck` — `git rev-parse HEAD` → `efd9bb98…`; `hostname` → grok-box-2 (read-only).
  - Left at: branch `cao/87b2c2bf` @ `efd9bb98`, clean. No apt/pip/uv installs, no lockfile changes, no temp files left.
  - No raw ssh used. No deviations.

## Docs commit — BLOCKED (needs supervisor action)

The two docs files I produced are written to the ROOT repo at
`docs-cache/kiro/session-management.md` and `docs-cache/kiro/f560-resume-verification.md`
(the dir is gitignored). Committing them on `cao/f560-docs` and pushing is
**blocked by the fx121 worktree-containment hook**, which refuses any git op
whose cwd/target is the root repo (`/home/chao/VScode_projects/cli-subagents`).
The files are on disk; the supervisor (root-repo writer) needs to force-add +
commit + push them. Sibling lane also left `v3-session-management.md`/`v3-overview.md` there.
