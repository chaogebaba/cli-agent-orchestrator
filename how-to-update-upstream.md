# 1. See what's new upstream
git fetch upstream
git log --oneline main..upstream/main   # their commits you don't have

# 2. Merge into your branch (keeps your patches as-is)
git merge upstream/main

# 3. Push to your fork
git push origin main

If the merge conflicts, it'll most likely be in the files we patched (providers/codex.py, services/draft_guard.py, terminal_service.py, api/main.py, session_service.py) — resolve, git add, git commit.

After any merge, redeploy:

uv tool install --force --python 3.13 ~/VScode_projects/cli-subagents/cli-agent-orchestrator
systemctl --user restart cao-server

Tip: run the test suite (uv run pytest) between merge and reinstall to catch upstream changes that break our patches before they go live.
