"""F970 (#819) — the read-only workspace surface and its containment.

The value of a pull surface is entirely in what it REFUSES, so these tests are
mostly refusals. The escape shapes are the ones their implementation
(codex-with-chatgpt, `src/workspace/manager.ts`) defends and that any
"just resolve the path" version gets wrong:

* ``..`` walks and absolute paths;
* a SYMLINK inside the tree pointing out of it (the case a string-prefix check
  passes and a realpath check catches);
* a symlinked PARENT with a non-existent leaf;
* a sensitive file asked for by name, and the same file reached through a
  subdirectory.

Plus the policy itself: ``.env`` denied, ``.env.example`` allowed, credential
stores denied, noise hidden from listings but not treated as an error.
"""

from __future__ import annotations

import os

import pytest

from cli_agent_orchestrator.services.workspace_read import (
    CODE_BINARY,
    CODE_NOT_A_DIRECTORY,
    CODE_NOT_FOUND,
    CODE_OUTSIDE,
    CODE_SENSITIVE,
    IgnoreRules,
    Workspace,
    WorkspaceError,
    assert_exposable,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "junk").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "src" / "app.py").write_text("print('hello')\nprint('world')\n", encoding="utf-8")
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    (root / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    (root / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    (root / "src" / ".env").write_text("NESTED=secret\n", encoding="utf-8")
    (root / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (root / "node_modules" / "junk" / "index.js").write_text("x", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("not yours\n", encoding="utf-8")
    return Workspace(root)


# ── containment ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "../../etc/passwd", "/etc/passwd", "src/../../outside.txt"],
)
def test_paths_that_leave_the_workspace_are_refused(ws, path):
    with pytest.raises(WorkspaceError) as exc:
        ws.resolve(path)
    assert exc.value.code == CODE_OUTSIDE


def test_a_symlink_pointing_out_of_the_tree_is_refused(ws, tmp_path):
    """The case a string-prefix containment check passes: the requested path is
    inside the root, the FILE is not."""
    link = ws.root / "escape.txt"
    link.symlink_to(tmp_path / "outside.txt")
    with pytest.raises(WorkspaceError) as exc:
        ws.resolve("escape.txt")
    assert exc.value.code == CODE_OUTSIDE


def test_a_symlinked_parent_with_a_missing_leaf_is_refused(ws, tmp_path):
    """Canonicalizing the deepest EXISTING ancestor is what catches this: the
    leaf does not exist, so a strict realpath would fail open."""
    (ws.root / "linkdir").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceError) as exc:
        ws.resolve("linkdir/not-created-yet.txt")
    assert exc.value.code == CODE_OUTSIDE


def test_ordinary_paths_resolve_relative_to_the_root(ws):
    assert ws.resolve("src/app.py").rel == "src/app.py"
    assert ws.resolve(".").rel == ""
    assert ws.resolve("workspace:/src/app.py").rel == "src/app.py"  # alias echoed by a model
    assert ws.resolve("src\\app.py").rel == "src/app.py"  # windows-style input


def test_a_null_byte_is_not_a_path(ws):
    with pytest.raises(WorkspaceError):
        ws.resolve("src/app\0.py")


# ── the sensitive-file policy ────────────────────────────────────────────────


@pytest.mark.parametrize("path", [".env", "src/.env", "id_rsa"])
def test_sensitive_files_are_denied_by_name_and_in_subdirectories(ws, path):
    with pytest.raises(WorkspaceError) as exc:
        ws.read_file(path)
    assert exc.value.code == CODE_SENSITIVE


def test_the_example_env_is_explicitly_allowed(ws):
    """The negation rule matters: teams keep .env.example in the repo on
    purpose, and denying it makes the surface useless for onboarding
    questions."""
    assert ws.read_file(".env.example")["content"].startswith("OPENAI_API_KEY=")


def test_the_policy_covers_this_forks_own_credential_stores():
    rules = IgnoreRules()
    for name in (
        "providers.toml",
        "session-export.json",
        ".git-credentials",
        "credentials.json",
        "service-account-prod.json",
        "sudo_passwd.txt",
        "cluster.pem",
    ):
        assert rules.is_sensitive(name), name
    assert not rules.is_sensitive("pyproject.toml")
    assert not rules.is_sensitive("src/app.py")


def test_a_caoignore_adds_rules_without_replacing_the_builtins(tmp_path):
    root = tmp_path / "r"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "private.md").write_text("x", encoding="utf-8")
    (root / ".env").write_text("x", encoding="utf-8")
    (root / ".caoignore").write_text("notes/\n# a comment\n", encoding="utf-8")
    ws = Workspace(root)
    with pytest.raises(WorkspaceError):
        ws.read_file("notes/private.md")
    with pytest.raises(WorkspaceError):
        ws.read_file(".env")


# ── reads ────────────────────────────────────────────────────────────────────


def test_listing_hides_noise_but_keeps_real_files(ws):
    listing = ws.list_directory(".", depth=2)
    paths = [e["path"] for e in listing["entries"]]
    assert "README.md" in paths and "src/app.py" in paths
    assert not any(p.startswith("node_modules") or p.startswith(".git/") for p in paths)
    # Sensitive files are not merely hidden from reads — they never appear.
    assert ".env" not in paths and "id_rsa" not in paths


def test_listing_paginates(ws):
    first = ws.list_directory(".", depth=2, limit=2, offset=0)
    assert len(first["entries"]) == 2 and first["has_more"] is True
    second = ws.list_directory(".", depth=2, limit=2, offset=first["next_offset"])
    assert first["entries"] != second["entries"]


def test_reading_a_directory_or_a_missing_file_is_typed(ws):
    with pytest.raises(WorkspaceError) as exc:
        ws.list_directory("README.md")
    assert exc.value.code == CODE_NOT_A_DIRECTORY
    with pytest.raises(WorkspaceError) as exc:
        ws.read_file("nope.txt")
    assert exc.value.code == CODE_NOT_FOUND


def test_binary_files_are_refused_not_mangled(ws):
    (ws.root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(WorkspaceError) as exc:
        ws.read_file("blob.bin")
    assert exc.value.code == CODE_BINARY


def test_read_returns_a_line_window_with_totals(ws):
    (ws.root / "long.txt").write_text("\n".join(f"line{i}" for i in range(1, 1001)), "utf-8")
    page = ws.read_file("long.txt", start_line=10, end_line=12)
    assert page["content"] == "line10\nline11\nline12"
    assert page["total_lines"] == 1000 and page["truncated"] is True


def test_search_returns_paths_and_line_numbers_and_skips_sensitive_files(ws):
    (ws.root / "src" / "b.py").write_text("token = 'needle'\n", encoding="utf-8")
    (ws.root / ".env").write_text("NEEDLE=1\n", encoding="utf-8")
    result = ws.search("needle", limit=10)
    hits = {(m["path"], m["line"]) for m in result["matches"]}
    assert ("src/b.py", 1) in hits
    assert not any(p.endswith(".env") for p, _ in hits)


def test_search_supports_a_glob_and_refuses_a_too_short_query(ws):
    result = ws.search("print", glob="*.py")
    assert all(m["path"].endswith(".py") for m in result["matches"])
    with pytest.raises(WorkspaceError):
        ws.search("x")


def test_info_declares_the_surface_read_only(ws):
    info = ws.info()
    assert info["read_only"] is True
    assert info["sensitive_policy"] == "deny"
    assert "README.md" in info["top_level"]


def test_there_is_no_write_capability_on_the_surface():
    """Read-only BY CONSTRUCTION is the claim; this is the test that keeps it
    true as the module grows."""
    forbidden = {
        "write",
        "write_file",
        "patch",
        "apply",
        "run",
        "exec",
        "shell",
        "commit",
        "delete",
    }
    assert forbidden.isdisjoint({n for n in dir(Workspace) if not n.startswith("_")})


# ── the runner's exposure hook ───────────────────────────────────────────────


def test_the_bundle_exposure_hook_refuses_a_sensitive_file(tmp_path):
    """ "Attach this file" must not be a way around "you may not read .env"."""
    secret = tmp_path / ".env"
    secret.write_text("k=v", encoding="utf-8")
    with pytest.raises(WorkspaceError) as exc:
        assert_exposable(secret)
    assert exc.value.code == CODE_SENSITIVE
    ok = tmp_path / "bundle.txt"
    ok.write_text("x", encoding="utf-8")
    assert assert_exposable(ok) == ok


def test_the_runner_refuses_such_a_bundle_before_reading_it(tmp_path, monkeypatch):
    from cli_agent_orchestrator.chatgpt_web_runner import production
    from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError, RunnerErrorCode

    secret = tmp_path / ".env"
    secret.write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    with pytest.raises(RunnerError) as exc:
        production.run_production_review(
            task_text="review this",
            artifact_path=str(tmp_path / "a.md"),
            bundle_path=str(secret),
            callback=lambda _m: None,
            verify_pin=lambda _p: True,
            browser_turn=lambda: pytest.fail("must never reach the browser"),
        )
    assert exc.value.code is RunnerErrorCode.ACCESS_DENIED


def test_git_status_and_diff_are_read_only_and_typed(tmp_path):
    import subprocess

    root = tmp_path / "g"
    root.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig")}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    ws = Workspace(root)
    status = ws.git_status()
    assert "a.txt" in status["untracked"]
    assert ".env" not in status["untracked"]  # a NAME is a disclosure too
    diff = ws.git_diff(mode="unstaged")
    assert diff["mode"] == "unstaged" and "diff" in diff
    with pytest.raises(WorkspaceError):
        ws.git_diff(mode="rm -rf")
