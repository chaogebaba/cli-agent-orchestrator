"""Executable AC-LITE-3 slice-1 scans (not a runtime component).

Exit 1 means the requested boundary is NOT achieved. No production finding is
allowlisted here. This conservative AST scan resolves simple dynamic strings,
joined paths and aliases; arbitrary computed paths still need runtime tracing.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import tarfile
import tomllib
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

_DOMAIN = re.compile(
    r"self[-_ ]?audit|(?:^|[/\\])(?:orchestrator|doctrine|blueprints)(?:[/\\]|$)"
    r"|(?:ORCH_MAP|HANDOFF|WP-BACKLOG|BUGS|MISTAKES|GOLDEN-TIPS)\.md"
    r"|compliance[-_ ]auditor|orchestration[-_ ]knowledge|workflow[-_ ]ledger"
    r"|golden[-_ ]tips|doctrine[-_ ](?:rules|compliance)",
    re.IGNORECASE,
)
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
_IMPORT_DOMAIN = re.compile(
    r"(?:^|\.)(?:orchestrator|doctrine|blueprints|self_?audit|compliance_auditor)(?:\.|$)",
    re.IGNORECASE,
)
_CONTENT_DOMAIN = re.compile(
    r"self[-_ ]?audit|compliance[-_ ]auditor|orchestration[-_ ]knowledge"
    r"|workflow[-_ ]ledger|golden[-_ ]tips|orchestrator doctrine",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class Finding:
    surface: str
    location: str
    rule: str
    evidence: str


def knowledge_domain(value: str) -> bool:
    return bool(_DOMAIN.search(value.replace("\\", "/")))


def _body(nodes: Iterable[ast.stmt]) -> list[ast.stmt]:
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
    tree = ast.parse(source, filename=location)
    # Remove docstrings before traversing: historical citations are not I/O.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = _body(node.body)
    values: dict[str, str] = {}
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            assigned_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = _value(node.value, values) if node.value else ""
            for assigned_target in assigned_targets:
                if (
                    isinstance(assigned_target, ast.Name)
                    and value
                    and values.get(assigned_target.id) != value
                ):
                    values[assigned_target.id] = value
                    changed = True
        if not changed:
            break
    findings: set[Finding] = set()
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets = [f"{node.module or ''}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Call) and _name(node.func).split(".")[-1] in {
            "import_module",
            "__import__",
        }:
            targets = [_value(arg, values) for arg in node.args[:1]]
        for target in targets:
            if _IMPORT_DOMAIN.search(target):
                findings.add(Finding("source", location, "reverse-import", target))
        if isinstance(node, ast.Call):
            call_name = _name(node.func)
            if re.search(r"self_?audit|compliance_auditor", call_name, re.IGNORECASE):
                findings.add(Finding("source", location, "audit-activation", call_name))
    scopes = [("<module>", tree.body)] + [
        (node.name, node.body)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope, body in scopes:
        # Module scan excludes function bodies to retain precise ownership.
        roots = [
            node
            for node in body
            if scope != "<module>"
            or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        nodes = [node for root in roots for node in ast.walk(root)]
        calls = sorted(
            {
                _name(node.func).split(".")[-1]
                for node in nodes
                if isinstance(node, ast.Call) and _name(node.func).split(".")[-1] in _IO
            }
        )
        domain = sorted(
            {_value(node, values) for node in nodes if knowledge_domain(_value(node, values))}
        )
        if calls and domain:
            findings.add(
                Finding(
                    "source",
                    f"{location}:{scope}",
                    "knowledge-io",
                    f"{','.join(calls)} -> {domain[0]}",
                )
            )
    return sorted(findings)


def scan_configuration(value: Any, location: str, surface: str = "configuration") -> list[Finding]:
    findings: list[Finding] = []
    if isinstance(value, dict):
        for key, child in value.items():
            findings.extend(scan_configuration(child, f"{location}.{key}", surface))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(scan_configuration(child, f"{location}[{index}]", surface))
    elif isinstance(value, str) and (knowledge_domain(value) or _IMPORT_DOMAIN.search(value)):
        findings.append(Finding(surface, location, "knowledge-configuration", value))
    return findings


def scan_repository(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted((root / "src").rglob("*.py")):
        findings.extend(
            scan_python(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
        )
    for path in sorted((root / "src").rglob("*")):
        if not path.is_file() or path.suffix not in {".md", ".txt", ".json", ".toml", ".sh"}:
            continue
        text = path.read_text(encoding="utf-8")
        signature = _CONTENT_DOMAIN.search(text)
        if signature is not None:
            findings.append(
                Finding(
                    "package-data",
                    path.relative_to(root).as_posix(),
                    "knowledge-content",
                    signature.group(),
                )
            )
        if path.suffix in {".json", ".toml"}:
            data = json.loads(text) if path.suffix == ".json" else tomllib.loads(text)
            findings.extend(
                scan_configuration(data, path.relative_to(root).as_posix(), "package-data")
            )
    metadata = root / "pyproject.toml"
    if metadata.is_file():
        data = tomllib.loads(metadata.read_text(encoding="utf-8"))
        # Documentation/linter contracts are not package metadata or startup.
        for key, value in (
            ("project", data.get("project", {})),
            ("build-system", data.get("build-system", {})),
            ("hatch", data.get("tool", {}).get("hatch", {})),
            ("cibuildwheel", data.get("tool", {}).get("cibuildwheel", {})),
        ):
            findings.extend(scan_configuration(value, f"pyproject.toml.{key}", "metadata"))
    # Only project-owned configurations, never a user's personal hook files.
    for directory in (root / ".claude", root / ".openai"):
        for path in sorted(directory.rglob("*.json")):
            findings.extend(
                scan_configuration(
                    json.loads(path.read_text()), path.relative_to(root).as_posix(), "hook"
                )
            )
    return sorted(set(findings))


def scan_members(members: Iterable[tuple[str, bytes]], surface: str) -> list[Finding]:
    findings: list[Finding] = []
    for name, content in members:
        if knowledge_domain(name):
            findings.append(Finding(surface, name, "packaged-knowledge", name))
        normalized = name.replace("\\", "/")
        if normalized.endswith((".md", ".txt", ".json", ".toml", ".sh")):
            signature = _CONTENT_DOMAIN.search(content.decode("utf-8"))
            if signature is not None:
                findings.append(Finding(surface, name, "knowledge-content", signature.group()))
        if normalized.endswith(".py") and (
            "cli_agent_orchestrator/" in normalized or "cao_workflow/" in normalized
        ):
            findings.extend(
                Finding(surface, row.location, row.rule, row.evidence)
                for row in scan_python(content.decode("utf-8"), name)
            )
        if normalized.endswith(("entry_points.txt", "METADATA", "PKG-INFO")):
            for line in content.decode("utf-8").splitlines():
                if knowledge_domain(line) or _IMPORT_DOMAIN.search(line):
                    findings.append(Finding(surface, name, "knowledge-entry-or-dependency", line))
    return sorted(set(findings))


def scan_artifact(path: Path) -> list[Finding]:
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
        members = []
        for member in archive.getmembers():
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is not None:
                    members.append((member.name, stream.read()))
        return scan_members(members, "sdist")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    args = parser.parse_args()
    findings = scan_repository(args.root)
    for path in args.artifact:
        findings.extend(scan_artifact(path))
    print(
        json.dumps(
            {"boundary_achieved": not findings, "findings": [asdict(row) for row in findings]},
            indent=2,
        )
    )
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
