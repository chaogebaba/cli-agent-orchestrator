# === FINDINGS ===

## V5 LIVE SANDBOX GATE (merge-upstream-2a6f20cb)

| Seam | Verdict | Method |
|------|---------|--------|
| Smoke (boot + CLI + assign-equiv + send_message) | **PASS** | G7 sandbox :9890 live |
| Seam1 CLAUDE.md import-reject 3rd arm | **PASS** | Route (a): synthetic pane → real `_handle_startup_prompts` |
| Seam2 deferred-init create→init→delete | **PASS** | G7 sandbox live lifecycle |

### Smoke (live sandbox :9890)
- Server healthy on worktree merkle; `cao --version` 2.4.1; instance `e0be0923`
- Session create + claude_code identity: `bwrap … claude` (F51 OK)
- defer_init worker `47a69725` UNKNOWN→idle; inbox send_message HTTP 200; reply token seen
- Evidence: `$CAO_ARTIFACTS_DIR/v5-gate-run1-*/` and `/v5-gate/evidence/smoke-*`

### Seam1 (route a — preferred)
Live Claude 2.1.223 in sandbox **did not surface** the external-import dialog (parent CLAUDE.md alone is insufficient; dialog is external-@include gated). Did not fight the trigger.

Instead: real handler under `shared-auth-read-only` with synthetic panes:

1. trust → import → welcome: reject Down+Enter, then continue (polls_after_import≥1)
2. import → welcome: reject arm alone + continue
3. **import → trust → welcome (load-bearing):** after reject, still sends Enter for trust — proves `continue` not upstream `return`

Evidence: `tmp/orch/v5-gate/evidence/seam1-route-a-reject-arm.json`

### Seam2 (live)
- create(defer_init) → init idle → DELETE force → GET 404
- No StatusMonitor ghost churn / quiesce_timeout / wedged lease in sandbox log
- tmux window gone after delete
- Evidence: `seam2-delete.json`, `seam2-log-after-delete.txt`

### Cleanup
`cao sandbox down --purge` completed; production :9889 untouched.
