# WPM2 r21 mutation evidence

The earlier prose-only mapping was withdrawn. The actual-only ledger is generated
by `tmp/orch/run-wpm2-mutations-r2.sh` and published at:

`/home/chao/VScode_projects/cli-subagents/tmp/orch/wpm2-mutation-r2/ACTUAL_MUTATION_LEDGER.md`

That artifact contains, for every m1-m18 mutation:

- the applied unified diff;
- the exact pytest command;
- the nonzero exit and failure excerpt;
- the full pytest output; and
- the live-source SHA-256 before/after restoration proof.

Latest actual run: **18 killed / 0 survived**. This file makes no projected kill
claim; the generated artifact is the sole result authority.
