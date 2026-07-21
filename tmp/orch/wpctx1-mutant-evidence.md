# WP-CTX1 Mutation Evidence

All five patches below apply independently to the clean build commit
`8ce8826f6cf9e70cc541839db98f9c01e6eac169`. Each was applied in a detached
worktree, killed by the listed pytest command, reversed, and hash-checked.
Apply and reverse them with `git apply --unidiff-zero <patch>` and
`git apply --unidiff-zero -R <patch>` respectively.

| Mutation | Independently applicable patch | Red output |
| --- | --- | --- |
| Remove digest artifact before refresh | `tmp/orch/wpctx1-patches/m1-remove-digest-artifact.patch` | `tmp/orch/wpctx1-patches/m1-remove-digest-artifact.red.txt` |
| Change `parent_artifact_sha` | `tmp/orch/wpctx1-patches/m2-change-parent-artifact-sha.patch` | `tmp/orch/wpctx1-patches/m2-change-parent-artifact-sha.red.txt` |
| Replace NUL path decoding with line splitting | `tmp/orch/wpctx1-patches/m3-line-split-git-paths.patch` | `tmp/orch/wpctx1-patches/m3-line-split-git-paths.red.txt` |
| Remove digest sender barrier exclusion | `tmp/orch/wpctx1-patches/m4-remove-digest-barrier-exclusion.patch` | `tmp/orch/wpctx1-patches/m4-remove-digest-barrier-exclusion.red.txt` |
| Drop `digest_head` from the CAS write | `tmp/orch/wpctx1-patches/m5-drop-digest-head-cas.patch` | `tmp/orch/wpctx1-patches/m5-drop-digest-head-cas.red.txt` |

The barrier-exclusion and lineage-head patches include the missing assertion
that makes the original named test discriminating at the build commit. Those
assertions are also retained in the fix commit's regression suite.

Aggregate post-restore SHA-256 values are recorded in
`tmp/orch/wpctx1-patches/restore-hashes.txt`. `git status --short` was empty
after every reverse application in the detached replay worktree.

Post-fix focused suite: `211 passed, 3 warnings in 11.08s`; full command and
output are in `tmp/orch/wpctx1-fix-focused.log`.
