import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.services.session_manifest_service as svc

RAW = """---
name: supervisor
description: Safe charter digest
role: supervisor
provider: claude_code
skills: [cao-worker-protocols]
sessionBrief: required
mcpServers:
  secret:
    command: x
    args: [CANARY_ARGS]
    env: {TOKEN: CANARY_SECRET}
codexConfig: {key: CANARY_SECRET}
hooks: {PreToolUse: [{command: CANARY_HOOK}]}
resources: [CANARY_RESOURCE]
toolsSettings: {config: CANARY_CONFIG}
---
Never route policy from inventory.
"""


def _seed(monkeypatch):
    monkeypatch.setattr(
        svc,
        "list_agent_profiles",
        lambda: [{"name": "supervisor", "source": "local", "duplicated_in": ["built-in"]}],
    )
    monkeypatch.setattr(svc, "read_agent_profile_source", lambda name: RAW)
    monkeypatch.setattr(
        svc,
        "list_bases",
        lambda: [
            {
                "name": "codex",
                "provider": "codex",
                "agent_profile": "codex_base",
                "source_terminal_id": "base0001",
                "cwd": "/repo",
                "git_sha": "abc",
                "staleness_count": 2,
                "status": "ready",
                "updated_at": "now",
                "dirty_hashes": "CANARY_SECRET",
            }
        ],
    )
    monkeypatch.setattr(
        svc, "list_skills", lambda: [SimpleNamespace(name="worker", description="protocol")]
    )
    monkeypatch.setattr(
        svc,
        "list_workflows",
        lambda: [SimpleNamespace(name="flow", description="desc", source_path="/flow.yaml")],
    )
    monkeypatch.setattr(
        svc,
        "list_terminals_by_session",
        lambda name: [
            {
                "id": "term0001",
                "agent_profile": "supervisor",
                "provider": "claude_code",
                "caller_id": None,
                "provider_session_id": "CANARY_SECRET",
            }
        ],
    )
    monkeypatch.setattr(svc.status_monitor, "get_status", lambda tid: SimpleNamespace(value="idle"))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_working_directory",
        lambda tid: "/repo",
    )
    monkeypatch.setattr(
        svc,
        "deployment_status",
        lambda root: {
            "cli_path": "current",
            "differing_files": 0,
            "server": "current",
            "source_root": str(root),
        },
    )
    monkeypatch.setenv("CAO_SOURCE_REPO", "/repo")


def test_manifest_projection_and_renderer_are_safe_and_deterministic(monkeypatch):
    _seed(monkeypatch)
    manifest = svc.build_session_manifest("cao-test")
    brief = svc.render_session_brief(manifest)
    encoded = json.dumps(manifest)
    assert manifest["schema_version"] == "cao.session-manifest/v1"
    assert manifest["sections"] == {
        **{
            name: "ok"
            for name in (
                "profiles",
                "ready_bases",
                "skills",
                "workflows",
                "terminals",
                "activation",
            )
        },
        "tools": "not_collected",
        "ledger": "not_collected",
    }
    assert manifest["complete"] is False
    assert manifest["session"]["supervisors"] == ["term0001"]
    assert "charter" not in manifest["profiles"][0]
    assert manifest["profiles"][0]["charter_digest"] == "Safe charter digest"
    for canary in (
        "CANARY_SECRET",
        "CANARY_ARGS",
        "CANARY_HOOK",
        "CANARY_RESOURCE",
        "CANARY_CONFIG",
    ):
        assert canary not in encoded + brief
    assert manifest["profiles"][0]["skills"] == ["cao-worker-protocols"]
    assert manifest["profiles"][0]["role"] == "supervisor"
    assert "worker — protocol" in brief
    assert "use " not in brief.lower()


def test_auth_staleness_current_is_observation_only(monkeypatch, tmp_path):
    _seed(monkeypatch)
    marker = tmp_path / "auth.json"
    marker.write_text("{}")
    os = __import__("os")
    os.utime(marker, (100, 100))
    monkeypatch.setattr(
        svc,
        "list_terminals_by_session",
        lambda _name: [
            {
                "id": "term0001",
                "agent_profile": "supervisor",
                "provider": "codex",
                "caller_id": None,
                "tmux_session": "cao-test",
                "tmux_window": "w",
            }
        ],
    )
    provider = SimpleNamespace(
        provider_process_started_at=lambda _pid: 200,
        auth_state_path=lambda: marker,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        lambda _tid: provider,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.fork_context_service.pane_pid", lambda *_a: 1
    )
    manifest = svc.build_session_manifest("cao-test")
    assert manifest["terminals"][0]["auth_staleness"] == "current"
    encoded = json.dumps(manifest).lower()
    assert "session recover" not in encoded
    assert "provider-reauth" not in encoded


def test_http_endpoint_uses_builder_once_and_preserves_safe_snapshot(monkeypatch):
    from cli_agent_orchestrator.api.main import app

    _seed(monkeypatch)
    real_builder = svc.build_session_manifest
    calls = []

    def counted(session_name, terminal_id=None):
        calls.append((session_name, terminal_id))
        return real_builder(session_name, terminal_id)

    monkeypatch.setattr(svc, "build_session_manifest", counted)
    response = TestClient(app).get("/sessions/cao-test/manifest", headers={"Host": "localhost"})
    assert response.status_code == 200
    assert calls == [("cao-test", None)]
    encoded = response.content.decode()
    for canary in (
        "CANARY_SECRET",
        "CANARY_ARGS",
        "CANARY_HOOK",
        "CANARY_RESOURCE",
        "CANARY_CONFIG",
    ):
        assert canary not in encoded
    assert response.json()["profiles"][0]["charter_digest"] == "Safe charter digest"


def test_partial_failure_is_honest_and_noncore(monkeypatch):
    _seed(monkeypatch)
    monkeypatch.setattr(
        svc, "list_workflows", lambda: (_ for _ in ()).throw(RuntimeError("broken"))
    )
    manifest = svc.build_session_manifest("cao-test")
    assert manifest["complete"] is False
    assert manifest["sections"]["workflows"] == "error"
    assert manifest["profiles"] and manifest["skills"]
    assert svc.core_sections_complete(manifest)


@pytest.mark.parametrize(
    ("section", "target"),
    [
        ("profiles", "list_agent_profiles"),
        ("ready_bases", "list_bases"),
        ("skills", "list_skills"),
        ("workflows", "list_workflows"),
        ("activation", "deployment_status"),
    ],
)
def test_every_source_failure_is_isolated_with_core_threshold(monkeypatch, section, target):
    _seed(monkeypatch)
    monkeypatch.setattr(
        svc, target, lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(section))
    )
    manifest = svc.build_session_manifest("cao-test")
    assert manifest["complete"] is False
    assert manifest["sections"][section] == "error"
    assert any(error["section"] == section for error in manifest["errors"])
    assert svc.core_sections_complete(manifest) is (section not in {"profiles", "skills"})


def test_terminal_projection_failure_is_isolated(monkeypatch):
    _seed(monkeypatch)
    monkeypatch.setattr(
        svc.status_monitor, "get_status", lambda _tid: (_ for _ in ()).throw(RuntimeError("status"))
    )
    manifest = svc.build_session_manifest("cao-test")
    assert manifest["sections"]["terminals"] == "error"
    assert manifest["terminals"] == []
    assert svc.core_sections_complete(manifest)


def test_unconfigured_source_root_is_explicit(monkeypatch):
    _seed(monkeypatch)
    monkeypatch.delenv("CAO_SOURCE_REPO")
    manifest = svc.build_session_manifest("cao-test")
    assert manifest["activation"]["source_root"] is None
    assert any(e["code"] == "source_root_unconfigured" for e in manifest["errors"])


def test_digest_normalizes_raw_source_and_truncates_unicode(monkeypatch):
    digest = "界" * 160
    raw = (
        "---\r\nname: supervisor\r\ndescription: '  '\r\n---\r\n"
        "\r\n   \r\n  " + digest + "  \r\nsecond line\r\n"
    )
    monkeypatch.setattr(svc, "read_agent_profile_source", lambda _name: raw)
    projected = svc._charter_projection("supervisor")
    assert projected["charter_digest"] == "界" * 140
    assert len(projected["charter_digest"]) == 140


def test_schema_is_additive_and_renderer_is_inventory_only(monkeypatch):
    _seed(monkeypatch)
    manifest = svc.build_session_manifest("cao-test")
    required = {
        "schema_version": str,
        "generated_at": str,
        "complete": bool,
        "errors": list,
        "sections": dict,
        "session": dict,
        "profiles": list,
        "ready_bases": list,
        "skills": list,
        "workflows": list,
        "terminals": list,
        "ledger": dict,
        "activation": dict,
    }
    manifest["future_additive_key"] = {"accepted": True}

    def parse_compatible(payload):
        for key, expected_type in required.items():
            assert isinstance(payload[key], expected_type)
        nested = {
            "sections": {
                name: str
                for name in (
                    "profiles",
                    "ready_bases",
                    "skills",
                    "workflows",
                    "terminals",
                    "activation",
                    "tools",
                    "ledger",
                )
            },
            "session": {
                "name": str,
                "supervisors": list,
                "supervisor_terminal_id": (str, type(None)),
            },
            "profiles": {"name": str, "description": str, "skills": list, "charter_digest": str},
            "ready_bases": {"name": str, "provider": str, "staleness_count": int},
            "terminals": {"id": str, "provider": str, "status": str, "kind": str},
        }
        for key, fields in nested.items():
            row = payload[key][0] if isinstance(payload[key], list) else payload[key]
            for field, expected_type in fields.items():
                assert isinstance(row[field], expected_type)
        return payload

    assert parse_compatible(manifest)["future_additive_key"] == {"accepted": True}
    import copy

    removed = copy.deepcopy(manifest)
    del removed["profiles"][0]["charter_digest"]
    with pytest.raises((AssertionError, KeyError)):
        parse_compatible(removed)
    retyped = copy.deepcopy(manifest)
    retyped["terminals"][0]["status"] = 3
    with pytest.raises(AssertionError):
        parse_compatible(retyped)
    brief = svc.render_session_brief(manifest)
    forbidden = ("route to", "use codex for", "gate procedure", "fold round", "review lane")
    assert all(phrase not in brief.lower() for phrase in forbidden)
    assert "- tools: not_collected" in brief
    assert "- ledger: not_collected" in brief


def test_supervisor_projection_is_role_based_sorted_and_honest(monkeypatch):
    _seed(monkeypatch)
    rows = [
        {"id": "term-z", "agent_profile": "supervisor", "provider": "codex"},
        {"id": "term-a", "agent_profile": "supervisor", "provider": "codex"},
    ]
    monkeypatch.setattr(svc, "list_terminals_by_session", lambda _name: list(rows))
    first = svc.build_session_manifest("cao-test")["session"]
    rows.reverse()
    second = svc.build_session_manifest("cao-test")["session"]
    assert first["supervisors"] == second["supervisors"] == ["term-a", "term-z"]
    assert first["supervisor_terminal_id"] is None

    monkeypatch.setattr(
        svc,
        "list_terminals_by_session",
        lambda _name: [{"id": "term-worker", "agent_profile": "unknown"}],
    )
    zero = svc.build_session_manifest("cao-test")["session"]
    assert zero["supervisors"] == []
    assert zero["supervisor_terminal_id"] is None


def test_manifest_reads_seeded_profile_skill_directories_and_database(tmp_path, monkeypatch):
    import subprocess

    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import workflow_spec_service
    from cli_agent_orchestrator.utils import agent_profiles, skills

    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "seeded.md").write_text(
        "---\nname: seeded\ndescription: Seeded profile\nrole: supervisor\n"
        "provider: codex\nskills: [seeded-skill]\n---\nSeed charter\n",
        encoding="utf-8",
    )
    duplicate_dir = tmp_path / "duplicate-agents"
    duplicate_dir.mkdir()
    (duplicate_dir / "seeded.md").write_text(
        "---\nname: seeded\ndescription: Shadow copy\nrole: worker\n---\nShadow\n",
        encoding="utf-8",
    )
    skill_dir = tmp_path / "skills"
    (skill_dir / "seeded-skill").mkdir(parents=True)
    (skill_dir / "seeded-skill" / "SKILL.md").write_text(
        "---\nname: seeded-skill\ndescription: Seeded protocol\n---\nBody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", agent_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs", lambda: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
        lambda: [str(duplicate_dir)],
    )
    monkeypatch.setattr(skills, "SKILLS_DIR", skill_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs", lambda: []
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'manifest.db'}")
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE",
        tmp_path / "manifest.db",
        raising=True,
    )
    database._migrate_workflow_index()
    database.create_terminal("term0001", "cao-seeded", "supervisor-1", "codex", "seeded", None)

    repo = tmp_path / "base-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    database.register_provider_session(
        name="seed-base",
        provider="codex",
        session_uuid="seed-uuid",
        cwd=str(repo),
        agent_profile="codex_base",
        git_sha=sha,
        dirty_hashes="{}",
        summary="seed",
        source_terminal_id="term0001",
    )
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    (workflow_dir / "seed-flow.yaml").write_text(
        "name: seed-flow\ndescription: Seeded flow\nmode: sequential\nsteps:\n"
        "  - id: only-step\n    provider: claude_code\n    agent: developer\n"
        "    prompt: do the thing\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(workflow_spec_service, "WORKFLOW_SPEC_DIR", workflow_dir)
    monkeypatch.setattr(
        workflow_spec_service, "_safe_dir", lambda scan_dir=None: str(workflow_dir.resolve())
    )
    monkeypatch.setattr(
        svc.status_monitor, "get_status", lambda _tid: SimpleNamespace(value="idle")
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_working_directory",
        lambda _tid: str(tmp_path),
    )
    monkeypatch.setattr(
        svc,
        "deployment_status",
        lambda root: {
            "cli_path": "current",
            "differing_files": 0,
            "server": "current",
            "source_root": str(root),
        },
    )
    monkeypatch.setenv("CAO_SOURCE_REPO", str(tmp_path))

    manifest = svc.build_session_manifest("cao-seeded")
    seeded = next(row for row in manifest["profiles"] if row["name"] == "seeded")
    assert seeded["charter_digest"] == "Seeded profile"
    assert seeded["duplicated_in"] == ["custom"]
    assert {row["name"] for row in manifest["skills"]} == {"seeded-skill"}
    assert manifest["ready_bases"][0]["name"] == "seed-base"
    assert manifest["ready_bases"][0]["staleness_count"] == 1
    assert manifest["workflows"][0]["name"] == "seed-flow"
    assert manifest["terminals"][0]["id"] == "term0001"



# ─── F234: init-health parity tests ────────────────────────────────────────────

from datetime import datetime, timedelta, timezone

from cli_agent_orchestrator.services.fleet_service import _compute_init_health


def _seed_f234(monkeypatch, terminals):
    """Minimal seeding for F234 init-health tests."""
    monkeypatch.setattr(
        svc,
        "list_agent_profiles",
        lambda: [{"name": "dev", "source": "local", "duplicated_in": []}],
    )
    monkeypatch.setattr(
        svc,
        "read_agent_profile_source",
        lambda _name: "---\nname: dev\ndescription: Dev\nrole: developer\nprovider: codex\n---\nDev\n",
    )
    monkeypatch.setattr(svc, "list_bases", lambda: [])
    monkeypatch.setattr(svc, "list_skills", lambda: [SimpleNamespace(name="s", description="d")])
    monkeypatch.setattr(
        svc, "list_workflows", lambda: [SimpleNamespace(name="w", description="d", source_path="/w")]
    )
    monkeypatch.setattr(svc, "list_terminals_by_session", lambda _name: terminals)
    monkeypatch.setattr(svc.status_monitor, "get_status", lambda _tid: SimpleNamespace(value="idle"))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_working_directory",
        lambda _tid: "/repo",
    )
    monkeypatch.setattr(
        svc,
        "deployment_status",
        lambda root: {
            "cli_path": "current",
            "differing_files": 0,
            "server": "current",
            "source_root": str(root),
        },
    )
    monkeypatch.setenv("CAO_SOURCE_REPO", "/repo")


class TestManifestFleetInitHealthParity:
    """F234: manifest and fleet derive identical init_health for same input."""

    NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)

    CASES = [
        # (desc, row_fragment, expected_health, expect_error_status)
        ("ready", {"init_state": "ready"}, "ready", False),
        (
            "launching_within_deadline",
            {
                "init_state": "init_pending",
                "init_started_at": datetime(2026, 8, 15, 11, 59, 0, tzinfo=timezone.utc),
                "init_deadline_s": 120,
            },
            "launching",
            False,
        ),
        (
            "failed_overdue",
            {
                "init_state": "init_pending",
                "init_started_at": datetime(2026, 8, 15, 11, 0, 0, tzinfo=timezone.utc),
                "init_deadline_s": 60,
            },
            "failed",
            True,
        ),
        ("failed_notified", {"init_state": "init_failed_notified"}, "failed", True),
        ("failed_caller_gone", {"init_state": "init_failed_caller_gone"}, "failed", True),
        ("legacy_null", {}, None, False),
        (
            "malformed_no_started_at",
            {"init_state": "init_pending", "init_started_at": None, "init_deadline_s": 60},
            "failed",
            True,
        ),
        (
            "malformed_no_deadline",
            {"init_state": "init_pending", "init_started_at": datetime(2026, 8, 15, 11, 59, 0, tzinfo=timezone.utc), "init_deadline_s": None},
            "failed",
            True,
        ),
    ]

    @pytest.mark.parametrize("desc,row_fragment,expected_health,expect_error", CASES, ids=[c[0] for c in CASES])
    def test_fleet_and_manifest_agree(self, desc, row_fragment, expected_health, expect_error, monkeypatch):
        now = self.NOW
        # AC9: Direct call to _compute_init_health matches expected
        row = {"id": "term0001", "agent_profile": "dev", "provider": "codex", **row_fragment}
        assert _compute_init_health(row, now) == expected_health

        # Build manifest with same terminal
        _seed_f234(monkeypatch, [row])
        manifest = svc.build_session_manifest("test-session", _now=now)
        terminal = manifest["terminals"][0]

        # AC1: init_state present
        assert terminal["init_state"] == row_fragment.get("init_state")
        # AC2: init_health matches _compute_init_health
        assert terminal["init_health"] == expected_health
        # AC3/AC4/AC5: status override
        if expect_error:
            assert terminal["status"] == "error"
        else:
            assert terminal["status"] == "idle"  # base status from status_monitor mock


class TestInitHealthDeterminism:
    """F234 AC7: single _now used across all terminals in one manifest build."""

    def test_two_terminals_at_deadline_boundary_get_same_health(self, monkeypatch):
        """Seed two terminals straddling a deadline; both use the same injected _now."""
        now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
        # Terminal 1: started 59s ago with 60s deadline → launching (1s remaining)
        # Terminal 2: started 61s ago with 60s deadline → failed (1s past)
        terminals = [
            {
                "id": "term-a",
                "agent_profile": "dev",
                "provider": "codex",
                "init_state": "init_pending",
                "init_started_at": now - timedelta(seconds=59),
                "init_deadline_s": 60,
            },
            {
                "id": "term-b",
                "agent_profile": "dev",
                "provider": "codex",
                "init_state": "init_pending",
                "init_started_at": now - timedelta(seconds=61),
                "init_deadline_s": 60,
            },
        ]
        _seed_f234(monkeypatch, terminals)
        manifest = svc.build_session_manifest("test-session", _now=now)
        by_id = {t["id"]: t for t in manifest["terminals"]}

        # Both use the same _now — deterministic results
        assert by_id["term-a"]["init_health"] == "launching"
        assert by_id["term-a"]["status"] == "idle"
        assert by_id["term-b"]["init_health"] == "failed"
        assert by_id["term-b"]["status"] == "error"

    def test_injected_now_is_honored_not_ignored(self, monkeypatch):
        """M8 kill: if _now is ignored, this test detects it."""
        # Use a _now far in the future so a 60s deadline that started "now" would be launching
        # if real datetime.now() were used, but failed with the injected time.
        future_now = datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        started = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
        terminals = [
            {
                "id": "term-x",
                "agent_profile": "dev",
                "provider": "codex",
                "init_state": "init_pending",
                "init_started_at": started,
                "init_deadline_s": 60,
            },
        ]
        _seed_f234(monkeypatch, terminals)
        manifest = svc.build_session_manifest("test-session", _now=future_now)
        terminal = manifest["terminals"][0]
        # With real now() in 2026, this would be "launching"; with injected 2099, it's "failed"
        assert terminal["init_health"] == "failed"
        assert terminal["status"] == "error"


class TestInitHealthRecoveryFieldsUndisturbed:
    """F234 AC10: recovery/auth fields unaffected by init_health projection."""

    def test_failed_health_does_not_alter_recovery_or_auth(self, monkeypatch):
        now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
        terminals = [
            {
                "id": "term0001",
                "agent_profile": "dev",
                "provider": "codex",
                "init_state": "init_failed_notified",
                "recovery_state": "recovering",
                "recovery_error": "timeout",
                "fallback_terminal_id": "fallback1",
            },
        ]
        _seed_f234(monkeypatch, terminals)
        manifest = svc.build_session_manifest("test-session", _now=now)
        t = manifest["terminals"][0]
        # Status overridden to error
        assert t["status"] == "error"
        # Recovery fields preserved verbatim
        assert t["recovery_state"] == "recovering"
        assert t["recovery_error"] == "timeout"
        assert t["fallback_terminal_id"] == "fallback1"
        # Auth fields present and unchanged (default "unknown" from mock)
        assert t["auth_staleness"] == "unknown"


class TestInitHealthImportNoCycle:
    """F234 AC8: importing _compute_init_health from fleet_service causes no cycle."""

    def test_import_succeeds(self):
        """Both modules load without ImportError — self-proving via test collection."""
        from cli_agent_orchestrator.services import fleet_service, session_manifest_service

        assert hasattr(fleet_service, "_compute_init_health")
        assert hasattr(session_manifest_service, "build_session_manifest")



class TestProductionDefaultPathKillsM6M7:
    """S1 closure: exercise _now=None path via monkeypatched datetime.

    Kills M6 (naive-now) and M7 (per-row-now) by asserting:
      - the init-health default captures datetime.now once, plus the existing
        generated_at timestamp (two UTC calls total; per-row capture adds calls)
      - datetime.now receives timezone.utc as the tz argument (M6: naive variant
        returns a naive datetime, causing TypeError on tz-aware subtraction)
    """

    def test_production_default_uses_utc_once_across_two_rows(self, monkeypatch):
        """Two rows use one health-clock capture plus the generated_at timestamp."""
        FIXED_NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
        now_calls: list[tuple] = []

        _real_datetime = datetime

        class _ControlledDatetime(_real_datetime):
            """Subclass that intercepts .now() calls on the module symbol."""

            @classmethod
            def now(cls, tz=None):
                now_calls.append((tz,))
                if tz is None:
                    # M6 kill: return naive datetime — will TypeError on tz-aware subtraction
                    return _real_datetime(2026, 8, 15, 12, 0, 0)
                return FIXED_NOW

        monkeypatch.setattr(svc, "datetime", _ControlledDatetime)

        # Two terminals straddling a deadline — deterministic only if same _now used
        started = _real_datetime(2026, 8, 15, 11, 59, 30, tzinfo=timezone.utc)
        terminals = [
            {
                "id": "term-p1",
                "agent_profile": "dev",
                "provider": "codex",
                "init_state": "init_pending",
                "init_started_at": started,
                "init_deadline_s": 60,
            },
            {
                "id": "term-p2",
                "agent_profile": "dev",
                "provider": "codex",
                "init_state": "init_pending",
                "init_started_at": started - timedelta(seconds=61),
                "init_deadline_s": 60,
            },
        ]
        _seed_f234(monkeypatch, terminals)

        manifest = svc.build_session_manifest("test-session", _now=None)

        # M7 kill: datetime.now(utc) must be called exactly twice for 2 terminal
        # rows — once for _manifest_now capture + once for generated_at.
        # A per-row capture mutant would produce 3 calls (2 rows + generated_at).
        utc_calls = [c for c in now_calls if c == (timezone.utc,)]
        assert len(utc_calls) == 2, (
            f"Expected exactly 2 calls to datetime.now(timezone.utc) "
            f"(1 _manifest_now + 1 generated_at), got {len(utc_calls)}; "
            f"all now() calls: {now_calls}"
        )

        # Verify health computed correctly with the fixed time
        by_id = {t["id"]: t for t in manifest["terminals"]}
        assert by_id["term-p1"]["init_health"] == "launching"
        assert by_id["term-p1"]["status"] == "idle"
        assert by_id["term-p2"]["init_health"] == "failed"
        assert by_id["term-p2"]["status"] == "error"
