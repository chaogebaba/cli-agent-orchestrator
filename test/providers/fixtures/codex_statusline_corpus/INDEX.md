# Codex TUI status_line fixture corpus

- **Codex:** 0.145.0 (`codex-cli 0.145.0`)
- **Date:** 2026-07-22
- **Method:** scratch `tmux new-session -d` (prefix `fx-codex-sl-*`, never CAO sessions); `tmux capture-pane -p` (plain) + `-e` (ansi); exit via Ctrl-C.
- **Full 5-element config:** `["current-dir","git-branch","model-with-reasoning","context-remaining","five-hour-limit"]`
- **Each variant:** `<name>.plain.txt` + `<name>.ansi.txt` + `<name>.meta.txt`
- **Launch notes:** trust dialog auto-accepted (Enter); hooks-review auto-chose `3` (continue without trusting) when present. Most idle captures used `CODEX_HOME=/tmp/fx-codex-minhome` (auth copied, no mcp_servers). Busy used real `~/.codex` + `-c` status_line override so model_provider streams.

## Index

| filename (pair) | config | cwd | git state | observed footer (verbatim, rstrip trailing spaces) |
|---|---|---|---|---|
| `01-git-master-full` | full 5-element | `~/VScode_projects/cli-subagents` | `master` | `~/VScode_projects/cli-subagents · master · gpt-5.6-sol high · Context 100% left` |
| `02-nongit-tmpx-full` | full 5-element | `/tmp/x` | non-git | `/tmp/x · gpt-5.6-sol high · Context 100% left` |
| `02b-nongit-home-full` | full 5-element | `~` (`/home/user`) | non-git | `~ · gpt-5.6-sol high · Context 100% left` |
| `03-deep-path-full` | full 5-element | deep `/tmp/deep/.../abcdefghijklmnopqrstuvwxyz` (cols=120) | non-git | `/tmp/deep/path/that/is/quite/long/and/maybe/truncates/status/line/segment/abcdefghijklmnopqrstuvwxyz · gpt-5.6-sol hi…` |
| `03b-deep-path-narrow60` | full 5-element | same deep path (cols=60) | non-git | `/tmp/deep/path/that/is/quite/long/and/maybe/truncates/sta…` |
| `04-special-path-dot-full` | full 5-element | `/tmp/fx a·b` (space + middle-dot in path) | non-git | `/tmp/fx a·b · gpt-5.6-sol high · Context 100% left` |
| `04b-special-branch-dot-full` | full 5-element | `/tmp/fx-statusline-repo` | `feat/x·y` (git allows middle-dot in branch) | `/tmp/fx-statusline-repo · feat/x·y · gpt-5.6-sol high · Context 100% left` |
| `05-five-hour-limit-pair` | `["current-dir","five-hour-limit"]` | cli-subagents | master | `~/VScode_projects/cli-subagents` |
| `05b-five-hour-only` | `["five-hour-limit"]` | cli-subagents | master | *(no status_line row — last non-blank is composer placeholder)* |
| `06-context-remaining-only` | `["context-remaining"]` | cli-subagents | master | `Context 100% left` |
| `06b-current-dir-only` | `["current-dir"]` | cli-subagents | master | `~/VScode_projects/cli-subagents` |
| `07-baseline-empty-statusline` | `tui.status_line=[]` | cli-subagents | master | `? for shortcuts                                                                                    100% context left` |
| `07b-baseline-empty-nongit` | `tui.status_line=[]` | `/tmp/x` | non-git | `? for shortcuts                                                                                    100% context left` |
| `08-busy-full` | full 5-element | cli-subagents | master | status still: `~/VScode_projects/cli-subagents · master · gpt-5.6-sol high · Context 100% left`; progress: `• Working (1s • esc to interrupt)` above suggestion `› …` |
| `09-dir-branch-only` | `["current-dir","git-branch"]` | cli-subagents | master | `~/VScode_projects/cli-subagents · master` |
| `09b-dir-branch-nongit` | `["current-dir","git-branch"]` | `/tmp/x` | non-git | `/tmp/x` |

Leading two spaces on status rows (from pane capture) omitted in the table for readability; present in the raw `.plain.txt` files.

## Variants NOT produced / caveats

1. **`five-hour-limit` segment never rendered** in this environment. Configs `["current-dir","five-hour-limit"]` and `["five-hour-limit"]` produced path-only or empty chrome respectively — no `Nh Nm left` / similar. Segment appears gated on an active five-hour usage-limit session (not just being listed in config). No synthetic way to force it without a live limit window.
2. **Legacy baseline wording differs** from status_line: empty config uses `100% context left` (lowercase “context”, percentage first); status_line uses `Context 100% left` (capital C, “Context” first).
3. **Missing git-branch** omits the segment entirely — no empty `· ·` double-separator. Nongit full config: `path · model · Context…` (branch slot collapsed). Nongit dir+branch only: just `path` (no trailing `·`).
4. **Deep path truncation** uses Unicode ellipsis `…` at end of the whole status line (cols=120) or of the path-only remnant (cols=60). Header box uses mid-path `…` (`/tmp/…/segment/...`) separately from status_line truncation.
5. **Path containing `·`** (`/tmp/fx a·b`) and **branch containing `·`** (`feat/x·y`) both render; a naive ` · `-split parser will over-split on these.
6. **Busy coexistence:** full status_line remains on the bottom row while `• Working (Ns • esc to interrupt)` appears above. Idle suggestion `› …` may still show under the progress line.
7. **MCP boot noise:** real `~/.codex` may show `• Booting MCP server: … (Ns • esc to interrupt)` before idle; that also contains `esc to interrupt` and can false-positive as “busy.” Min-home captures avoided configured MCP servers; built-in apps still sometimes booted until `--disable apps`.

## File inventory

16 plain + 16 ansi + 16 meta = 48 capture files, plus this INDEX.
