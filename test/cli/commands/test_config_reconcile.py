"""Acceptance coverage for ``cao config reconcile`` and redeploy config reset."""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import config_reconcile as command
from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.utils import agent_profiles

_TEMPLATE = b'[codex]\nmodel = "gpt-current"\nreasoning_effort = "high"\n'


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_profile(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {name}\nprovider: codex\n---\nPrompt for {name}.\n",
        encoding="utf-8",
    )


@pytest.fixture
def reconcile_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    workspace = tmp_path / "workspace"
    profiles = workspace / "profiles"
    profiles.mkdir(parents=True)
    template = workspace / "providers.toml.default"
    template.write_bytes(_TEMPLATE)
    cao_home = tmp_path / "cao-home"
    store = cao_home / "agent-store"
    context = cao_home / "agent-context"
    settings = cao_home / "settings.json"

    monkeypatch.setattr(command, "CAO_HOME_DIR", cao_home)
    monkeypatch.setattr(command, "_workspace_root", lambda: workspace)
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", store)
    monkeypatch.setattr(settings_service, "CAO_HOME_DIR", cao_home)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings)
    monkeypatch.setattr(
        settings_service,
        "_DEFAULTS",
        {
            "kiro_cli": str(tmp_path / "kiro-agents"),
            "claude_code": str(store),
            "codex": str(store),
            "cao_installed": str(context),
        },
    )
    return {
        "workspace": workspace,
        "profiles": profiles,
        "template": template,
        "cao_home": cao_home,
        "target": cao_home / "providers.toml",
        "store": store,
        "context": context,
    }


def test_help_surfaces_and_plain_reconcile_preserves_existing_bytes(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "hand-edited"\n')
    before = _digest(target)

    config_help = CliRunner().invoke(cli, ["config", "reconcile", "--help"])
    redeploy_help = CliRunner().invoke(cli, ["redeploy", "--help"])
    result = CliRunner().invoke(cli, ["config", "reconcile"])

    assert config_help.exit_code == 0
    assert "--force-providers" in config_help.output
    assert "--audit-only" in config_help.output
    assert redeploy_help.exit_code == 0
    assert "--force-providers" in redeploy_help.output
    assert result.exit_code == 0
    assert _digest(target) == before
    assert not list(target.parent.glob("providers.toml.bak.*"))


def test_force_backs_up_old_bytes_and_publishes_template(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    old_bytes = b'[codex]\nmodel = "old"\n'
    target.write_bytes(old_bytes)
    monkeypatch.setattr(command, "_backup_timestamp", lambda: "20260725T120000Z")

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    backup = target.with_name("providers.toml.bak.20260725T120000Z")
    assert result.exit_code == 0
    assert backup.read_bytes() == old_bytes
    assert target.read_bytes() == reconcile_env["template"].read_bytes()


def test_backup_collision_uses_dash_two_without_overwriting(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    old_bytes = b'[codex]\nmodel = "old"\n'
    target.write_bytes(old_bytes)
    monkeypatch.setattr(command, "_backup_timestamp", lambda: "20260725T120000Z")
    first = target.with_name("providers.toml.bak.20260725T120000Z")
    first.write_bytes(b"keep me")

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code == 0
    assert first.read_bytes() == b"keep me"
    assert first.with_name(f"{first.name}-2").read_bytes() == old_bytes


def test_malformed_template_fails_before_backup_or_live_mutation(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)
    reconcile_env["template"].write_text("[codex\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code != 0
    assert "invalid providers template" in result.output
    assert _digest(target) == before
    assert not list(target.parent.glob("providers.toml.bak.*"))


def test_replace_failure_leaves_live_file_whole_and_cleans_temp(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)

    def fail_replace(source, destination):
        raise OSError("publish failed")

    monkeypatch.setattr(command.os, "replace", fail_replace)
    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    source = inspect.getsource(command)
    assert result.exit_code != 0
    assert _digest(target) == before
    assert not list(target.parent.glob("providers.toml.*.tmp"))
    assert "shutil.copyfile" not in source


def test_force_with_absent_target_is_an_ordinary_seed_without_backup(reconcile_env):
    target = reconcile_env["target"]

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code == 0
    assert target.read_bytes() == reconcile_env["template"].read_bytes()
    assert not list(target.parent.glob("providers.toml.bak.*"))


def test_orphan_audit_reports_context_and_store_without_deleting(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(_TEMPLATE)
    context_orphan = reconcile_env["context"] / "context_orphan.md"
    store_orphan = reconcile_env["store"] / "store_orphan.md"
    _write_profile(context_orphan, "context_orphan")
    _write_profile(store_orphan, "store_orphan")

    result = CliRunner().invoke(cli, ["config", "reconcile"])

    assert result.exit_code == 0
    assert f"orphan profile context_orphan in {reconcile_env['context']}" in result.stderr
    assert f"orphan profile store_orphan in {reconcile_env['store']}" in result.stderr
    assert "orphan profile developer" not in result.stderr
    assert "orphan profile reviewer" not in result.stderr
    assert context_orphan.exists()
    assert store_orphan.exists()


def test_dead_stanza_warns_but_loadable_builtin_stanza_does_not(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_text(
        '[codex]\nmodel = "gpt-current"\n'
        '[codex.profiles.does_not_exist]\nreasoning_effort = "high"\n'
        '[codex.profiles.developer]\nreasoning_effort = "high"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["config", "reconcile"])

    assert result.exit_code == 0
    assert "[codex.profiles.does_not_exist] names no known profile" in result.stderr
    assert "[codex.profiles.developer] names" not in result.stderr


def test_template_drift_names_changed_key_and_identical_mapping_is_quiet(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "old"\nreasoning_effort = "high"\n')

    drifted = CliRunner().invoke(cli, ["config", "reconcile"])
    target.write_bytes(b"# hand comment\n" + _TEMPLATE)
    identical = CliRunner().invoke(cli, ["config", "reconcile"])

    assert drifted.exit_code == 0
    assert "providers.toml differs at codex.model" in drifted.stderr
    assert "--force-providers" in drifted.stderr
    assert identical.exit_code == 0
    assert "providers.toml differs" not in identical.stderr


def test_sandbox_guard_runs_before_reconcile_or_mutation(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)
    monkeypatch.setenv("CAO_INSTANCE_ID", "sandbox-test")

    def mutation_sink(*args, **kwargs):
        pytest.fail("reconcile reached before sandbox guard")

    monkeypatch.setattr(command, "_reconcile_config", mutation_sink)
    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code != 0
    assert "sandbox mutation forbidden" in str(result.exception)
    assert _digest(target) == before
    assert not list(target.parent.glob("providers.toml.bak.*"))


#: Shell/text-processing verbs that would mean the installer is *reading into*
#: a TOML document rather than moving its bytes around.
_TEXT_PROCESSORS = frozenset(
    {
        "grep",
        "egrep",
        "fgrep",
        "sed",
        "awk",
        "gawk",
        "cut",
        "tr",
        "head",
        "tail",
        "sort",
        "uniq",
        "xargs",
        "eval",
        "read",
        "source",
    }
)

#: Verbs that would mean the installer *writes* the file it names. The
#: reconcile command owns providers.toml end to end (seed, backup, publish);
#: install.sh may name it in a message or an existence test, never author it.
#: ``install`` is deliberately absent — every warning line here is prefixed
#: ``[install]``.
_FILE_MUTATORS = frozenset(
    {"cp", "mv", "rm", "tee", "touch", "ln", "dd", "truncate", "mktemp", "chmod"}
)

#: TOML readers. Any of these anywhere in the script (shell *or* an embedded
#: python heredoc) means install.sh re-grew the parser the fork already owns.
_TOML_PARSERS = ("tomllib", "tomlkit", "import toml", "from toml", "toml.load")

_ASSIGN_RE = re.compile(
    r"^\s*(?:local\s+|export\s+|declare\s+(?:-\w+\s+)*|typeset\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)
_FOR_IN_RE = re.compile(r"^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.*)$")
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_CMD_SUB_RE = re.compile(r"\$\(|`")
#: A redirection and its target word: ``> f``, ``>>"$f"``, ``2>/dev/null``.
_REDIRECT_RE = re.compile(r">>?\s*(\"[^\"]*\"|'[^']*'|[^\s|&;<>]+)")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def _taint_toml_vars(
    code_lines: "list[tuple[int, str]]",
) -> "dict[int, dict[str, bool]]":
    """Single forward pass marking shell variables that *hold a TOML path*.

    Returns ``{lineno: {variable: names_providers_toml}}`` — the taint state as it
    stands when that line EXECUTES, not one final map. A variable is tainted when it
    is assigned a word containing ``.toml`` (``f="$d/_clauses.toml"``), or
    assigned from another tainted variable (``g="$f"``); a ``for x in *.toml``
    header taints the loop variable the same way. The flag distinguishes
    providers.toml handles from the (legitimately copied) ``_clauses.toml`` /
    ``routing.toml`` ones, so check (3) can stay pointed at providers.toml.

    Per-line snapshots, not one final map (the N4 clear-after-use hole): with a
    single return value a later harmless reassignment (``_n4="$HOME/README.md"``
    after ``_n4=…/providers.toml``) rewrote history for every EARLIER line, so
    ``awk … "$_n4"`` between the two assignments read as untainted and was
    accepted. Each line is now judged against the taint state at that line.

    Deliberately NOT a shell parser. Known blind spots, each one a shape the
    guard below cannot see (N4-class holes, named rather than left silent):

    * command substitution — ``x=$(...)`` captures a command's *output*, not a
      path, so it clears rather than spreads taint. That is what keeps the real
      installer's ``_found=$(… "$_live")`` / ``printf … | sed`` warn block legal,
      and it also breaks the chain at ``name=$(basename "$src")``: the derived
      ``dst`` is untainted, so the herdr stage's ``grep -qF "$MARKER" "$dst"``
      marker probe is not flagged. A mutant hiding a scrape behind
      ``v=$(basename "$t"); awk … "$d/$v"`` would slip through.
    * shell arrays (``a=(x.toml)``/``${a[@]}``), ``read``-into-variable
      (``read -r f < list.toml``), positional parameters inside a function
      (``_src="$1"``), and indirect expansion (``${!name}``/``eval``).
    * heredoc bodies: an embedded interpreter is caught only by the TOML-reader
      token check (2). Neither the shell-verb check (4) nor the write check (3)
      reads python, so ``open(".../providers.toml", "w")`` inside a heredoc is
      invisible to them.
    * one forward pass in text order: a variable used above its assignment
      (a function body called later) is not tracked.
    """
    tainted: "dict[str, bool]" = {}
    snapshots: "dict[int, dict[str, bool]]" = {}
    for _lineno, raw in code_lines:
        # Snapshot BEFORE this line's own assignment is applied: the line is a
        # *use* of whatever the earlier lines established, so a self-referencing
        # write (``f="$f"``) sees the value it read, exactly as the shell would.
        snapshots[_lineno] = dict(tainted)
        match = _ASSIGN_RE.match(raw) or _FOR_IN_RE.match(raw)
        if not match:
            continue
        name, rhs = match.group(1), match.group(2)
        lowered_rhs = rhs.lower()
        if ".toml" in lowered_rhs:
            tainted[name] = "providers.toml" in lowered_rhs
        elif _CMD_SUB_RE.search(rhs):
            tainted.pop(name, None)
        else:
            inherited = [v for v in _VAR_REF_RE.findall(rhs) if v in tainted]
            if inherited:
                tainted[name] = any(tainted[v] for v in inherited)
            else:
                tainted.pop(name, None)
    return snapshots


def _assert_installer_delegates(contents: str) -> None:
    """Four structural checks for F63 criterion 11 — see the test docstring."""
    lowered = contents.lower()

    # Comment lines may describe the delegation freely; only executable lines
    # are constrained.
    code_lines = [
        (n, raw)
        for n, raw in enumerate(contents.splitlines(), start=1)
        if not raw.strip().startswith("#")
    ]
    code = "\n".join(raw for _, raw in code_lines).lower()

    # (1) Exactly one delegation point (executable lines; the F937 warn block
    #     explains reconcile's seeding rules in prose above it).
    assert code.count("cao config reconcile") == 1, (
        "install.sh must call `cao config reconcile` exactly once "
        f"(found {code.count('cao config reconcile')} executable mentions)"
    )

    # (2) No TOML reader is constructed anywhere — shell or embedded heredoc.
    for parser in _TOML_PARSERS:
        assert parser not in lowered, (
            f"install.sh re-grew a TOML parser ({parser!r}); "
            "D6 delegates that to `cao config reconcile`"
        )

    # The taint state AT EACH LINE (see _taint_toml_vars): a later reassignment
    # must not retroactively un-taint an earlier use, which is the N4
    # clear-after-use hole (``_n4=…/providers.toml`` … ``awk … "$_n4"`` …
    # ``_n4="$HOME/README.md"``).
    tainted_at = _taint_toml_vars(code_lines)

    # (4a) No provider-stanza reasoning anywhere in executable text.
    assert ".profiles." not in code, "install.sh reasons about provider stanzas"

    for lineno, raw in code_lines:
        lowered_line = raw.lower()
        tainted = tainted_at.get(lineno, {})
        refs = {v for v in _VAR_REF_RE.findall(raw) if v in tainted}
        if "toml" not in lowered_line and not refs:
            continue
        tokens = {tok.lower() for tok in _WORD_RE.findall(raw)}

        # (4b) No text-processing pointed at a TOML path — named literally on
        #      this line, or reached through a variable holding one (the N4
        #      indirection: `f="$d/x.toml"` on one line, `awk … "$f"` on the
        #      next). Every executable `toml` touch must be a plain file op.
        offenders = sorted(tokens & _TEXT_PROCESSORS)
        assert not offenders, (
            f"install.sh:{lineno} text-processes a TOML file with {offenders}: "
            f"{raw.strip()!r} — parsing belongs to `cao config reconcile`"
        )

        # (3) providers.toml is the reconcile command's file: the installer may
        #     test for it and name it in a warning, but never seeds, diffs,
        #     backs up, copies or writes it.
        if "providers.toml" not in lowered_line and not any(tainted[v] for v in refs):
            continue
        mutators = sorted(tokens & _FILE_MUTATORS)
        assert not mutators, (
            f"install.sh:{lineno} writes providers.toml with {mutators}: "
            f"{raw.strip()!r} — that file is owned by `cao config reconcile`"
        )
        provider_vars = [f"${v}" for v in refs if tainted[v]]
        provider_vars += [f"${{{v}}}" for v in refs if tainted[v]]
        for target in _REDIRECT_RE.findall(raw):
            lowered_target = target.lower()
            hit = "providers.toml" in lowered_target or any(var in target for var in provider_vars)
            assert not hit, (
                f"install.sh:{lineno} redirects into providers.toml: "
                f"{raw.strip()!r} — that file is owned by `cao config reconcile`"
            )


def _real_installer_text() -> str:
    from test.conftest import ROOT_REPO

    if ROOT_REPO is None:
        pytest.skip("root repo not found (worktree without .git context)")
    return (ROOT_REPO / "install.sh").read_text(encoding="utf-8")


def test_root_installer_delegates_without_toml_or_stanza_parsing():
    """F63 criterion 11 (structural): the root installer *delegates* provider
    config reconciliation instead of re-growing its own TOML reader.

    D6's rationale: D5(b)/(c) need TOML parsing plus a key-level semantic diff,
    which POSIX ``sh`` cannot express, and each workaround (an inline
    ``python3 -c`` parser, ``grep``-based stanza scraping, a ``uv run`` shim
    around a hand-rolled reader) either breaks ``install.sh``'s contract or
    re-derives a parser the fork already owns. So the pin is: exactly one
    ``cao config reconcile`` call, no TOML parser anywhere in the script, no
    stanza/text-processing aimed at a ``.toml`` file — directly or through a
    variable holding one — and no writing of providers.toml.

    Copying a TOML file byte-for-byte is NOT parsing. Since 2026-09-11 the
    installer syncs ``profiles/<sub>/_clauses.toml`` (F613 #469 — without it
    every routing-driven assign fails "clause table not found") and
    ``orchestrator/routing.toml`` into the agent store via ``cp``/``mv``; the
    shell never interprets those bytes. The original blanket
    ``"toml" not in contents`` was a stale proxy for the invariant above and is
    replaced by the four checks in :func:`_assert_installer_delegates`, which
    still fail on every workaround D6 rejected —
    :func:`test_installer_guard_rejects_parsing_mutants` is the receipt.

    Check (3) reads "never writes providers.toml", not "never names it": the
    F937 warn block legitimately builds the live path, tests it with ``[ -f ]``
    and prints its name in the remediation message. Every *use* of that path is
    still constrained by (4) via the taint pass, so a scrape hidden behind the
    variable is rejected.
    """
    _assert_installer_delegates(_real_installer_text())


#: Workarounds D6 rejected, each appended to the real installer. The guard is
#: only worth its line count if these are rejected, so they run as a test.
_INSTALLER_MUTANTS = {
    "n4_variable_indirection": (
        '_f="$REPO_DIR/profiles/positions/_clauses.toml"\n'
        "awk -F'=' '/^\\[/ {print $1}' \"$_f\"\n"
    ),
    "n4_two_hop_indirection": (
        '_a="$REPO_DIR/orchestrator/routing.toml"\n_b="$_a"\nhead -n 20 "$_b"\n'
    ),
    "n4_taint_cleared_after_use": (
        '_n4="$HOME/.aws/cli-agent-orchestrator/providers.toml"\n'
        "awk -F= '{print $1}' \"$_n4\"\n"
        '_n4="$HOME/README.md"\n'
    ),
    "n4_loop_glob_indirection": (
        'for _t in "$CAO_STORE_DIR"/*.toml; do\n    sed -n "1p" "$_t"\ndone\n'
    ),
    "inline_python_tomllib_parser": (
        "python3 -c 'import tomllib,sys;"
        ' print(tomllib.load(open(sys.argv[1], "rb")))\''
        ' "$HOME/.aws/cli-agent-orchestrator/providers.toml"\n'
    ),
    "grep_stanza_scrape": (
        "grep '^\\[codex.profiles.' \"$HOME/.aws/cli-agent-orchestrator/providers.toml\""
        " | cut -d= -f2\n"
    ),
    "sed_on_template": ('sed -n "/^\\[codex\\]/,/^\\[/p" "$REPO_DIR/providers.toml.default"\n'),
    "duplicate_reconcile": '(cd "$REPO_DIR" && cao config reconcile)\n',
    "seed_providers_toml": (
        'cp "$REPO_DIR/providers.toml.default" '
        '"$HOME/.aws/cli-agent-orchestrator/providers.toml"\n'
    ),
    "redirect_into_providers_toml": (
        '_lp="$HOME/.aws/cli-agent-orchestrator/providers.toml"\n'
        "printf '[codex]\\n' > \"$_lp\"\n"
    ),
}


@pytest.mark.parametrize("mutant", sorted(_INSTALLER_MUTANTS))
def test_installer_guard_rejects_parsing_mutants(mutant: str) -> None:
    """Non-vacuity: the guard rejects every shape D6 outlawed, including the
    variable-indirection scrape that defeats a line-local check."""
    mutated = _real_installer_text() + "\n" + _INSTALLER_MUTANTS[mutant]
    with pytest.raises(AssertionError):
        _assert_installer_delegates(mutated)


def test_backup_failure_aborts_before_atomic_publish(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)
    publish_calls = []

    def fail_backup(_target):
        raise OSError("backup failed")

    monkeypatch.setattr(command, "_write_backup", fail_backup)
    monkeypatch.setattr(
        command,
        "_atomic_publish",
        lambda *args, **kwargs: publish_calls.append((args, kwargs)),
    )

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code != 0
    assert "config reconcile failed: backup failed" in result.output
    assert publish_calls == []
    assert _digest(target) == before


def test_read_only_cao_home_aborts_without_live_mutation(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)
    (target.parent / "providers.toml.lock").touch()
    target.parent.chmod(0o500)

    try:
        result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])
    finally:
        target.parent.chmod(0o700)

    assert result.exit_code != 0
    assert _digest(target) == before


def test_live_flock_contender_fails_without_touching_target(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    before = _digest(target)
    lock_path = target.parent / "providers.toml.lock"

    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code != 0
    assert "another redeploy holds the config lock" in result.output
    assert _digest(target) == before


def test_flock_is_released_when_holder_is_sigkilled(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "live"\n')
    lock_path = target.parent / "providers.toml.lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, signal, sys; "
                "stream=open(sys.argv[1], 'a+b'); "
                "fcntl.flock(stream.fileno(), fcntl.LOCK_EX); "
                "print('ready', flush=True); signal.pause()"
            ),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        child.kill()
        child.wait(timeout=5)
        result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    assert result.exit_code == 0
    assert target.read_bytes() == _TEMPLATE
    assert "os.kill" not in inspect.getsource(command)


def test_unloadable_and_unknown_stanzas_are_reported_distinctly(reconcile_env):
    broken = reconcile_env["context"] / "broken.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("---\nname: [broken\n---\n", encoding="utf-8")
    target = reconcile_env["target"]
    target.write_text(
        '[codex]\nmodel = "gpt-current"\n'
        '[codex.profiles.broken]\nreasoning_effort = "high"\n'
        '[codex.profiles.missing]\nreasoning_effort = "high"\n'
        '[codex.profiles.developer]\nreasoning_effort = "high"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["config", "reconcile"])

    assert result.exit_code == 0
    assert "[codex.profiles.broken] names a profile that fails to load" in result.stderr
    assert "[codex.profiles.missing] names no known profile" in result.stderr
    assert "[codex.profiles.developer] names" not in result.stderr


def test_builtin_context_shadow_warns_but_nonbuiltin_duplicate_does_not(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(_TEMPLATE)
    _write_profile(reconcile_env["context"] / "developer.md", "developer")
    _write_profile(reconcile_env["store"] / "owned.md", "owned")
    _write_profile(reconcile_env["context"] / "owned.md", "owned")
    _write_profile(reconcile_env["profiles"] / "owned.md", "owned")

    result = CliRunner().invoke(cli, ["config", "reconcile"])

    assert result.exit_code == 0
    assert "SHADOW profile developer" in result.stderr
    assert str(reconcile_env["context"]) in result.stderr
    assert "orphan profile developer" not in result.stderr
    assert "SHADOW profile owned" not in result.stderr


def test_force_diff_is_emitted_before_atomic_replace(reconcile_env, monkeypatch):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[codex]\nmodel = "old"\nreasoning_effort = "high"\n')
    events: list[str] = []
    original_emit = command._emit_template_drift
    original_replace = command.os.replace

    def record_diff(*args, **kwargs):
        original_emit(*args, **kwargs)
        events.append("diff")

    def record_replace(source, destination):
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(command, "_emit_template_drift", record_diff)
    monkeypatch.setattr(command.os, "replace", record_replace)
    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code == 0
    assert "providers.toml differs at codex.model" in result.stderr
    assert events == ["diff", "replace"]


def test_audit_only_never_seeds_or_takes_the_writer_lock(reconcile_env):
    lock_path = reconcile_env["cao_home"] / "providers.toml.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = CliRunner().invoke(
            cli,
            ["config", "reconcile", "--audit-only", "--force-providers"],
        )

    assert result.exit_code == 0
    assert "providers.toml is missing" in result.stderr
    assert not reconcile_env["target"].exists()


def test_force_preserves_symlink_and_publishes_to_resolved_target(reconcile_env):
    target = reconcile_env["target"]
    target.parent.mkdir(parents=True)
    real_target = reconcile_env["workspace"] / "dotfiles" / "providers.toml"
    real_target.parent.mkdir(parents=True)
    real_target.write_bytes(b'[codex]\nmodel = "old"\n')
    target.symlink_to(real_target)

    result = CliRunner().invoke(cli, ["config", "reconcile", "--force-providers"])

    assert result.exit_code == 0
    assert target.is_symlink()
    assert real_target.read_bytes() == _TEMPLATE
    assert list(real_target.parent.glob("providers.toml.bak.*"))
