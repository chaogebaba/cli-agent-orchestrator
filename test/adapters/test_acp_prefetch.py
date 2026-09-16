"""AC-S1.20 — the three ``npx -y`` adapters start from vendored artefacts.

The AC's live arm (npm cache cleared, network unavailable at spawn) belongs to
Phase B's box round.  What is provable here, and what the live arm cannot prove
on its own, is the MECHANISM: that a resolved argv names a real file and never
``npx``, that a missing vendor tree is a typed refusal rather than a silent
fallback, and that the versions are pinned rather than floated.

The fallback arm matters most.  A fallback to ``npx`` would pass the live arm on
any machine with a warm cache — which is exactly the machine S0 ran on, and
exactly why AC-S0.7's PASS carried a caveat instead of closing the question.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.acp.prefetch import (
    NPX_ADAPTERS,
    AdapterUnavailable,
    prefetch_adapters,
    resolve_adapter_argv,
    vendor_root,
)


@pytest.fixture
def vendored(tmp_path: Path) -> dict[str, str]:
    """A vendor tree with all three adapters present."""
    env = {"CAO_HOME_DIR": str(tmp_path)}
    root = vendor_root(environ=env)
    for spec in NPX_ADAPTERS:
        entry = root / "node_modules" / spec.package / spec.entry
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// vendored\n")
    return env


@pytest.mark.parametrize("spec", NPX_ADAPTERS, ids=lambda s: s.name)
def test_a_resolved_argv_names_a_real_file_and_never_npx(
    spec: object, vendored: dict[str, str]
) -> None:
    argv = resolve_adapter_argv(spec.name, environ=vendored, node="/usr/bin/node")  # type: ignore[attr-defined]
    assert "npx" not in " ".join(argv)
    # ``resolve()`` follows the symlink, so the argv names the real interpreter
    # (``node-22`` on this box) rather than a link that a package upgrade can
    # repoint underneath a running server.
    assert Path(argv[0]).is_absolute()
    assert Path(argv[0]).name.startswith("node")
    assert Path(argv[1]).exists()


def test_the_node_binary_is_absolute(vendored: dict[str, str]) -> None:
    """``cao-server`` runs as a systemd user service whose PATH is not a login
    shell's.  A bare ``node`` fails in production while passing every test run
    from a terminal, which the fork has already been bitten by."""
    argv = resolve_adapter_argv("claude-acp", environ=vendored, node="/usr/bin/node")
    assert Path(argv[0]).is_absolute()


def test_a_missing_vendor_tree_is_a_typed_refusal_not_a_fallback(tmp_path: Path) -> None:
    """The arm that the live round cannot run.

    A fallback would pass a cache-cleared live arm on any machine whose cache
    refilled during the test, and would reintroduce the hang everywhere else.
    """
    with pytest.raises(AdapterUnavailable) as raised:
        resolve_adapter_argv("claude-acp", environ={"CAO_HOME_DIR": str(tmp_path)})
    message = str(raised.value)
    assert "not vendored" in message
    assert "install.sh" in message, "the refusal must carry its own remediation"


def test_a_partially_vendored_tree_refuses_the_missing_one(
    tmp_path: Path, vendored: dict[str, str]
) -> None:
    """Per adapter, not per tree: two working adapters must not vouch for a third."""
    root = vendor_root(environ=vendored)
    (root / "node_modules" / NPX_ADAPTERS[1].package / NPX_ADAPTERS[1].entry).unlink()
    assert resolve_adapter_argv(NPX_ADAPTERS[0].name, environ=vendored, node="/usr/bin/node")
    with pytest.raises(AdapterUnavailable):
        resolve_adapter_argv(NPX_ADAPTERS[1].name, environ=vendored, node="/usr/bin/node")


def test_an_unknown_adapter_name_is_refused(vendored: dict[str, str]) -> None:
    with pytest.raises(AdapterUnavailable, match="no vendored"):
        resolve_adapter_argv("not-an-adapter", environ=vendored)


def test_the_versions_are_pinned_not_floated() -> None:
    """D13 certifies per ``adapter package@version``.  A floating spec would
    invalidate a certification row nobody had touched."""
    pinned = {spec.name: spec.version for spec in NPX_ADAPTERS}
    assert pinned["claude-acp"] == "0.76.0"
    assert pinned["codex-acp"] == "1.11.0"
    # pi-acp WAS ``latest`` while its certification row said 0.0.33 — a pin that
    # could not hold the row it keys, which the S1 review caught (S5). It is now
    # the version the row was measured against.
    assert pinned["pi-acp"] == "0.0.33"


def test_no_adapter_floats() -> None:
    """The general form of the rule, so a fourth adapter cannot slip in loose.

    D13 keys certification on ``adapter package@version``. A floating spec
    invalidates a row nobody touched, and the failure is silent: the row still
    names a version, and the thing that starts is a different one.
    """
    floating = {spec.name: spec.version for spec in NPX_ADAPTERS if spec.version == "latest"}
    assert (
        not floating
    ), f"these adapters float and their certification rows cannot hold: {floating}"


def test_prefetch_installs_all_three_into_the_vendor_root(tmp_path: Path) -> None:
    """The command shape, asserted without a network."""
    calls: list[list[str]] = []

    def runner(command: list[str], **_: object) -> None:
        calls.append(command)

    env = {"CAO_HOME_DIR": str(tmp_path)}
    specs = prefetch_adapters(environ=env, runner=runner)
    assert len(calls) == 1
    command = calls[0]
    assert command[1] == "install"
    assert "--prefix" in command
    assert command[command.index("--prefix") + 1] == str(vendor_root(environ=env))
    for spec in NPX_ADAPTERS:
        assert spec.spec in command
        assert spec.spec in specs


def test_the_vendor_root_lives_with_caos_state(tmp_path: Path) -> None:
    """Not a cache directory: something else may clear a cache, and the whole
    point is that this survives a cache wipe."""
    root = vendor_root(environ={"CAO_HOME_DIR": str(tmp_path)})
    assert root == tmp_path / "acp-adapters"
    assert "cache" not in str(root)


def test_no_module_in_the_plane_builds_an_npx_argv() -> None:
    """The grep-checkable half: ``npx`` appears in the plane only as the thing
    being removed, never as a command someone builds."""
    import ast

    import cli_agent_orchestrator

    src = Path(cli_agent_orchestrator.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted((src / "adapters" / "acp").rglob("*.py")):
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and getattr(node, "body", None)
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                # A COMMAND, not a mention: the refusal message names ``npx`` to
                # explain what is deliberately not being done, and forbidding the
                # explanation would leave only the rule.
                and (node.value == "npx" or node.value.startswith("npx "))
            ):
                offenders.append(f"{path.name}: {node.value[:60]}")
    assert not offenders, "an npx argv is being built in the plane:\n" + "\n".join(offenders)
