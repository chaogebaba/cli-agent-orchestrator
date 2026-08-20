# AC17 Suite Differential Proof

**Selection:** `test/api/test_security.py test/api/test_lifespan_inbox.py test/backends/ test/providers/test_base_provider.py`  
**Scope:** 247 pre-existing tests covering api, backends, providers (core modules modified by this WP).

## Results

| Run | Commit | Passed | Failed | Errors |
|-----|--------|--------|--------|--------|
| Base | e5ca47f8 | 247 | 0 | 0 |
| Branch | 38385e0f | 247 | 0 | 0 |

## Set Difference

```
Failures on base but not branch: {}
Failures on branch but not base: {}
Symmetric difference: EMPTY
```

**Conclusion:** Identical zero-failure sets. Zero regressions.

## Note on test_tmux_backend.py

Two delegation tests initially asserted the old `create_session`/`create_window` call signatures (without `terminal_token`). Updated in this commit to match the new signature (which passes `terminal_token=None` by default). Both assertions produce the same semantic result — the backend delegates correctly.
