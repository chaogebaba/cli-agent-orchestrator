# ChatGPT-web lane (`chatgpt_web`)

A worker provider that drives a logged-in chatgpt.com **web** session from
Python (F862) to produce findings on the near-free web-account quota. The
platform API is prohibited on this path and is denied at request time.

How a turn works today (F862 shape A + F970):

| Step | Who does it | Why |
|---|---|---|
| Compose + send | the real page (composer keypress) | the app computes the sentinel/PoW/Turnstile material; the runner forges none of it |
| Progress + quota | the send SSE, **teed and consumed** (F970) | token-level progress and `limits_progress` without touching the anti-bot surface |
| Completion + answer | `GET /backend-api/conversation/<id>` | authoritative (D6); the stream never promotes a partial |

## Watching a turn live (F970 relay)

The runner publishes its allow-listed progress to cao-server, which relays it
over SSE with per-turn `seq` ids and replay. The turn id is the run id printed
by the runner (`[chatgpt_web] RUNNING` … the `FINDINGS-READY` marker names the
report; the run id is in the report path).

```bash
# follow a turn live (native SSE; ids are per-turn seq numbers)
curl -N http://127.0.0.1:8990/chatgpt/turns/<turn-id>/events

# resume exactly after the last event you saw (or send Last-Event-ID)
curl -N 'http://127.0.0.1:8990/chatgpt/turns/<turn-id>/events?after_seq=42'

# one-shot JSON snapshot instead of a stream
curl -s 'http://127.0.0.1:8990/chatgpt/turns/<turn-id>/events?stream=false' | jq
```

Frames are `event: <kind>` with `id: <seq>`: `turn_started`, `token`, `status`,
`metadata`, `quota`, `stream_complete`, and terminal `turn_finished` /
`turn_failed` (which close the stream). A reader that fell behind the bounded
ring receives an explicit `event: gap` frame rather than a silently
discontiguous stream. `turn_started`, `quota` and the terminal events are also
mirrored onto the fleet `/events` bus, so a fleet watcher sees lifecycle without
the token firehose.

Publishing is bound to the worker's own `X-CAO-Terminal-Token` — a write scope
alone cannot inject a turn stream under a worker's name. Set
`CAO_CHATGPT_TURN_RELAY=0` in the worker env to disable publishing entirely.

## Pulling context instead of uploading it (F970)

The lane's most fragile subsystem is the attachment path (upload-readiness
calibration, chip identity, stall DOM dumps). The alternative, taken in shape
from `XiaoDuoYa/codex-with-chatgpt` (MIT), is to let the model **pull** what it
needs from the real tree instead of being handed a snapshot someone else chose.

`cao-mcp-server` therefore carries an optional, **read-only** workspace group:

```bash
CAO_WORKSPACE_READ_TOOLS=1 CAO_WORKSPACE_ROOT=/path/to/repo   # then restart cao-server
```

Tools: `workspace_info`, `workspace_list_directory`, `workspace_read_file`,
`workspace_search`, `workspace_git_status`, `workspace_git_diff`.

- **Registration-time gating.** With the flag unset the tools do not exist, so
  a disabled feature costs no tool-surface context on any worker's turn.
- **Read-only by construction.** There is no write, patch, shell or commit tool
  in the group — absent, not disabled — so prompt injection has nothing to
  reach.
- **Canonical containment.** Every path is realpath-resolved (deepest existing
  ancestor first) and checked against the root, so `..`, absolute paths, a
  symlink out of the tree and a symlinked parent with a missing leaf all fail
  identically with `PATH_OUTSIDE_WORKSPACE`.
- **Sensitive files are denied, not hidden**: `.env*` (except `.env.example`),
  keys, `.ssh/`, `.aws/`, `.gnupg/`, netrc, credential JSON, cookie stores, plus
  this fork's own `providers.toml` and `session-export.json`. `.caoignore` adds
  rules; it cannot remove the built-ins. The same policy gates the ChatGPT-web
  lane's **bundle** path, so "attach this file" is not a way around "you may not
  read `.env`".
- Refusals come back as `{ok: false, error_code: …}` data, never a traceback.

Serving these to ChatGPT itself as a custom connector is a follow-up: it needs
Settings → Security and login → **Developer mode** turned on by the account
owner, then chatgpt.com/plugins → **+** → a streamable-HTTP endpoint whose URL
ends in `/mcp`. Until then the group is a local MCP surface for CAO's own
workers.
