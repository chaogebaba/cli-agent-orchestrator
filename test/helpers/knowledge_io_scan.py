"""AST scan for infrastructure code reaching a skill-owned knowledge path.

Internal test helper, not a runtime component and not a release gate. It is the
``knowledge-io`` half of ``scripts/lite_boundary.py`` on ``cao/lite-boundary-slice1``
(@ ``eb1cf2c0``), retained per ``wp-arch-modular-core.md`` B/slice 1 and reworked to
assert ZERO findings rather than to ratchet a debt count.

Deliberately narrower than the branch it came from. Only the ``knowledge-io`` rule is
kept, because AC-LITE-2 is exactly "no non-test module opens/stats/globs/parent-walks
a path in the A.2 closed list". The branch's other two rules are dropped:
``reverse-import`` fired on ``chatgpt_web_runner.orchestrator`` — a module named for
ChatGPT-web run sequencing, verified in A.1 to read no knowledge path, i.e. a keyword
false positive — and ``audit-activation`` has nothing to gate under thin lite (A.5,
"No slice for a self-audit activation gate").

Also dropped with the amendment: the ``_CONTENT_DOMAIN`` prose scan over source data
files (it flags test fixtures containing the string "GOLDEN-TIPS"), the ``.claude/*.json``
configuration scan (this checkout's hooks are dev config and are meant to be
audit-bearing), and the rc-1 ``boundary_achieved: false`` contract.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# The A.2 closed list, as a path/filename matcher. Adding to it is a blueprint edit.
#
# `routing.toml` is deliberately ABSENT: the copy inside CAO's own agent store
# (`constants.routing_toml_path()`) is infrastructure, and only `<repo>/orchestrator/
# routing.toml` is skill-owned — which the `orchestrator/` segment already catches.
# Directory segments, case-insensitively.
_DIR_DOMAIN = re.compile(
    r"(?:^|[/\\])(?:orchestrator|doctrine|blueprints)(?:[/\\]|$)",
    re.IGNORECASE,
)

# The named files, case-SENSITIVELY, and only as a whole path component.
#
# Case matters here because A.2 clarifies that `docusaurus/docs/patterns/handoff.md` is a
# generic product doc and "the word 'handoff' is not classification". The skill's own files
# are SHOUTED by convention (HANDOFF.md, GOLDEN-TIPS.md, BUGS.md, ...), the product doc is
# not, and that is the only signal available in the path. A case-insensitive match here
# reported the docusaurus page as packaged knowledge — measured, not hypothesised.
_FILE_DOMAIN = re.compile(
    r"(?:^|[/\\])(?:ORCH_MAP|HANDOFF|WP-BACKLOG|BUGS|MISTAKES|GOLDEN-TIPS|ROUTING)\.md$"
    r"|(?:^|[/\\])self-audit\.md$"
)

# Read, stat, glob and parent-walk surfaces. `find_workspace_file` is CAO's own
# parent-walking helper and is the one that made `cao ledger` skill-coupled.
_IO = {
    "open",
    "read_text",
    "read_bytes",
    "exists",
    "is_file",
    "is_dir",
    "stat",
    "lstat",
    "glob",
    "rglob",
    "iterdir",
    "find_workspace_file",
    "open_text",
    "open_binary",
    "get_data",
    "files",
    "resource_filename",
    "resource_stream",
    "write_text",
    "write_bytes",
    "mkdir",
    "makedirs",
    "access",
    "listdir",
}

# Modules the skill owns. They are reached only through the separate
# `cao-orchestrator` console script, so they are free to read the A.2 list (A.4).
SKILL_ALLOWLIST = (
    "src/cli_agent_orchestrator/cli/orchestrator_main.py",
    "src/cli_agent_orchestrator/cli/orchestrator_commands/__init__.py",
    "src/cli_agent_orchestrator/cli/orchestrator_commands/ledger.py",
    "src/cli_agent_orchestrator/cli/orchestrator_commands/lint_doctrine.py",
    "src/cli_agent_orchestrator/cli/orchestrator_commands/fold_corpus.py",
    "src/cli_agent_orchestrator/cli/orchestrator_commands/sync_routing.py",
)


@dataclass(frozen=True, order=True)
class Finding:
    location: str
    rule: str
    evidence: str


def knowledge_domain(value: str) -> bool:
    """True when ``value`` names a path on the A.2 closed list."""
    normalized = value.replace("\\", "/")
    return bool(_DIR_DOMAIN.search(normalized) or _FILE_DOMAIN.search(normalized))


def _body(nodes: Iterable[ast.stmt]) -> list[ast.stmt]:
    """Drop docstrings: a historical citation in prose is not I/O."""
    return [
        node
        for node in nodes
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def _value(node: ast.AST, values: dict[str, str]) -> str:
    """Best-effort constant folding over str literals, ``/`` joins and f-strings."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id, "")
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
        left, right = _value(node.left, values), _value(node.right, values)
        return left + ("/" if isinstance(node.op, ast.Div) else "") + right
    if isinstance(node, ast.JoinedStr):
        return "".join(
            _value(part.value if isinstance(part, ast.FormattedValue) else part, values)
            for part in node.values
        )
    if isinstance(node, ast.Call) and _name(node.func).split(".")[-1] in {"Path", "join"}:
        return "/".join(_value(arg, values) for arg in node.args)
    return ""


def scan_python(source: str, location: str) -> list[Finding]:
    """Report every scope in ``source`` that performs I/O against an A.2 path."""
    tree = ast.parse(source, filename=location)
    # `from pathlib import Path as P` / `from os.path import exists as e` style aliases,
    # so renaming the call is not an escape hatch.
    io_aliases = {
        alias.asname: alias.name
        for imported in ast.walk(tree)
        if isinstance(imported, ast.ImportFrom)
        for alias in imported.names
        if alias.asname and alias.name in _IO
    }

    def io_call(node: ast.Call) -> str:
        name = _name(node.func).split(".")[-1]
        return io_aliases.get(name, name)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = _body(node.body)

    # Fixed-point over assignments so a path built across several statements still folds.
    values: dict[str, str] = {}
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = _value(node.value, values) if node.value else ""
            for target in targets:
                if isinstance(target, ast.Name) and value and values.get(target.id) != value:
                    values[target.id] = value
                    changed = True
        if not changed:
            break

    findings: set[Finding] = set()
    scopes = [("<module>", tree.body)] + [
        (node.name, node.body)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope, body in scopes:
        # Module scope excludes nested definitions so a finding names its real owner.
        roots = [
            node
            for node in body
            if scope != "<module>"
            or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        nodes = [node for root in roots for node in ast.walk(root)]
        calls = sorted(
            {io_call(node) for node in nodes if isinstance(node, ast.Call) and io_call(node) in _IO}
        )
        domain = sorted(
            {_value(node, values) for node in nodes if knowledge_domain(_value(node, values))}
        )
        if calls and domain:
            findings.add(
                Finding(f"{location}:{scope}", "knowledge-io", f"{','.join(calls)} -> {domain[0]}")
            )
    return sorted(findings)


def scan_source_tree(root: Path, allowlist: Iterable[str] = SKILL_ALLOWLIST) -> list[Finding]:
    """Scan every non-test module under ``root/src``, minus the skill-owned allowlist."""
    allowed = set(allowlist)
    findings: list[Finding] = []
    for path in sorted((root / "src").rglob("*.py")):
        location = path.relative_to(root).as_posix()
        if location in allowed:
            continue
        findings.extend(scan_python(path.read_text(encoding="utf-8"), location))
    return sorted(set(findings))


# ---------------------------------------------------------------------------------------
# Artifact scan (AC-LITE-5, slice 3). Retained from `cao/lite-boundary-slice1` @ eb1cf2c0
# (`scan_members`/`scan_artifact`, :278-330).
# ---------------------------------------------------------------------------------------

# The prose scan. It is deliberately NOT applied to source or to test fixtures — there it
# flags any file containing the string "GOLDEN-TIPS" (three `status_truth` fixtures,
# `test/providers/fixtures/f568/spinner-ebbing-bare.txt`, `test/tier-census.json`) and is
# pure noise. Inside a built artifact's PACKAGE DATA it earns its place: a packaged prompt
# renamed to slip past a path check is a real way to ship doctrine in a wheel.
_CONTENT_DOMAIN = re.compile(
    r"self[-_ ]?audit|compliance[-_ ]auditor|orchestration[-_ ]knowledge"
    r"|workflow[-_ ]ledger|golden[-_ ]tips|orchestrator doctrine",
    re.IGNORECASE,
)

_DATA_SUFFIXES = (".md", ".txt", ".json", ".toml", ".sh")


def scan_members(
    members: Iterable[tuple[str, bytes]],
    surface: str,
    *,
    content_roots: tuple[str, ...] = ("src/", "cli_agent_orchestrator/", "cao_workflow/"),
) -> list[Finding]:
    """Report packaged knowledge in an archive's members.

    Two rules. ``packaged-knowledge`` is the path check over every member. ``knowledge-content``
    is the prose check, restricted to package data under ``content_roots`` so that test
    fixtures quoting a doctrine filename are not mistaken for shipped doctrine.
    """
    findings: list[Finding] = []
    for name, content in members:
        normalized = name.replace("\\", "/")
        # Archive members are prefixed with `<project>-<version>/`; compare on the tail too.
        tail = normalized.partition("/")[2] or normalized
        if knowledge_domain(normalized) or knowledge_domain(tail):
            findings.append(Finding(name, "packaged-knowledge", tail))
        if normalized.endswith("-build-report.md"):
            findings.append(Finding(name, "packaged-build-report", tail))
        if tail.startswith(content_roots) and normalized.endswith(_DATA_SUFFIXES):
            signature = _CONTENT_DOMAIN.search(content.decode("utf-8", errors="replace"))
            if signature is not None:
                findings.append(Finding(name, "knowledge-content", signature.group()))
    return sorted(set(findings))


def scan_artifact(path: Path) -> list[Finding]:
    """Scan a built sdist (``.tar.gz``), a wheel (``.zip``), or an installed directory."""
    import tarfile
    import zipfile

    if path.is_dir():
        return scan_members(
            (
                (p.relative_to(path).as_posix(), p.read_bytes())
                for p in sorted(path.rglob("*"))
                if p.is_file()
            ),
            "installed",
        )
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return scan_members(
                (
                    (name, archive.read(name))
                    for name in archive.namelist()
                    if not name.endswith("/")
                ),
                "wheel",
            )
    with tarfile.open(path) as archive:
        members: list[tuple[str, bytes]] = []
        for member in archive.getmembers():
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            if stream is not None:
                members.append((member.name, stream.read()))
        return scan_members(members, "sdist")
