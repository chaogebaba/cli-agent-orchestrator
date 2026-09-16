# Auto-answers rule samples (F597 #454 B2)

One representative RENDERED screen per rule in the versioned corpus
(`../auto_answers_corpus/<provider>.yaml`), named `<rule-name>.txt`. A rule with
more than one screen declares its extras in
`../auto_answers_corpus/extra-samples.yaml`.

`test/services/test_f597_auto_answers_corpus.py` loads every rule from the
corpus and asserts each still matches its sample(s) after the two-domain
canonicalization (contains → full canonical; regex → light canonical,
punctuation preserved). These samples are the regression guard for the gate's B2
finding: three enabled regex rules (`askuserquestion-fork-prompt`,
`codex-ratelimit-model-switch`, `codex-update-available`) matched raw at base and
broke under the single-domain canonicalize; the light domain restores them.

Samples are rendered approximations (walls/glyphs/punctuation included where the
rule's regex depends on them), not verbatim pane captures, and are sufficient to
exercise the match. If a rule's wording changes, update its sample here.

Until F932 #784 the test enumerated the rules in the machine's live
`~/.aws/cli-agent-orchestrator/auto-answers/*.yaml` instead, which made the
suite's colour depend on the host. The corpus is versioned now; the live
directory is only ever read to REPORT drift. See `../auto_answers_corpus/README.md`.

The pairing runs both ways and both directions are enforced: a corpus rule with
no sample fails `test_every_enabled_rule_has_a_sample`, and a sample no corpus
rule claims fails `test_every_sample_belongs_to_a_corpus_rule`.
