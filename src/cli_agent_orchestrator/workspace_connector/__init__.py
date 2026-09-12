"""F862 Amendment D connector: read-only workspace pull plane (D5).

A port, file-for-file in behaviour, of the read-only pull plane of
``XiaoDuoYa/codex-with-chatgpt`` (MIT), pinned at upstream commit
``8fdd97c188c7678d0d9c43b3769b426940de568a``.  TypeScript is never vendored;
each module's docstring records the upstream path it was ported from.  See
``THIRD_PARTY_NOTICES.md`` (``codex-with-chatgpt``) and
``LICENSE.codex-with-chatgpt`` shipped beside this package.

Upstream modules ported here (D5 source map):

* ``src/mcp/server.ts``    -> :mod:`.tools` (six read-only tools; the three
  execution tools — ``test_status``/``execution_summary``/``execution_output``
  — are deliberately NOT ported; D5)
* ``src/workspace/manager.ts`` -> :mod:`.workspace_manager`
* ``src/workspace/git.ts``     -> :mod:`.git_reader`
* ``src/workspace/search.ts``  -> :mod:`.search`
* ``src/workspace/ignore.ts``  -> :mod:`.ignore_rules`
* ``src/auth/oauth.ts``        -> :mod:`.oauth`
* ``src/auth/middleware.ts``   -> :mod:`.middleware`
* ``src/auth/store.ts``        -> :mod:`.auth_store`
* ``src/pairing/manager.ts``   -> :mod:`.pairing`
* ``src/mcp/http.ts``          -> :mod:`.http_server`

Deliberately NOT ported (D5): ``src/tunnel/*``, ``src/bridge/*``,
``src/process/daemon.ts``, ``src/execution/*``, ``src/session/state.ts``,
``src/cli/*``, ``src/auth/html.ts`` (the pairing page is re-authored minimal).

CAO additions beyond the upstream schemas (blueprint D5, r3 N4): a content
digest on every result, an attempt-scoped audit projection, ``.caoignore`` (the
``.c2cignore`` rename), manifest enforcement, and the pull budgets
(``CHATGPT_PULL_RESULT_BYTES`` / ``CHATGPT_PULL_ATTEMPT_BYTES`` /
``CHATGPT_PULL_ATTEMPT_CALLS``).
"""
