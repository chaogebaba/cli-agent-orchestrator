# Versioned auto-answers corpus (F932 #784)

One file per provider, mirroring the shape of
`~/.aws/cli-agent-orchestrator/auto-answers/<provider>.yaml`, holding the rules
that have a committed sample under `../auto_answers_samples/`.

## Why this exists

`test/services/test_f597_auto_answers_corpus.py` used to enumerate the rules in
the **live** `~/.aws/cli-agent-orchestrator/auto-answers/*.yaml`. Those files are
operator state: written into the home by `cao install`, then hand-edited, and
versioned in neither repo. The suite's colour therefore depended on the host —
the same commit was green on grok-box-003, red on grok-box-009
(`codex-hooks-list-close`) and red differently on the laptop
(`codex-shared-agents-unavailable-return`, `kiro-command-palette-transient`).
A "new vs pre-existing failure" comparison could not be made across machines.

These files make the input hermetic: the corpus is a repo-vs-repo assertion, so
every host reads the same rules. The live directory is still consulted, but only
by `test_live_rule_drift_is_reported_not_asserted`, which WARNS and never fails.

## What is in it

A rule belongs here when it has a sample. That pairing is the whole point: the
F597 #454 B2 guard is "this rule still matches a representative rendered screen
under the two-domain canonical matcher", and a rule with no sample has no
evidence to guard. Disabled rules are kept when they have a sample — matching is
independent of `enabled`, and the canonicalization regression this guards
against does not care whether a rule is switched on.

Live rules deliberately **not** snapshotted, because no sample exists for them
(2026-09-16). A sample is a rendered screen; authoring one from the rule's own
`question` pattern would make the test circular, so these stay gaps and are
reported by the drift test rather than faked:

| rule | provider | enabled live |
|---|---|---|
| `codex-hooks-list-close` | codex | yes |
| `codex-shared-agents-unavailable-return` | codex | yes |
| `kiro-command-palette-transient` | kiro_cli | yes |
| `codex-content-policy-banner` | codex | no |

## Changing it

Adding a rule here without adding its sample fails
`test_every_enabled_rule_has_a_sample`. Adding a sample that no corpus rule
claims fails `test_every_sample_belongs_to_a_corpus_rule`. Rules shipped in
`auto_responder.SEED_RULES` must be present here — that is the hermetic form of
"no shipped rule may lack a sample", enforced by
`test_seed_rule_is_in_the_corpus_and_matches_its_sample`.
