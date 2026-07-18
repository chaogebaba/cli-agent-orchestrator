# Updating from upstream

## 1. See what's new

```
git fetch upstream
git log --oneline main..upstream/main   # their commits you don't have
```

## 2. Evaluate BEFORE merging (standard step)

Delegate evaluation to a **grok_dev CAO worker** (via `assign` from the
supervisor): give it the upstream commit range and have it assess, against
BOTH our patched codebase and the active blueprints under
`../blueprints/`:

- what each upstream commit changes and why;
- overlap with our patch surface (historically: `providers/codex.py`,
  `services/draft_guard.py`, `terminal_service.py`, `api/main.py`,
  `session_service.py` — but check the actual diff, our surface grows);
- whether any upstream change contradicts a frozen/shipped blueprint law
  or invalidates a pinned behavior.

**Non-trivial** (touches our patch surface, changes behavior we pinned, or
conflicts with a blueprint): grok_dev writes a report
(`tmp/orch/upstream-eval-<sha>.md` in the outer repo) covering impact,
conflicts, and a recommended resolution per file — read it before merging.

**Trivial** (no overlap with our patches/blueprints, e.g. docs, CI,
untouched modules): the supervisor decides the merge shape directly; no
report needed.

## 3. Merge into your branch (keeps your patches as-is)

```
git merge upstream/main
```

If the merge conflicts, resolve per the evaluation (report's per-file
recommendations for non-trivial; preserve-both-laws default for
independent features), `git add`, `git commit`. A conflict that forces
choosing one behavior over another is a hard STOP — surface it to the
user, don't pick silently.

## 4. Test between merge and deploy

```
uv run pytest        # catches upstream changes that break our patches before they go live
```

Also rerun the no-extras mypy cells if a merge touches typed modules —
the canonical baseline count is pinned in the current WP blueprints.

## 5. Push and redeploy

```
git push origin main
```

Redeploy pair (kills every live CAO session — the supervisor must NOT run
this mid-session; announce readiness and let the user run it):

```
~/VScode_projects/cli-subagents/install.sh   # uv tool install --force + profile reinstall
systemctl --user restart cao-server
```
