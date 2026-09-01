"""F663 (#518) — EMPIRICAL gate: epoch recovery must thread caller_id so the
F620 laptop-shim decision is made by the SAME predicate as original creation.

The defect: ``epoch_recovery_service._recover_row`` re-created the base terminal
via ``create_terminal(...)`` but never threaded ``caller_id``. The F620 shim
block in ``terminal_service.create_terminal`` is guarded by ``if caller_id:``,
so on the recovery path the guard was always False, ``maybe_shim_env`` never
ran, and a recovered LAPTOP worker came back with no deny wrappers on PATH —
free to run ``pytest``/``mypy``/``uv`` on the laptop, which F620 forbids.

These arms are BEHAVIORAL, not text-presence. Each drives the real
``_recover_row`` through a ``create_terminal`` seam that faithfully replicates
the real F620 guard block (``if caller_id: maybe_shim_env(extra_env,
is_worker=True, repo_root=find_repo_root(cwd))``), calls the REAL
``laptop_shim.maybe_shim_env``, and then EXECUTES THE REAL ``pytest`` shim
binary from the composed PATH. The arm asserts on the shim's exit code
(97 + ``LAPTOP-DENIED``) — the very thing F620 exists to produce.

The mutation that falsifies: deleting ``caller_id=recovered_caller_id`` from the
single ``create_terminal(...)`` call in ``epoch_recovery_service`` makes the
seam receive ``caller_id=None`` even for a worker, so the guard is False, no
shim lands, and ``pytest`` runs (exit 0) instead of being denied. Arm 1 goes
RED; reverting the kwarg turns it GREEN again. That flip is the whole evidence.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services import epoch_recovery_service as service
from cli_agent_orchestrator.services import laptop_shim

# Repo root of THIS checkout: <repo>/test/services/test_f663_epoch_recovery_shim.py
_REAL_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_SHIM_DIR = _REAL_REPO_ROOT / "scripts" / "laptop-shims"
_DENY_MSG = "LAPTOP-DENIED"

_ACTIVE_TSV = textwrap.dedent("""\
    box@grok-box-1\tfrozen\t2026-08-27\tin use elsewhere
    box@grok-box-2\tactive\t-
    """)


def _fake_offload_repo(tmp_path: Path) -> Path:
    """A repo root carrying the F620 furniture: an active boxes.tsv and a
    laptop-shims dir whose ``pytest`` wrapper is the REAL deny shim from this
    checkout (copied so the composed PATH points at a genuine deny wrapper)."""
    repo = tmp_path / "offload-repo"
    shims = repo / "scripts" / "laptop-shims"
    shims.mkdir(parents=True)
    (repo / "scripts" / "boxes.tsv").write_text(_ACTIVE_TSV)
    real_pytest = _REAL_SHIM_DIR / "pytest"
    dst = shims / "pytest"
    dst.write_text(real_pytest.read_text())
    dst.chmod(0o755)
    return repo


def _seam_create_terminal(captured: dict, repo_root: Path):
    """A ``create_terminal`` replacement that faithfully replicates the real
    F620 guard block from ``terminal_service.create_terminal`` (the ONLY part
    of that function this behaviour depends on), then records the composed env.

    It reads the ``caller_id`` kwarg it is HANDED — so removing the threading in
    ``epoch_recovery_service`` (the mutation) is observed here as ``caller_id``
    arriving None, exactly as the production guard would see it.
    """

    async def _create(**kwargs):
        extra_env: dict[str, str] = {}
        caller_id = kwargs.get("caller_id")
        # Verbatim shape of terminal_service.create_terminal's F620 block.
        if caller_id:
            laptop_shim.maybe_shim_env(
                extra_env,
                is_worker=True,
                repo_root=str(repo_root),
                base_path="/usr/bin:/bin",
            )
        captured["caller_id"] = caller_id
        captured["extra_env"] = extra_env
        return SimpleNamespace(id="recovered")

    return _create


def _install_recovery_stubs(monkeypatch, *, source_caller_id, repo_cwd: str, captured: dict):
    """Wire ``_recover_row`` so only the create_terminal seam and the source
    terminal's caller_id vary; everything else is a benign pass-through."""
    row = {
        "name": "b",
        "provider": "codex",
        "session_uuid": "u",
        "cwd": repo_cwd,
        "agent_profile": "dev",
        "session_name": "cao-s",
        "summary": None,
        "source_terminal_id": "src-term",
    }
    monkeypatch.setattr(service, "_preflight", lambda *_: None)
    monkeypatch.setattr(
        service,
        "get_terminal_metadata",
        lambda tid: {"caller_id": source_caller_id} if tid == "src-term" else None,
    )
    monkeypatch.setattr(service, "generate_terminal_id", lambda: "recovered")
    token = SimpleNamespace(terminal_id="recovered")
    monkeypatch.setattr(service, "acquire_rebind_lease", lambda _: token)
    monkeypatch.setattr(service, "release_rebind_lease", lambda _: None)

    from cli_agent_orchestrator.services import provider_session_lease, session_lifecycle_lease

    monkeypatch.setattr(provider_session_lease, "acquire_provider_session_lease", lambda _: token)
    monkeypatch.setattr(provider_session_lease, "release_provider_session_lease", lambda _: None)
    monkeypatch.setattr(
        session_lifecycle_lease, "acquire_session_lifecycle_shared", lambda _: token
    )
    monkeypatch.setattr(session_lifecycle_lease, "release_session_lifecycle_lease", lambda _: None)

    monkeypatch.setattr(service, "create_terminal", _seam_create_terminal(captured, Path(repo_cwd)))
    monkeypatch.setattr(service, "mark_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(service, "staleness", lambda _: SimpleNamespace(changed_count=0))
    return row


def _run_pytest_shim_from(path_value: str) -> subprocess.CompletedProcess:
    """Invoke bare ``pytest`` resolved through ``path_value`` — the composed
    worker PATH. With the shim dir leading, this is the deny wrapper (exit 97);
    without it, it is whatever real pytest the base PATH resolves."""
    env = dict(os.environ)
    env.pop("LAPTOP_OK", None)
    env["PATH"] = path_value
    return subprocess.run(
        ["pytest", "--version"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# ARM 1 — recovered LAPTOP worker IS shimmed via the epoch path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm1_recovered_worker_is_shimmed_pytest_denied(monkeypatch, tmp_path):
    monkeypatch.delenv("LAPTOP_OK", raising=False)
    repo = _fake_offload_repo(tmp_path)
    captured: dict = {}
    row = _install_recovery_stubs(
        monkeypatch, source_caller_id="supervisor-9064394e", repo_cwd=str(repo), captured=captured
    )

    result, source = await service._recover_row(row, "cao-s")

    # The recovered lane is created AS a worker (its source terminal's caller_id
    # is threaded through), so the F620 guard fires and shims PATH.
    assert result["status"] == "resumed"
    assert captured["caller_id"] == "supervisor-9064394e"
    shim_dir = str(repo / "scripts" / "laptop-shims")
    assert captured["extra_env"]["PATH"].split(os.pathsep)[0] == shim_dir

    # Behavioral proof: pytest resolved from the composed PATH is the deny
    # wrapper — exit 97, LAPTOP-DENIED. This is the containment F620 restores.
    proc = _run_pytest_shim_from(captured["extra_env"]["PATH"])
    assert proc.returncode == 97, proc.stderr
    assert _DENY_MSG in proc.stderr


# ---------------------------------------------------------------------------
# ARM 2 — non-worker / no caller_id: behaves exactly as today, no shim.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm2_non_worker_source_no_shim(monkeypatch, tmp_path):
    repo = _fake_offload_repo(tmp_path)
    captured: dict = {}
    # Source terminal was an operator/supervisor launch — caller_id is None, so
    # the recovered lane is not a worker and must NOT be shimmed.
    row = _install_recovery_stubs(
        monkeypatch, source_caller_id=None, repo_cwd=str(repo), captured=captured
    )

    result, source = await service._recover_row(row, "cao-s")

    assert result["status"] == "resumed"
    assert captured["caller_id"] is None
    # No PATH key added — env composes exactly as before F663.
    assert "PATH" not in captured["extra_env"]


@pytest.mark.asyncio
async def test_arm2b_missing_source_terminal_row_no_shim(monkeypatch, tmp_path):
    """Source terminal row gone at recovery time -> no worker identity to
    inherit -> caller_id None -> no shim. The non-worker default, safely."""
    repo = _fake_offload_repo(tmp_path)
    captured: dict = {}
    row = _install_recovery_stubs(
        monkeypatch, source_caller_id=None, repo_cwd=str(repo), captured=captured
    )
    # Override so metadata lookup returns None (row absent) rather than a dict.
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: None)

    result, _source = await service._recover_row(row, "cao-s")

    assert result["status"] == "resumed"
    assert captured["caller_id"] is None
    assert "PATH" not in captured["extra_env"]


# ---------------------------------------------------------------------------
# ARM 3 — MALFORMED source metadata must DEGRADE, never abort recovery.
#
# The resolver runs before create_terminal and outside its try/except, so a
# raise here aborts the whole row (the base is not recovered at all) instead of
# falling back to the safe non-worker default. A truthy non-string caller_id is
# equally unacceptable: it is handed straight to create_terminal despite the
# declared ``str | None`` contract, so the shim guard fires on a value the rest
# of the stack cannot use.
# ---------------------------------------------------------------------------

# id -> the object get_terminal_metadata is made to return.
_MALFORMED_METADATA = {
    "non_mapping_string": "src-term",
    "non_mapping_list": ["caller_id"],
    "non_mapping_int": 7,
    "empty_mapping": {},
    "caller_id_int": {"caller_id": 123},
    "caller_id_list": {"caller_id": []},
    "caller_id_empty_string": {"caller_id": ""},
    "caller_id_absent": {"wrong_key": "x"},
    "caller_id_none": {"caller_id": None},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_MALFORMED_METADATA))
async def test_arm3_malformed_source_metadata_degrades_to_no_shim(monkeypatch, tmp_path, shape):
    repo = _fake_offload_repo(tmp_path)
    captured: dict = {}
    row = _install_recovery_stubs(
        monkeypatch, source_caller_id=None, repo_cwd=str(repo), captured=captured
    )
    metadata = _MALFORMED_METADATA[shape]
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: metadata)

    # Must NOT raise: recovery completes on the safe non-worker default.
    result, _source = await service._recover_row(row, "cao-s")

    assert result["status"] == "resumed"
    assert captured["caller_id"] is None
    assert "PATH" not in captured["extra_env"]


# ---------------------------------------------------------------------------
# Resolver unit: _recovered_caller_id maps source_terminal_id -> its caller_id,
# and degrades to None for every absent/malformed shape.
# ---------------------------------------------------------------------------


def test_recovered_caller_id_resolution(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_terminal_metadata",
        lambda tid: {"caller_id": "sup-1"} if tid == "src" else None,
    )
    assert service._recovered_caller_id({"source_terminal_id": "src"}) == "sup-1"
    # No source terminal id -> None (no metadata lookup needed).
    assert service._recovered_caller_id({"source_terminal_id": None}) is None
    assert service._recovered_caller_id({}) is None
    # Source id present but row gone (deleted terminal) -> None.
    assert service._recovered_caller_id({"source_terminal_id": "ghost"}) is None


@pytest.mark.parametrize("shape", sorted(_MALFORMED_METADATA))
def test_recovered_caller_id_returns_none_for_malformed_metadata(monkeypatch, shape):
    metadata = _MALFORMED_METADATA[shape]
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: metadata)

    resolved = service._recovered_caller_id({"source_terminal_id": "src"})

    # Never raises, and never yields a non-string: exactly None.
    assert resolved is None


def test_recovered_caller_id_returns_the_string_for_a_valid_worker(monkeypatch):
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: {"caller_id": "sup-2"})
    resolved = service._recovered_caller_id({"source_terminal_id": "src"})
    assert resolved == "sup-2"
    assert isinstance(resolved, str)
