"""Top-level pytest configuration.

Sets process-wide env vars that disable optional v2.5 listeners so the
existing test suite (and CI) doesn't have to coordinate around real
port bindings or filesystem writes.

These knobs match how the lifespan reads them at runtime — see
``api/main.py``. Each is opt-out: the default is "feature on" in
production; tests flip them off.

Also exposes shared security fixtures (RSA keys, JWKS stub,
``AUTH0_*`` env, JWT mint helper) for tests outside ``test/security/``
that need to exercise the Auth0 paths.
"""

import os
import pathlib
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

# Every clean-process suite run gets a private initialized schema before any
# test module can import the global database engine. This prevents tests from
# depending on (or migrating) the installed production database.
_TEST_CAO_HOME = Path(tempfile.mkdtemp(prefix="cao-pytest-"))


# ---------------------------------------------------------------------------
# F113: Worktree-safe root-repo derivation
# ---------------------------------------------------------------------------
# In a normal checkout, the subrepo lives at <root-repo>/cli-agent-orchestrator/
# and tests can reach the root repo via parents[N]. In a git worktree (e.g.
# /tmp/<name>/), parent indices shift, breaking any hard-coded depth. This
# helper walks up first (covers normal checkouts), then falls back to
# git-common-dir (covers worktrees) to reliably locate the root repo.

def _derive_root_repo() -> "Path | None":
    """Find the root repository that contains the cli-agent-orchestrator subrepo.

    Returns None if the root repo cannot be located (e.g. running from an
    extracted tarball with no .git context).
    """
    import subprocess as _sp

    subrepo = Path(__file__).resolve().parent.parent  # test/ -> subrepo root

    # Strategy 1: walk up from subrepo looking for root-repo markers
    # (providers.toml.default or install.sh — both are root-repo-only files)
    for parent in subrepo.parents:
        if (parent / "providers.toml.default").exists() or (parent / "install.sh").exists():
            return parent

    # Strategy 2: git-common-dir fallback (worktree → main checkout's .git)
    try:
        common = Path(_sp.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=subrepo, text=True, stderr=_sp.DEVNULL,
        ).strip())
        # common = <root-repo>/cli-agent-orchestrator/.git → root repo = common.parents[1]
        candidate = common.parents[1]
        if candidate.exists() and (
            (candidate / "providers.toml.default").exists()
            or (candidate / "install.sh").exists()
        ):
            return candidate
    except Exception:
        pass

    return None


ROOT_REPO: "Path | None" = _derive_root_repo()
os.environ["CAO_HOME_DIR"] = str(_TEST_CAO_HOME)

from cli_agent_orchestrator.clients.database import engine, init_db  # noqa: E402

init_db()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Release the isolated suite database and remove its namespace."""
    engine.dispose()
    shutil.rmtree(_TEST_CAO_HOME, ignore_errors=True)


# Make the `mock_cli` test-fixture binary discoverable for the pytest
# session so MockCliProvider can `shlex.join(["mock_cli", ...])` without
# an absolute path. Not on PATH outside the test session — production
# code paths never reach this binary. See docs/mock-cli-provider.md.
_MOCK_CLI_BIN_DIR = pathlib.Path(__file__).parent / "providers" / "fixtures" / "bin"
if str(_MOCK_CLI_BIN_DIR) not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = f"{_MOCK_CLI_BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}"


# Expose the managed-subprocess fixtures (cao_server, cao_server_with_auth,
# cao_terminal) and the shared infra fixtures (jwt_factory, jwks_server,
# terminal_factory) to every test under test/ without per-conftest imports.
pytest_plugins = (
    "test.fixtures.cao_server",
    "test.fixtures.jwt_factory",
    "test.fixtures.jwks_server",
    "test.fixtures.terminal_factory",
    "test.plugins.rss_guard",
    "test.plugins.local_fixture_guard",
    "test.plugins.smoke_tags",
    "test.plugins.suite_slot",
    "test.plugins.tier_marks",
    "test.plugins.tier_budget",
    "test.plugins.quarantine",
    "test.plugins.quarantine_expiry",
    "test.plugins.env_capabilities",
    "test.plugins.resource_census",
    "test.plugins.basetemp_offload",
    "test.plugins.tmux_finalizer",
    "test.plugins.worktree_pruner",
    "test.plugins.xdist_remove_node_fix",
)


_AUTH_TEST_DOMAIN = "test.local"
_AUTH_TEST_AUDIENCE = "cao://test"


@pytest.fixture(scope="session")
def rsa_keys():
    """Generate a session-scoped RSA-2048 keypair for tests.

    F254 D25: promoted from function to session scope — the value is immutable
    and regenerating RSA-2048 per test is pure waste (0.17 s setup cluster).
    Local overrides in test/security/test_auth.py win by proximity (D11).
    """
    from authlib.jose import JsonWebKey
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_jwk = JsonWebKey.import_key(public_pem, {"kty": "RSA", "use": "sig", "kid": "test-kid"})
    return private_pem, public_jwk


def mint_test_token(
    private_pem: bytes,
    *,
    scopes: str = "cao:read cao:write cao:admin",
    audience: str = _AUTH_TEST_AUDIENCE,
    exp_offset: int = 300,
    iat_offset: int = 0,
) -> str:
    """Mint an RS256 JWT for tests. Mirrors test/security/test_auth.py."""
    from authlib.jose import JsonWebToken

    jwt = JsonWebToken(["RS256"])
    now = int(time.time())
    header = {"alg": "RS256", "kid": "test-kid"}
    claims: Dict[str, Any] = {
        "iss": f"https://{_AUTH_TEST_DOMAIN}/",
        "aud": audience,
        "iat": now + iat_offset,
        "exp": now + exp_offset,
        "scope": scopes,
    }
    token = jwt.encode(header, claims, private_pem)
    return token.decode("utf-8") if isinstance(token, bytes) else token


@pytest.fixture
def auth_enabled_env(monkeypatch):
    """Switch on Auth0 enforcement (AUTH0_DOMAIN + AUTH0_AUDIENCE)."""
    from cli_agent_orchestrator.security import auth as _auth_mod

    monkeypatch.setenv("AUTH0_DOMAIN", _AUTH_TEST_DOMAIN)
    monkeypatch.setenv("AUTH0_AUDIENCE", _AUTH_TEST_AUDIENCE)
    _auth_mod.reset_jwks_cache()
    yield
    _auth_mod.reset_jwks_cache()


@pytest.fixture
def mock_jwks(rsa_keys):
    """Stub the JWKS HTTP fetch with the in-process public key."""
    _, public_jwk = rsa_keys
    jwks = {"keys": [public_jwk.as_dict()]}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return jwks

    with patch("cli_agent_orchestrator.security.auth.requests.get", return_value=_Resp()):
        yield


@pytest.fixture(autouse=True)
def _no_llm_compile_in_tests(monkeypatch):
    """Default memory wiki compilation to append mode for every test.

    The production default is "llm", which drives whichever coding-agent CLI
    (claude / codex / kiro-cli) is installed on the developer's machine — each
    invocation cold-starts for tens of seconds and would make the suite both
    slow and non-hermetic. Tests that exercise the LLM path override this env
    var themselves or stub the ``wiki_compiler`` seams.
    """
    monkeypatch.setenv("CAO_MEMORY_COMPILE_MODE", "append")


@pytest.fixture(autouse=True)
def _enable_memory_in_tests(monkeypatch):
    """F488: memory defaults OFF at install, but tests assume it's on.

    Production default is now False (opt-in) to avoid spending Claude quota.
    Tests that exercise the disabled path override via their own monkeypatch.
    """
    monkeypatch.setenv("CAO_MEMORY_ENABLED", "true")


@pytest.fixture(autouse=True)
def _reset_backend_registry():
    """Prevent leaked backend singletons from crossing test boundaries (fixes #522)."""
    from cli_agent_orchestrator.backends import registry

    original = registry._backend
    registry._backend = None
    yield
    registry._backend = original


# ---------------------------------------------------------------------------
# F352: Global sender-token bypass for tests not exercising enforcement
# ---------------------------------------------------------------------------
_F352_ENFORCEMENT_MODULES = frozenset((
    "test_inbox_sender_token",
    "test_f352_sender_token_injection",
))


@pytest.fixture(autouse=True)
def _bypass_sender_token(request, monkeypatch):
    """Bypass verify_sender_token for tests that don't exercise enforcement.

    F352 sender-token enforcement rejects inbox POSTs without a valid
    X-CAO-Terminal-Token header. Most tests don't test the enforcement itself
    and should not be burdened with presenting tokens. The dedicated test modules
    that DO test enforcement are excluded from this bypass.
    """
    mod_name = request.node.module.__name__.rsplit(".", 1)[-1]
    if mod_name in _F352_ENFORCEMENT_MODULES:
        return
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_token_service.verify_sender_token",
        lambda _db, _sender_id, _presented: (True, ""),
    )


@pytest.fixture(autouse=True)
def _sim_leak_guard():
    """F254 D14: suite-wide guard — no sim clock/RNG/backend leaks across tests.

    Promoted from test/simulation/conftest.py to suite-wide scope. Extended
    to also assert backends.registry._backend is None on entry (D14 amendment).
    """
    from cli_agent_orchestrator.sim.clock import active as clock_active
    from cli_agent_orchestrator.sim.rng import active as rng_active
    from cli_agent_orchestrator.backends import registry

    # Pre-check: should not be installed
    leaked_clock_pre = clock_active()
    leaked_rng_pre = rng_active()
    if leaked_clock_pre is not None or leaked_rng_pre is not None:
        # Force cleanup from a previous leak
        import cli_agent_orchestrator.sim.clock as _clk
        import cli_agent_orchestrator.sim.rng as _rng
        _clk._active_clock = None
        _rng._active_rng = None

    yield

    # Post-check: must be clean after test
    leaked_clock = clock_active()
    leaked_rng = rng_active()
    if leaked_clock is not None or leaked_rng is not None:
        import cli_agent_orchestrator.sim.clock as _clk
        import cli_agent_orchestrator.sim.rng as _rng
        _clk._active_clock = None
        _rng._active_rng = None
        parts = []
        if leaked_clock is not None:
            parts.append("SimClock")
        if leaked_rng is not None:
            parts.append("SimRNG")
        pytest.fail(
            f"Sim binding leak detected: {', '.join(parts)} still installed after test. "
            "Wrap sim usage in a context manager or call world.uninstall() (D14)."
        )


@pytest.fixture(autouse=True)
def _isolate_seam_parity_and_incarnations():
    """F254 D24: unified teardown for seam-parity and process-incarnation tables.

    Replaces three separate autouse fixtures (two _clean_f138_incarnations +
    one _isolate_seam_parity_state) with a single DB session, reducing per-test
    overhead from 3 sessions to 1 (and 4→1 under test/services/).
    """
    yield

    from sqlalchemy.exc import SQLAlchemyError

    try:
        from cli_agent_orchestrator.clients.database import (
            SeamParityMismatchModel,
            SeamParityModel,
            SessionLocal,
        )
        from sqlalchemy import inspect, text

        with SessionLocal() as db:
            # Seam-parity cleanup (was _isolate_seam_parity_state)
            try:
                tables = set(inspect(db.get_bind()).get_table_names())
                for model in (SeamParityMismatchModel, SeamParityModel):
                    if model.__tablename__ in tables:
                        db.query(model).delete()
            except (AttributeError, SQLAlchemyError):
                # Migration and fault-injection tests intentionally replace
                # SessionLocal with incomplete schemas.
                pass

            # F138 incarnation cleanup (was _clean_f138_incarnations)
            try:
                db.execute(text("DELETE FROM process_incarnations"))
                db.execute(text("DELETE FROM orphan_reconcile_jobs"))
            except Exception:
                pass

            db.commit()
    except Exception:
        # If SessionLocal itself is broken (e.g. no DB file), skip silently.
        pass


@pytest.fixture(autouse=True)
def _hermetic_cao_env(monkeypatch, tmp_path):
    """Keep tests independent of CAO runtime identity and persisted settings.

    Settings reads use a fresh per-test file while ``CAO_HOME_DIR`` stays
    unchanged, so tests see documented defaults without invalidating assertions
    about the default home layout. Runtime env vars are removed before each test;
    tests can still set them explicitly after fixture setup. In particular,
    stripping ``CAO_TERMINAL_ID`` is load-bearing for vault-recall exclusion
    tests as well as sender-id defaults.
    """
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_service, "_server_settings_cache", None)
    monkeypatch.setattr(settings_service, "_server_settings_mtime_ns", -1)

    # server.py defaults sender_id to "supervisor" when unset
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    # server.py reads these for workflow_return context detection
    monkeypatch.delenv("CAO_WORKFLOW_RUN_ID", raising=False)
    monkeypatch.delenv("CAO_WORKFLOW_STEP_ID", raising=False)
    # cli/commands/info.py uses this for session detection
    monkeypatch.delenv("CAO_SESSION_NAME", raising=False)
    # HTTP clients must not inherit the enclosing CAO sandbox binding.
    monkeypatch.delenv("CAO_ENDPOINT", raising=False)
    monkeypatch.delenv("CAO_INSTANCE_ID", raising=False)
    # F469: tmux_argv() reads this at call-time; leaking a real socket into
    # tests that don't explicitly set one would route tmux commands to the
    # enclosing CAO session's server instead of the test's isolated server.
    monkeypatch.delenv("CAO_TMUX_SOCKET", raising=False)


@pytest.fixture(autouse=True)
def _clear_terminal_metadata_cache():
    """F351/B2: Clear the metadata TTL cache before and after each test.

    The module-level cache in database.py persists across tests within the same
    xdist worker. Tests that swap SessionLocal to an isolated DB would otherwise
    get stale metadata from a prior test's database.
    """
    from cli_agent_orchestrator.clients.database import clear_terminal_metadata_cache

    clear_terminal_metadata_cache()
    yield
    clear_terminal_metadata_cache()


@pytest.fixture
def isolated_memory_db(tmp_path, monkeypatch):
    """Route default memory sessions to an initialized per-test SQLite database."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients import database

    engine = create_engine(
        f"sqlite:///{tmp_path / 'memory-metadata.db'}",
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    try:
        yield engine
    finally:
        engine.dispose()
