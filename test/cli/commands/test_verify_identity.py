"""Acceptance tests for ``cao verify identity`` (F94R)."""

from __future__ import annotations

import ast
import json
import socket
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest
from click.testing import CliRunner

import cli_agent_orchestrator
from cli_agent_orchestrator.backends.base import PaneIdentityReadResult
from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.cli.commands import verify as verify_command
from cli_agent_orchestrator.services import identity_verify_service as identity_service
from cli_agent_orchestrator.services.identity_verify_service import ProcSnapshot, scan_identity

PRODUCTION = "http://127.0.0.1:9889"
SANDBOX = "http://127.0.0.1:9890"


def _database(tmp_path: Path, *rows: tuple[str, str, str, str | None]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "cao.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE terminals ("
        "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, tmux_window TEXT NOT NULL, "
        "agent_profile TEXT)"
    )
    connection.executemany(
        "INSERT INTO terminals (id, tmux_session, tmux_window, agent_profile) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()
    return path


def _carrier(pid: int) -> ProcSnapshot:
    return ProcSnapshot(pid=pid, comm="cao-mcp-server", argv=["python"])


def _scan(
    db_path: Path,
    processes: list[ProcSnapshot],
    environs: dict[int, dict[str, str]],
    *,
    endpoint: str = PRODUCTION,
    parents: dict[int, tuple[int | None, str]] | None = None,
    windows: dict[tuple[str, str], bool] | None = None,
    panes: dict[tuple[str, str], PaneIdentityReadResult] | None = None,
    environ_reader: Callable[[int], dict[str, str]] | None = None,
    parent_reader: Callable[[int], tuple[int | None, str]] | None = None,
    pane_calls: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    parent_values = parents or {
        process.pid: (process.pid + 1000, "claude") for process in processes
    }
    window_values = windows or {}
    pane_values = panes or {}

    def read_pane(session: str, window: str) -> PaneIdentityReadResult:
        if pane_calls is not None:
            pane_calls.append((session, window))
        return pane_values.get((session, window), PaneIdentityReadResult(reason="read_error"))

    return scan_identity(
        endpoint=endpoint,
        db_path=db_path,
        processes=processes,
        process_environ_reader=environ_reader or environs.__getitem__,
        parent_reader=parent_reader or parent_values.__getitem__,
        window_reader=lambda session, window: window_values.get((session, window), False),
        pane_reader=read_pane,
    )


def _env(tid: str | None, endpoint: str = PRODUCTION, **extra: str) -> dict[str, str]:
    values = {"CAO_ENDPOINT": endpoint, **extra}
    if tid is not None:
        values["CAO_TERMINAL_ID"] = tid
    return values


def test_ac1_union_enumeration_exact_structural_grammar(tmp_path: Path) -> None:
    processes = [
        ProcSnapshot(1, "cao-mcp-server", ["/venv/bin/python", "/bin/cao-mcp-server"]),
        ProcSnapshot(2, "python", ["python", "-m", identity_service._MCP_MODULE]),
        ProcSnapshot(3, "grok", ["grok", "--system-prompt-override", "use cao-mcp-server"]),
        ProcSnapshot(4, "zsh", ["zsh", "-c", f"pgrep -f {identity_service._MCP_MODULE}"]),
        ProcSnapshot(5, "python", ["python", "-c", "import sys", identity_service._MCP_MODULE]),
        ProcSnapshot(6, "cao-mcp-server-helper", ["/bin/cao-mcp-server"]),
        ProcSnapshot(7, "python", ["python", "-m", "other.module", identity_service._MCP_MODULE]),
        ProcSnapshot(8, "python", ["python", "-m", f"{identity_service._MCP_MODULE}.extra"]),
        ProcSnapshot(9, "cao-mcp-server", ["python", "-m", identity_service._MCP_MODULE]),
    ]
    db = _database(tmp_path, ("live9999", "s", "w", "worker"))
    environs = {process.pid: _env("live9999") for process in processes}
    result = _scan(
        db,
        processes,
        environs,
        windows={("s", "w"): True},
        panes={("s", "w"): PaneIdentityReadResult(identity="live9999")},
    )

    assert [row["mcp_pid"] for row in result["rows"]] == [1, 2, 9]
    assert [row["mcp_pid"] for row in result["out_of_scope"]] == []


def test_ac2_endpoint_prefilter_keeps_sandbox_out_of_fail_and_carries_parent(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path, ("aaaa1111", "prod", "worker", "worker"))
    result = _scan(
        db,
        [_carrier(10), _carrier(20)],
        {10: _env("aaaa1111"), 20: _env("bbbb2222", SANDBOX)},
        parents={10: (110, "claude host"), 20: (220, "grok sandbox")},
        windows={("prod", "worker"): True},
        panes={("prod", "worker"): PaneIdentityReadResult(identity="aaaa1111")},
    )
    assert result["summary"]["fail"] == 0
    assert [row["mcp_pid"] for row in result["rows"]] == [10]
    assert result["out_of_scope"] == [
        {
            "mcp_pid": 20,
            "mcp_tid": "bbbb2222",
            "endpoint": SANDBOX,
            "reason": "endpoint_mismatch",
            "parent_cmd": "grok sandbox",
        }
    ]

    vanished_parent = _scan(
        db,
        [_carrier(20)],
        {20: _env("bbbb2222", SANDBOX)},
        parent_reader=lambda _pid: (None, ""),
    )
    assert vanished_parent["out_of_scope"][0] == {
        "mcp_pid": 20,
        "mcp_tid": "bbbb2222",
        "endpoint": SANDBOX,
        "reason": "endpoint_mismatch",
        "parent_cmd": "",
    }


def test_ac3_genuine_split_is_exactly_one_fail(tmp_path: Path) -> None:
    db = _database(tmp_path, ("live9999", "s", "w", "worker"))
    result = _scan(
        db,
        [_carrier(1), _carrier(2)],
        {1: _env("dead0000"), 2: _env("live9999")},
        windows={("s", "w"): True},
        panes={("s", "w"): PaneIdentityReadResult(identity="live9999")},
    )
    failed = [row for row in result["rows"] if row["verdict"] == "FAIL"]
    assert [(row["mcp_pid"], row["fail_reasons"]) for row in failed] == [(1, ["tid_not_in_db"])]
    assert next(row for row in result["rows"] if row["mcp_pid"] == 2)["verdict"] == "OK"
    assert result["summary"]["fail"] == 1


def test_ac4_pane_mismatch_fails_but_unreadable_pane_warns(tmp_path: Path) -> None:
    db = _database(
        tmp_path,
        ("live9999", "s", "mismatch", "worker"),
        ("live8888", "s", "unknown", "worker"),
    )
    result = _scan(
        db,
        [_carrier(1), _carrier(2)],
        {1: _env("live9999"), 2: _env("live8888")},
        windows={("s", "mismatch"): True, ("s", "unknown"): True},
        panes={
            ("s", "mismatch"): PaneIdentityReadResult(identity="other1111"),
            ("s", "unknown"): PaneIdentityReadResult(reason="pane_cardinality"),
        },
    )
    rows = {row["mcp_pid"]: row for row in result["rows"]}
    assert rows[1]["verdict"] == "FAIL"
    assert rows[1]["fail_reasons"] == ["pane_tid_mismatch"]
    assert rows[2]["verdict"] == "WARN"
    assert rows[2]["pane_reason"] == "pane_cardinality"
    assert rows[2]["fail_reasons"] == []


def test_ac5_vanished_pid_does_not_abort_or_fail(tmp_path: Path) -> None:
    db = _database(tmp_path, ("live9999", "s", "w", "worker"))

    def environ(pid: int) -> dict[str, str]:
        if pid == 1:
            raise FileNotFoundError(pid)
        return _env("live9999")

    result = _scan(
        db,
        [_carrier(1), _carrier(2)],
        {},
        environ_reader=environ,
        windows={("s", "w"): True},
        panes={("s", "w"): PaneIdentityReadResult(identity="live9999")},
    )
    assert result["vanished_pids"] == [1]
    assert [(row["mcp_pid"], row["verdict"]) for row in result["rows"]] == [(2, "OK")]
    assert result["summary"]["fail"] == 0


def test_ac6_healthy_multiplicity_and_parent_classification(tmp_path: Path) -> None:
    db = _database(tmp_path, ("same1111", "s", "w", "worker"))
    result = _scan(
        db,
        [_carrier(1), _carrier(2)],
        {1: _env("same1111"), 2: _env("same1111")},
        parents={1: (101, "claude --flag"), 2: (102, "claude bg-spare --flag")},
        windows={("s", "w"): True},
        panes={("s", "w"): PaneIdentityReadResult(identity="same1111")},
    )
    assert [row["verdict"] for row in result["rows"]] == ["OK", "OK"]
    assert [
        (row["parent_pid"], row["parent_cmd"], row["parent_kind"]) for row in result["rows"]
    ] == [
        (101, "claude --flag", "claude"),
        (102, "claude bg-spare --flag", "bg-spare"),
    ]


def test_ac6_default_parent_seam_distinguishes_carrier_and_parent_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _database(tmp_path, ("same1111", "s", "w", "worker"))
    monkeypatch.setattr(identity_service, "_read_carrier_status", lambda pid: 900 + pid)

    def scan() -> dict[str, Any]:
        return scan_identity(
            endpoint=PRODUCTION,
            db_path=db,
            processes=[_carrier(1)],
            process_environ_reader={1: _env("same1111")}.__getitem__,
            window_reader=lambda session, window: (session, window) == ("s", "w"),
            pane_reader=lambda _session, _window: PaneIdentityReadResult(identity="same1111"),
        )

    def parent_gone(_ppid: int) -> str:
        raise FileNotFoundError

    monkeypatch.setattr(identity_service, "_read_parent_cmdline", parent_gone)
    parent_loss = scan()
    assert parent_loss["vanished_pids"] == []
    assert parent_loss["rows"][0]["parent_pid"] is None
    assert parent_loss["rows"][0]["parent_kind"] == "other"

    def carrier_gone(_pid: int) -> int:
        raise FileNotFoundError

    monkeypatch.setattr(identity_service, "_read_carrier_status", carrier_gone)
    carrier_loss = scan()
    assert carrier_loss["vanished_pids"] == [1]
    assert carrier_loss["rows"] == []


def test_parent_kind_uses_full_cmdline_before_stored_value_is_truncated(tmp_path: Path) -> None:
    db = _database(tmp_path, ("same1111", "s", "w", "worker"))
    full_cmdline = f"{'x' * 201} bg-spare --flag"
    result = _scan(
        db,
        [_carrier(1)],
        {1: _env("same1111")},
        parents={1: (101, full_cmdline)},
        windows={("s", "w"): True},
        panes={("s", "w"): PaneIdentityReadResult(identity="same1111")},
    )
    row = result["rows"][0]
    assert row["parent_kind"] == "bg-spare"
    assert row["parent_cmd"] == "x" * 200


def test_live_processes_keeps_exact_comm_carrier_with_non_utf8_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProcFile:
        def __init__(self, name: str) -> None:
            self.name = name

        def read_text(self, *, encoding: str) -> str:
            assert (self.name, encoding) == ("comm", "utf-8")
            return "cao-mcp-server\n"

        def read_bytes(self) -> bytes:
            assert self.name == "cmdline"
            return b"python\0\xff\0"

    class ProcEntry:
        name = "123"

        def __truediv__(self, name: str) -> ProcFile:
            return ProcFile(name)

    class ProcRoot:
        def iterdir(self) -> list[ProcEntry]:
            return [ProcEntry()]

    def fake_path(value: str) -> ProcRoot | ProcFile:
        if value == "/proc":
            return ProcRoot()
        assert value == "/proc/123/cmdline"
        return ProcFile("cmdline")

    monkeypatch.setattr(identity_service, "Path", fake_path)
    processes = identity_service._live_processes()
    assert processes == [ProcSnapshot(123, "cao-mcp-server", ["python", "\ufffd"])]
    assert identity_service._is_carrier(processes[0])


def test_non_utf8_parent_cmdline_becomes_unknown_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _database(tmp_path, ("same1111", "s", "w", "worker"))
    monkeypatch.setattr(identity_service, "_read_carrier_status", lambda _pid: 901)

    def non_utf8_parent(_ppid: int) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(identity_service, "_read_parent_cmdline", non_utf8_parent)
    result = scan_identity(
        endpoint=PRODUCTION,
        db_path=db,
        processes=[_carrier(1)],
        process_environ_reader={1: _env("same1111")}.__getitem__,
        window_reader=lambda session, window: (session, window) == ("s", "w"),
        pane_reader=lambda _session, _window: PaneIdentityReadResult(identity="same1111"),
    )
    assert result["vanished_pids"] == []
    assert result["rows"][0]["parent_pid"] is None
    assert result["rows"][0]["parent_cmd"] == ""
    assert result["rows"][0]["parent_kind"] == "other"


def test_ac7_process_count_and_mcp_json_residue_never_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _database(tmp_path, ("same1111", "s", "w", "worker"))
    residue = tmp_path / "residue"
    residue.mkdir()
    monkeypatch.setenv("CAO_TMP_DIR", str(residue))
    for tid in ("deadbeef", "cafe1234", "f00dfeed"):
        (residue / f"{tid}.mcp.json").write_text("{}", encoding="utf-8")
    result = _scan(
        db,
        [_carrier(1), _carrier(2)],
        {1: _env("same1111"), 2: _env("same1111")},
        windows={("s", "w"): True},
        panes={("s", "w"): PaneIdentityReadResult(identity="same1111")},
    )
    assert len(result["rows"]) == 2
    assert result["summary"]["fail"] == 0
    serialized = json.dumps(result)
    assert all(tid not in serialized for tid in ("deadbeef", "cafe1234", "f00dfeed"))


def test_ac8_verify_identity_help_is_registered() -> None:
    group_help = CliRunner().invoke(cli, ["verify", "--help"])
    command_help = CliRunner().invoke(cli, ["verify", "identity", "--help"])
    assert group_help.exit_code == 0
    assert "identity" in group_help.output
    assert command_help.exit_code == 0
    assert all(flag in command_help.output for flag in ("--json", "--endpoint", "--db"))


def _imported_targets(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            targets.append(base)
            targets.extend(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return targets


def test_ac9a_identity_surface_has_no_http_import_and_allowlist_cannot_drift() -> None:
    package_root = Path(cli_agent_orchestrator.__file__).parent
    service_dir = package_root / "services"
    identity_modules = sorted(service_dir.glob("identity_verify*.py"))
    expected = service_dir / "identity_verify_service.py"
    assert identity_modules == [expected]
    scanned = [expected, package_root / "cli" / "commands" / "verify.py"]
    forbidden = {
        "urllib.request",
        "urllib.error",
        "http.client",
        "requests",
        "httpx",
        "CAOHttpClient",
        "cao_http",
        "cli_agent_orchestrator.utils.http",
    }
    violations: list[str] = []
    for path in scanned:
        for target in _imported_targets(path):
            if any(
                target == item or target.startswith(f"{item}.") or target.endswith(f".{item}")
                for item in forbidden
            ):
                violations.append(f"{path.name}: {target}")
    assert violations == []


def test_ac9b_ok_scan_never_opens_a_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[tuple[object, ...]] = []

    def no_connect(*args: object, **_kwargs: object) -> None:
        attempts.append(args)
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_connect)
    db = _database(tmp_path, ("live9999", "s", "w", "worker"))
    result = _scan(
        db,
        [_carrier(1)],
        {1: _env("live9999")},
        windows={("s", "w"): True},
        panes={("s", "w"): PaneIdentityReadResult(identity="live9999")},
    )
    assert result["rows"][0]["verdict"] == "OK"
    assert attempts == []


def test_ac10_default_endpoint_uses_scanner_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _database(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setenv("CAO_API_PORT", "9890")

    def fake_scan(*, endpoint: str, db_path: Path) -> dict[str, Any]:
        captured.update(endpoint=endpoint, db_path=db_path)
        return {
            "scan_endpoint": endpoint,
            "scan_db": str(db_path),
            "rows": [],
            "out_of_scope": [],
            "vanished_pids": [],
            "summary": {
                "in_scope": 0,
                "ok": 0,
                "warn": 0,
                "fail": 0,
                "out_of_scope": 0,
                "vanished": 0,
                "scan_warning": None,
            },
            "summary_warn": [],
            "window_authority": [],
        }

    monkeypatch.setattr(verify_command, "scan_identity", fake_scan)
    result = CliRunner().invoke(cli, ["verify", "identity", "--db", str(db), "--json"])
    assert result.exit_code == 0
    assert captured["endpoint"] == SANDBOX

    in_scope = _scan(
        _database(tmp_path / "positive", ("live9999", "s", "w", "worker")),
        [_carrier(1)],
        {1: _env("live9999", SANDBOX)},
        endpoint=SANDBOX,
        windows={("s", "w"): True},
        panes={("s", "w"): PaneIdentityReadResult(identity="live9999")},
    )
    assert in_scope["rows"][0]["verdict"] == "OK"
    assert in_scope["summary"]["scan_warning"] is None


def test_ac10_carrier_default_uses_carrier_environment_and_warns(tmp_path: Path) -> None:
    db = _database(tmp_path)
    process = _carrier(1)
    result = _scan(
        db,
        [process],
        {1: {"CAO_TERMINAL_ID": "sandbox1", "CAO_API_PORT": "9890"}},
    )
    assert result["rows"] == []
    assert result["out_of_scope"][0]["endpoint"] == SANDBOX
    assert result["summary"]["scan_warning"] == (
        "1 carriers found, none matched scan endpoint http://127.0.0.1:9889"
    )


def test_ac10_human_warning_is_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _database(tmp_path)
    document = _scan(db, [_carrier(1)], {1: _env("outside", SANDBOX)})
    monkeypatch.setattr(verify_command, "scan_identity", lambda **_kwargs: document)
    result = CliRunner().invoke(cli, ["verify", "identity", "--db", str(db)])
    assert result.exit_code == 0
    assert "WARN: 1 carriers found, none matched scan endpoint" in result.output


def test_ac11_verdict_clauses_and_pane_nonconsultation(tmp_path: Path) -> None:
    db = _database(
        tmp_path,
        ("ok111111", "s", "ok", "worker"),
        ("dead2222", "s", "dead", "worker"),
        ("comp3333", "s", "compete", "worker"),
    )
    pane_calls: list[tuple[str, str]] = []
    result = _scan(
        db,
        [_carrier(1), _carrier(2), _carrier(3), _carrier(4)],
        {1: _env("ok111111"), 2: _env("dead2222"), 3: _env(None), 4: _env("comp3333")},
        windows={("s", "ok"): True, ("s", "dead"): False, ("s", "compete"): True},
        panes={
            ("s", "ok"): PaneIdentityReadResult(identity="ok111111"),
            ("s", "compete"): PaneIdentityReadResult(identity="other444"),
        },
        pane_calls=pane_calls,
    )
    rows = {row["mcp_pid"]: row for row in result["rows"]}
    assert (rows[1]["verdict"], rows[1]["pane_agrees"], rows[1]["pane_reason"]) == (
        "OK",
        True,
        None,
    )
    assert (rows[2]["verdict"], rows[2]["pane_tid"], rows[2]["pane_reason"]) == (
        "WARN",
        None,
        None,
    )
    assert rows[3]["verdict"] == "FAIL"
    assert rows[3]["fail_reasons"] == ["missing_mcp_tid"]
    assert rows[4]["verdict"] == "FAIL"
    assert rows[4]["pane_tid"] == "other444"
    assert ("s", "dead") not in pane_calls


def test_ac11_fail_summary_drives_cli_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _database(tmp_path)
    document = _scan(db, [_carrier(1)], {1: _env("missing1")})
    monkeypatch.setattr(verify_command, "scan_identity", lambda **_kwargs: document)
    result = CliRunner().invoke(cli, ["verify", "identity", "--db", str(db), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["summary"]["fail"] == 1


def test_ac12_window_authority_and_summary_warn_are_machine_visible(tmp_path: Path) -> None:
    db = _database(
        tmp_path,
        ("one11111", "s", "shared", "worker"),
        ("two22222", "s", "shared", "worker"),
    )
    result = _scan(
        db,
        [],
        {},
        windows={("s", "shared"): True},
    )
    assert result["window_authority"] == [
        {"tmux_session": "s", "tmux_window": "shared", "db_ids": ["one11111", "two22222"]}
    ]
    assert result["summary_warn"]
    assert "multiple DB identities" in result["summary_warn"][0]


def test_ac12_normalization_ordering_endpoint_source_and_read_only_db(tmp_path: Path) -> None:
    db = _database(
        tmp_path,
        ("zzzz9999", "s", "z", "worker"),
        ("aaaa1111", "s", "a", "worker"),
    )
    result = _scan(
        db,
        [_carrier(20), _carrier(10), _carrier(40), _carrier(30)],
        {
            20: {"CAO_TERMINAL_ID": "aaaa1111", "CAO_ENDPOINT": f"{PRODUCTION}/"},
            10: {"CAO_TERMINAL_ID": "zzzz9999"},
            40: _env("outside2", SANDBOX),
            30: _env("outside", SANDBOX),
        },
    )
    assert [row["mcp_pid"] for row in result["rows"]] == [20, 10]
    assert [row["mcp_pid"] for row in result["out_of_scope"]] == [30, 40]
    assert {row["mcp_pid"]: row["endpoint_source"] for row in result["rows"]} == {
        20: "env",
        10: "defaulted",
    }
    assert result["scan_endpoint"] == PRODUCTION
    with sqlite3.connect(db) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    assert tables == [("terminals",)]


def test_ac12_sqlite_connection_is_uri_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _database(tmp_path)
    real_connect = identity_service.sqlite3.connect
    calls: list[tuple[object, bool]] = []

    def recording_connect(database: object, *, uri: bool = False) -> sqlite3.Connection:
        calls.append((database, uri))
        return real_connect(database, uri=uri)

    monkeypatch.setattr(identity_service.sqlite3, "connect", recording_connect)
    _scan(db, [], {})
    assert len(calls) == 1
    assert "mode=ro" in str(calls[0][0])
    assert calls[0][1] is True


def test_ac12_vanished_pids_are_sorted(tmp_path: Path) -> None:
    db = _database(tmp_path)

    def gone(_pid: int) -> dict[str, str]:
        raise FileNotFoundError

    result = _scan(
        db,
        [_carrier(9), _carrier(2)],
        {},
        environ_reader=gone,
    )
    assert result["vanished_pids"] == [2, 9]


def test_ac12_default_host_and_cli_validators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _database(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setenv("CAO_API_HOST", "localhost")
    monkeypatch.delenv("CAO_API_PORT", raising=False)

    def fake_scan(*, endpoint: str, db_path: Path) -> dict[str, Any]:
        captured["endpoint"] = endpoint
        return {
            "scan_endpoint": endpoint,
            "scan_db": str(db_path),
            "rows": [],
            "out_of_scope": [],
            "vanished_pids": [],
            "summary": {
                "in_scope": 0,
                "ok": 0,
                "warn": 0,
                "fail": 0,
                "out_of_scope": 0,
                "vanished": 0,
                "scan_warning": None,
            },
            "summary_warn": [],
            "window_authority": [],
        }

    monkeypatch.setattr(verify_command, "scan_identity", fake_scan)
    good = CliRunner().invoke(cli, ["verify", "identity", "--db", str(db), "--json"])
    assert good.exit_code == 0
    assert captured["endpoint"] == "http://localhost:9889"

    runner = CliRunner()
    remote = runner.invoke(
        cli,
        ["verify", "identity", "--endpoint", "http://example.com:9889", "--db", str(db)],
    )
    with runner.isolated_filesystem():
        relative_path = _database(Path.cwd())
        relative = runner.invoke(cli, ["verify", "identity", "--db", relative_path.name])
    assert remote.exit_code == 2
    assert relative.exit_code == 2


def test_malformed_endpoint_url_exits_two(tmp_path: Path) -> None:
    db = _database(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["verify", "identity", "--endpoint", "http://[::1", "--db", str(db)],
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert "Invalid value" in result.output


def test_ac12_human_output_carries_pane_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _database(tmp_path, ("live9999", "s", "w", "worker"))
    document = _scan(
        db,
        [_carrier(1)],
        {1: _env("live9999")},
        windows={("s", "w"): True},
        panes={("s", "w"): PaneIdentityReadResult(reason="pane_cardinality")},
    )
    monkeypatch.setattr(verify_command, "scan_identity", lambda **_kwargs: document)
    result = CliRunner().invoke(cli, ["verify", "identity", "--db", str(db)])
    assert result.exit_code == 0
    assert "pane_cardinality" in result.output
