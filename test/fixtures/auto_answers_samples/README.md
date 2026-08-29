# Auto-answers rule samples (F597 #454 B2)

One representative RENDERED screen per enabled rule in
`~/.aws/cli-agent-orchestrator/auto-answers/*.yaml`, named `<rule-name>.txt`.

`test/services/test_f597_auto_answers_corpus.py` loads every enabled rule from
the live yaml files (read-only) and asserts each still matches its sample after
the two-domain canonicalization (contains → full canonical; regex → light
canonical, punctuation preserved). These samples are the regression guard for
the gate's B2 finding: three enabled regex rules (`askuserquestion-fork-prompt`,
`codex-ratelimit-model-switch`, `codex-update-available`) matched raw at base
and broke under the single-domain canonicalize; the light domain restores them.

Samples are rendered approximations (walls/glyphs/punctuation included where the
rule's regex depends on them), not verbatim pane captures, and are sufficient to
exercise the match. If a rule's wording changes, update its sample here.
