# F545 build report — #401 doorbell rings wrong Claude when seat window is split

## Base / branch / SHAs

- Repo: `chaogebaba/cli-subagents` (fork)
- Base: `main` @ `f6c3504670c9a2ec53c60a470df6f8b925ac1123`
- Worktree branch: `cao/a9ab7f9e`
- Code commit: `ec872703` — `F545: doorbell resolve_target keys on window's first pane, not active pane (#401)`
- Report commit: this commit — `F545: build report`

## Did the lead hold? YES — confirmed empirically, root cause unchanged

The issue lead named `pane_pid(tmux_session, tmux_window)` (via
`resolve_target` → `_descendants` → candidate records) as resolving the
WINDOW'S ACTIVE pane, so a split seat window with the second pane focused
walked the consultant's process tree.

Proven with a throwaway split window (my own scratch tmux window, killed
immediately — the supervisor session was never split/altered) — a bare
window-target `#{pane_pid}` returns the **active** pane's pid:

```
$ tmux list-panes -t <session>:<probe> -F '#{pane_index} #{pane_id} #{pane_pid} active=#{pane_active}'
0 %17 377853 active=0
1 %18 377867 active=1
$ tmux select-pane -t <session>:<probe>.1
$ tmux display-message -p -t <session>:<probe> '#{pane_id} #{pane_pid} active=#{pane_active}'
%18 377867 active=1      # <-- ACTIVE pane, i.e. the SECOND pane / consultant tree
```

The cross-check `_record_matches_pane` only compared `<session>+<window_id>`,
so a second pane in the same window passed. Lead confirmed; no re-diagnosis
needed.

## Fix — AC checklist with file:line

### AC1 — `pane_pid` resolves the window's FIRST pane, not the active pane; cite tmux format
- `src/cli_agent_orchestrator/services/fork_context_service.py:406` — new
  `first_pane(session, window) -> (pane_id, pid)` runs
  `tmux list-panes -t <session>:<window> -F '#{pane_index} #{pane_id} #{pane_pid}'`,
  sorts rows by `#{pane_index}` ascending, returns the lowest-index pane
  (consistent with `clients/tmux.py:478` `window.panes[0]`, called from the
  first-pane usages around `clients/tmux.py:1284`).
- `src/cli_agent_orchestrator/services/fork_context_service.py:450` — `pane_pid()`
  now returns `first_pane(session, window)[1]` (line 456). The old bug used
  `display-message -p -t <session>:<window> '#{pane_pid}'` (active pane).

### AC2 — cross-check compares pane_id when the record has one, against the first pane's %N
- `src/cli_agent_orchestrator/services/cc_session_registry.py:259` —
  `_record_matches_pane(record, session, window_id, pane_id=None)` now parses the
  record's `%N` and, when `pane_id` is supplied, requires `rec_pane_id == pane_id`
  (session+window_id alone no longer suffices).
- `src/cli_agent_orchestrator/services/cc_session_registry.py:322` — `resolve_target`
  calls `first_pane` to get `(seat_pane_id, pane_leader)` and passes `seat_pane_id`
  into the cross-check.

### AC3 — a candidate whose pid tree is not under the first pane is never a target, and never `target_ambiguous`
- `src/cli_agent_orchestrator/services/cc_session_registry.py:326` — `_descendants(pane_leader)`
  is computed from the FIRST pane's leader, so `candidate_records` only ever
  contains records under the first pane. A second-pane record is excluded before
  any ambiguity check runs. Empty candidate set → `no_descendant_record` (nudge
  fallback), never `target_ambiguous`. See the `len(matched)==0` branch at
  `cc_session_registry.py:352` (single procfs candidate w/o pane match is still
  used — procfs is authoritative for "which pane"; it can only be a first-pane
  record).

### Unit tests
- `test/services/test_fx170_native_doorbell.py` → `TestF545FirstPaneResolution`:
  - `test_two_panes_both_have_cc_records_picks_first_pane` — two-pane split, CC
    records in BOTH trees → first pane's record (pid 300, `session_id="seat"`)
    chosen; consultant (pid 500, `%1`) excluded. Real `_descendants` over a
    synthetic `/proc`.
  - `test_single_pane_unchanged` — single pane, one record → resolves it.
  - `test_cc_only_in_second_pane_no_target_not_ambiguous` — CC only under second
    pane → `no_descendant_record`, asserted `!= target_ambiguous`.
- `test/services/test_fork_context_service.py`:
  - `test_first_pane_selects_lowest_index_not_active` — list-panes emits active
    pane first (`%18`), first_pane still returns lowest-index `%17`.
  - `test_first_pane_single_pane`, `test_first_pane_no_panes_raises`.
- Updated existing patch sites (patched the now-removed `cc_session_registry.pane_pid`)
  to patch `first_pane` returning `(pane_id, pid)`:
  `test_fx170_native_doorbell.py` (9), `test_fx179_epoch_timestamps.py` (5),
  `test_f216_null_socket_path.py` (4).

### Follow-up (NOT in this change, per brief)
- Do NOT persist pane ids in terminal metadata here. Follow-up: persist pane `%N`
  in terminal metadata at create time (`terminal_service.py` ~2279) so the
  registry cross-check has a stored pane id to compare against (F544 note).

## Verbatim test lines

Mandated set — touched test files (`test_cc_session_registry*` / `test_doorbell*`
do not exist in-tree; the resolver + doorbell coverage lives in the fx170/fx179/
f216 files, all run):

```
$ uv run pytest test/services/test_fork_context_service.py \
    test/services/test_fx170_native_doorbell.py \
    test/services/test_fx179_epoch_timestamps.py \
    test/services/test_f216_null_socket_path.py -p no:cacheprovider
119 passed in 10.02s
```

Doorbell + auth-handshake regression net:

```
$ uv run pytest test/services/test_f158_doorbell_fallback.py \
    test/services/test_f158_r2_doorbell_regression.py \
    test/services/test_f461_doorbell_coalesce.py \
    test/services/test_fx168_doorbell.py \
    test/services/test_f337_auth_handshake.py -p no:cacheprovider
115 passed in 5.35s
```

F545-only tests:

```
$ uv run pytest test/services/test_fx170_native_doorbell.py::TestF545FirstPaneResolution \
    test/services/test_fork_context_service.py::test_first_pane_selects_lowest_index_not_active \
    test/services/test_fork_context_service.py::test_first_pane_single_pane \
    test/services/test_fork_context_service.py::test_first_pane_no_panes_raises -p no:cacheprovider
6 passed in 3.30s
```

## Lint / type status (touched lines)

- `black` (repo config, line-length 100, target py310): my touched lines are
  clean — `black --diff` shows ZERO hunks touching the inserted `first_pane`,
  `pane_pid`, `_record_matches_pane`, resolve-step, or test-patch lines. The
  files `test_fx179_epoch_timestamps.py` / `test_f216_null_socket_path.py` carry
  PRE-EXISTING black non-conformance (blank line after inline imports; def
  signature wrapping) that predates this change and was deliberately left
  untouched (scope discipline — no whole-file drive-by reformat).
- `isort`: touched imports clean.
- `mypy --strict src/.../fork_context_service.py src/.../cc_session_registry.py`:
  5 errors reported, ALL on PRE-EXISTING lines
  (`fork_context_service.py:204,260`; `cc_session_registry.py:91,117`) — none on
  any line touched by this change. New `first_pane` is fully annotated
  (`-> tuple[str, int]`).

## Reviewer verification recipe

From the worktree `.cao/worktrees/a9ab7f9e` (branch `cao/a9ab7f9e`):

```
# 1. Run the tests (no full suite — laptop RAM ban):
uv run pytest test/services/test_fork_context_service.py \
  test/services/test_fx170_native_doorbell.py \
  test/services/test_fx179_epoch_timestamps.py \
  test/services/test_f216_null_socket_path.py -p no:cacheprovider

# 2. Confirm my touched lines are black-clean (no hunk on first_pane/resolve edits):
uv run black --diff src/cli_agent_orchestrator/services/fork_context_service.py \
  src/cli_agent_orchestrator/services/cc_session_registry.py | grep -E '^@@|first_pane'

# 3. LIVE read-only repro of the ROOT CAUSE against a real split window.
#    Proves display-message on a window target returns the ACTIVE pane while
#    list-panes (what first_pane uses) returns the lowest-index pane.
#    Use a THROWAWAY tmux session of your own — NEVER split or alter
#    cao-claude-orch5 (the user's supervisor session):
S=f545review; W=probe
tmux new-session -d -s "$S" -n "$W" 'sleep 120'
tmux split-window -h -t "$S:$W" 'sleep 120'
tmux select-pane -t "$S:$W.1"                       # focus the SECOND pane
echo "list-panes (first_pane picks lowest index):"
tmux list-panes -t "$S:$W" -F '#{pane_index} #{pane_id} #{pane_pid} active=#{pane_active}'
echo "display-message window-target (OLD pane_pid = ACTIVE pane):"
tmux display-message -p -t "$S:$W" '#{pane_id} #{pane_pid} active=#{pane_active}'
tmux kill-session -t "$S"
# Expected: display-message reports active=1 (second pane); list-panes' first row
# is index 0 / active=0 (the pane first_pane / pane_pid now select).

# 4. (Optional, read-only) inspect the supervisor seat window layout — READ ONLY,
#    do NOT split/alter it (the user's live two-pane repro has since been closed):
tmux list-windows -t cao-claude-orch5 -F '#{window_id} #{window_name} #{window_index}'
tmux list-panes  -t cao-claude-orch5:0 -F '#{pane_index} #{pane_id} #{pane_pid} active=#{pane_active}'
```
