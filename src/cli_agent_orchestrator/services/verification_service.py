"""Repository verification helpers used by the ``cao`` CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, TypedDict
from urllib.parse import urlparse
from urllib.request import url2pathname

from cli_agent_orchestrator.constants import SERVER_PORT


STAMP_MAGIC = "# CAO_SUITE_LOG_V1"
STAMP_FIELDS = ("commit", "dirty", "timestamp", "cwd")
_PYTEST_OUTCOME = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|error|errors|skipped|deselected|xfailed|xpassed)\b"
)
_PYTEST_COMPLETION = re.compile(
    r"\bin\s+\d+(?:\.\d+)?s(?:\s+\(\d+:\d{2}:\d{2}\))?\s*$"
)


class DeploymentStatus(TypedDict):
    cli_path: str
    differing_files: int | None
    server: str
    source_root: str


def git_root(cwd: Path | None = None) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, text=True,
        capture_output=True, check=True,
    )
    return Path(result.stdout.strip()).resolve()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    ).stdout


def changed_files(root: Path) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-m", "-o", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=True,
    ).stdout
    return sorted({item.decode("utf-8", "surrogateescape") for item in output.split(b"\0") if item})


def short_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest[:12]


def tree_stamp(root: Path) -> dict[str, object]:
    dirty = {name: short_hash(root / name) for name in changed_files(root)}
    return {
        "commit": _git(root, "rev-parse", "HEAD").strip(),
        "dirty": dirty,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cwd": str(root),
    }


def write_stamp(stream: IO[str], stamp: dict[str, object]) -> None:
    stream.write(f"{STAMP_MAGIC}\n")
    for field in STAMP_FIELDS:
        stream.write(f"# {field}: {json.dumps(stamp[field], sort_keys=True)}\n")
    stream.write("\n")


def parse_stamp(path: Path) -> tuple[dict[str, object], str]:
    with path.open(encoding="utf-8", errors="replace") as stream:
        if stream.readline().rstrip("\n") != STAMP_MAGIC:
            raise ValueError("missing or invalid suite-log stamp header")
        stamp: dict[str, object] = {}
        for field in STAMP_FIELDS:
            line = stream.readline().rstrip("\n")
            prefix = f"# {field}: "
            if not line.startswith(prefix):
                raise ValueError(f"missing stamp field: {field}")
            try:
                stamp[field] = json.loads(line[len(prefix):])
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid stamp field: {field}") from exc
        if stream.readline() != "\n":
            raise ValueError("stamp header is not terminated")
        if not isinstance(stamp["commit"], str) or not stamp["commit"]:
            raise ValueError("invalid stamp field type: commit")
        if not isinstance(stamp["dirty"], dict) or not all(
            isinstance(name, str) and (value is None or isinstance(value, str))
            for name, value in stamp["dirty"].items()
        ):
            raise ValueError("invalid stamp field type: dirty")
        if not isinstance(stamp["timestamp"], str) or not stamp["timestamp"]:
            raise ValueError("invalid stamp field type: timestamp")
        try:
            datetime.fromisoformat(stamp["timestamp"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid stamp timestamp") from exc
        if not isinstance(stamp["cwd"], str) or not stamp["cwd"]:
            raise ValueError("invalid stamp field type: cwd")
        return stamp, stream.read()


def pytest_summary_error(body: str) -> str | None:
    """Return why pytest output is not a successful completed run."""
    if re.search(r"KeyboardInterrupt|interrupted", body, re.IGNORECASE):
        return "suite output shows an interrupted run"
    if re.search(r"^(?:FAILED|ERROR)\s", body, re.MULTILINE):
        return "suite output contains a pytest failure/error marker"
    summaries = []
    for line in body.splitlines():
        normalized = line.strip().strip("=").strip()
        if _PYTEST_COMPLETION.search(normalized) and _PYTEST_OUTCOME.search(normalized):
            summaries.append(normalized)
    if not summaries:
        return "suite output has no pytest completion summary"
    if len(summaries) != 1:
        return f"suite output has {len(summaries)} pytest completion summaries"
    summary = summaries[0]
    for count, kind in _PYTEST_OUTCOME.findall(body):
        if kind in {"failed", "error", "errors"} and int(count) > 0:
            return "suite output contains a nonzero failed/error outcome"
    counts = {kind: int(count) for count, kind in _PYTEST_OUTCOME.findall(summary)}
    failures = counts.get("failed", 0)
    errors = counts.get("error", 0) + counts.get("errors", 0)
    if failures or errors:
        return f"suite summary has {failures} failed and {errors} errors"
    if counts.get("passed", 0) < 1:
        return "suite summary has no passing tests"
    return None


def run_suite(feature: str, stdout: IO[str]) -> tuple[int, Path, str]:
    root = git_root()
    log_dir = root / "tmp" / "orch"
    log_dir.mkdir(parents=True, exist_ok=True)
    final_path = log_dir / f"suite-{feature}.log"
    stamp = tree_stamp(root)
    raw_fd, raw_name = tempfile.mkstemp(prefix=f"suite-{feature}-", suffix=".raw", dir=log_dir)
    os.close(raw_fd)
    raw_path = Path(raw_name)
    summary = "pytest summary unavailable"
    try:
        with raw_path.open("w", encoding="utf-8") as raw:
            process = subprocess.Popen(
                ["uv", "run", "pytest"], cwd=root, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            assert process.stdout is not None
            for line in process.stdout:
                stdout.write(line)
                stdout.flush()
                raw.write(line)
                if re.search(r"(?:passed|failed|error|skipped|deselected)", line):
                    summary = line.strip()
            exit_code = process.wait()
        if exit_code == 0:
            final_fd, final_name = tempfile.mkstemp(
                prefix=f".{final_path.name}.", dir=log_dir, text=True
            )
            with os.fdopen(final_fd, "w", encoding="utf-8") as target:
                write_stamp(target, stamp)
                target.write(raw_path.read_text(encoding="utf-8", errors="replace"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(final_name, final_path)
        return exit_code, final_path, summary
    finally:
        raw_path.unlink(missing_ok=True)


def verify_suite_log(path: Path) -> tuple[bool, list[str], str]:
    reasons: list[str] = []
    try:
        stamp, body = parse_stamp(path)
    except (OSError, ValueError) as exc:
        return False, [str(exc)], "unknown"
    try:
        root = git_root()
        current = tree_stamp(root)
        if Path(str(stamp["cwd"])).resolve() != root:
            reasons.append("stamped cwd differs from current repository root")
        if stamp["commit"] != current["commit"]:
            reasons.append("HEAD differs")
        if stamp["dirty"] != current["dirty"]:
            reasons.append("dirty file set or hashes differ")
    except (OSError, subprocess.CalledProcessError) as exc:
        reasons.append(f"could not inspect current tree: {exc}")
    summary_error = pytest_summary_error(body)
    if summary_error:
        reasons.append(summary_error)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return not reasons, reasons, mtime


def installed_package_root() -> Path | None:
    executable = shutil.which("cao")
    if not executable:
        return None
    first = Path(executable).read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
    if not first or not first[0].startswith("#!"):
        return None
    python = first[0][2:].strip()
    result = subprocess.run(
        [python, "-c", "import pathlib,cli_agent_orchestrator as p; print(pathlib.Path(p.__file__).parent)"],
        text=True, capture_output=True,
    )
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def installed_source_root(installed: Path) -> Path | None:
    """Return the local source recorded by the installed wheel, when available."""
    direct_urls = installed.parent.glob("cli_agent_orchestrator-*.dist-info/direct_url.json")
    for path in sorted(direct_urls):
        try:
            url = json.loads(path.read_text(encoding="utf-8"))["url"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(url, str):
            continue
        parsed = urlparse(url)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            continue
        root = Path(url2pathname(parsed.path)).resolve()
        if (root / "src" / "cli_agent_orchestrator").is_dir():
            return root
    return None


def cli_deploy_root(repo_root: Path) -> Path:
    """Resolve the CLI comparison root, using local install provenance as fallback."""
    root = repo_root.resolve()
    if (root / "src" / "cli_agent_orchestrator").is_dir():
        return root
    installed = installed_package_root()
    if installed is not None and installed.is_dir():
        return installed_source_root(installed) or root
    return root


def compare_installed(
    root: Path, installed: Path
) -> tuple[str, int | None, float | None]:
    source = root / "src" / "cli_agent_orchestrator"
    newest = max((p.stat().st_mtime for p in installed.rglob("*.py")), default=None)
    if not source.is_dir():
        return "source-not-found", None, newest
    repo_files = {p.relative_to(source) for p in source.rglob("*.py")}
    installed_files = {p.relative_to(installed) for p in installed.rglob("*.py")}
    differing = 0
    for rel in repo_files | installed_files:
        left, right = source / rel, installed / rel
        if not left.is_file() or not right.is_file() or left.read_bytes() != right.read_bytes():
            differing += 1
    return ("current" if differing == 0 else "stale"), differing, newest


def listening_pid(port: int) -> int | None:
    result = subprocess.run(
        ["ss", "-ltnp", f"sport = :{port}"], text=True, capture_output=True
    )
    match = re.search(r'pid=(\d+)', result.stdout)
    return int(match.group(1)) if match else None


def process_start_time(pid: int) -> float | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        ticks = int(stat[stat.rfind(")") + 2:].split()[19])
        boot = next(
            int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def deployment_status(repo_root: Path) -> DeploymentStatus:
    """Return structured deploy truth for an explicit source root."""
    source_root = repo_root.resolve()
    installed = installed_package_root()
    if installed is None or not installed.is_dir():
        state, count, newest = "not-found", None, None
    else:
        state, count, newest = compare_installed(source_root, installed)
    pid = listening_pid(SERVER_PORT)
    if pid is None:
        server = "not-running"
    else:
        started = process_start_time(pid)
        server = "unknown" if started is None or newest is None else ("restart-needed" if newest > started else "current")
    return {
        "cli_path": state,
        "differing_files": count,
        "server": server,
        "source_root": str(source_root),
    }


def format_server_status(
    server: str,
    *,
    restarted: bool = False,
    timeout_seconds: int | None = None,
) -> str:
    """Return an operator-facing explanation without changing status values."""
    if server == "current" and restarted:
        return f"server: current (restarted and listening on :{SERVER_PORT})"
    if server == "not-running":
        elapsed = (
            f" after {timeout_seconds}s" if timeout_seconds is not None else ""
        )
        return (
            f"server: not-running (no listener on :{SERVER_PORT}{elapsed} - check: "
            "systemctl --user status cao-server; "
            "journalctl --user -u cao-server)"
        )
    if server == "restart-needed" and restarted:
        return (
            "server: restart-needed\n"
            "hint: cao-server is listening, but it predates the installed CLI"
        )
    if server == "unknown" and restarted:
        return (
            "server: unknown\n"
            "hint: cao-server is listening, but deployment freshness could not be determined"
        )
    return f"server: {server}"


def find_workspace_file(start: Path, name: str) -> Path | None:
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None
