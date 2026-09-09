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
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator
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
        common = Path(
            _sp.check_output(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=subrepo,
                text=True,
                stderr=_sp.DEVNULL,
            ).strip()
        )
        # common = <root-repo>/cli-agent-orchestrator/.git → root repo = common.parents[1]
        candidate = common.parents[1]
        if candidate.exists() and (
            (candidate / "providers.toml.default").exists() or (candidate / "install.sh").exists()
        ):
            return candidate
    except Exception:
        pass

    return None


ROOT_REPO: "Path | None" = _derive_root_repo()
os.environ["CAO_HOME_DIR"] = str(_TEST_CAO_HOME)
# F549 (#405): pin the kiro agents dir into the test home too. Without this,
# CAO_AGENTS_DIR defaults to the REAL ~/.kiro/agents and any test that calls
# install_agent (or another provider-file write) clobbers the user's live agent
# configs (incident 2026-08-28). Set at import, BEFORE constants.py binds it.
os.environ["CAO_AGENTS_DIR"] = str(_TEST_CAO_HOME / "kiro-agents")

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
# terminal_service process-global registries
# ---------------------------------------------------------------------------
# ``terminal_service`` keeps eleven module-level registries that are correct for
# a long-lived server process and wrong for a test worker, because the worker
# runs thousands of "processes" back to back in one interpreter. The names they
# key on are not unique across tests -- a full-suite instrumented run on
# grok-box-005 (2026-09-04, main 9b93dd24) caught 45 tests ending with state they
# did not create, keyed on ids as generic as ``worker1``, ``test1234``,
# ``queued``, ``receiver`` and sessions ``cao-test``, ``cao-race``. Two of those
# registries are gates: ``_memory_injected_terminals`` makes the first-message
# memory injection a no-op for an id it already holds, and
# ``_f160_retried_terminals`` spends a terminal's one watchdog re-arm, which is
# in-memory by design. A test that picks a used id therefore
# exercises a different branch than it does in isolation.
#
# Save/restore rather than clear: whatever a class- or module-scoped fixture put
# there during setup is still there for the rest of that fixture's tests, while
# anything a test itself adds is gone before the next one starts. Restoring in
# place (not rebinding) keeps the aliases in ``from ... import`` test modules --
# several import these registries by name -- pointing at the live objects.
_TERMINAL_SERVICE_REGISTRIES = (
    "_memory_injected_terminals",
    "_deferred_init_tasks",
    "_deferred_reconciler_tasks",
    "_f160_retried_terminals",
    "_deferred_tasks_by_terminal",
    "_fork_refresh_locks",
    "_cap_admission_locks",
    "_cap_reservations",
    "_cap_publishing_ids",
    "_cap_token_seq",
    "_cap_gen",
)
# Same treatment, but these are plain ints rather than containers.
_TERMINAL_SERVICE_COUNTERS = ("_cap_registry_cardinality", "_cap_registry_warned_at")


_TERMINAL_SERVICE_MODULE = "cli_agent_orchestrator.services.terminal_service"


@pytest.fixture(autouse=True)
def _isolate_terminal_service_registries() -> Iterator[None]:
    """Confine terminal_service's module-global registries to one test.

    Reads ``sys.modules`` rather than importing: this fixture is autouse over the
    whole suite, and importing terminal_service here would drag it (and its
    dependency graph) into thousands of tests that never touch it. That is not
    hypothetical — at base, a trivial test runs with the module absent from
    ``sys.modules``, so an unconditional import here changes the process state
    every test observes. There is nothing to isolate while the module is unloaded
    anyway: its registries cannot hold another test's state until something
    imports it.
    """
    module = sys.modules.get(_TERMINAL_SERVICE_MODULE)
    # Values are (live container, copy taken at setup). The containers are a mix
    # of dict and set, so this is deliberately not narrowed past Any: every use
    # below is clear()/update(), which both support identically.
    saved: Dict[str, tuple[Any, Any]] = {}
    counters: "Dict[str, Any] | None" = None
    if module is not None:
        for name in _TERMINAL_SERVICE_REGISTRIES:
            live = getattr(module, name)
            saved[name] = (live, live.copy())
        counters = {name: getattr(module, name) for name in _TERMINAL_SERVICE_COUNTERS}

    yield

    module = sys.modules.get(_TERMINAL_SERVICE_MODULE)
    if module is None:
        return
    if counters is not None:
        for name, (live, snapshot) in saved.items():
            live.clear()
            live.update(snapshot)
        for name, value in counters.items():
            setattr(module, name, value)
    else:
        # The test imported it. A fresh import starts every registry empty and
        # both counters at zero, so whatever is in them now, this test put there.
        for name in _TERMINAL_SERVICE_REGISTRIES:
            getattr(module, name).clear()
        for name in _TERMINAL_SERVICE_COUNTERS:
            setattr(module, name, 0)


# ---------------------------------------------------------------------------
# F352: Global sender-token bypass for tests not exercising enforcement
# ---------------------------------------------------------------------------
_F352_ENFORCEMENT_MODULES = frozenset(
    (
        "test_inbox_sender_token",
        "test_f352_sender_token_injection",
        # F707 (#562): the inbox drain edges reuse verify_sender_token to bind
        # the caller to the route terminal — that module exercises enforcement.
        "test_f707_drain_authz",
    )
)


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
    from cli_agent_orchestrator.backends import registry
    from cli_agent_orchestrator.sim.clock import active as clock_active
    from cli_agent_orchestrator.sim.rng import active as rng_active

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
        from sqlalchemy import inspect, text

        from cli_agent_orchestrator.clients.database import (
            SeamParityMismatchModel,
            SeamParityModel,
            SessionLocal,
        )

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
def _f549_guard_real_home_dirs():
    """F549 (#405): fail-loud if a test writes into the user's REAL home dirs.

    CAO_HOME_DIR and CAO_AGENTS_DIR are pinned into the test home at import, so
    a correctly-behaved test never touches ``~/.kiro`` or
    ``~/.aws/cli-agent-orchestrator``. This snapshots the mtimes of those real
    dirs (and the kiro agents dir) at setup and asserts they are unchanged at
    teardown, catching any code path that still resolves a real dir at import
    time and writes to it (the 2026-08-28 install-clobber incident shape).
    """
    import os as _os

    watched = [
        pathlib.Path.home() / ".kiro" / "agents",
        pathlib.Path.home() / ".aws" / "cli-agent-orchestrator" / "agent-store",
        pathlib.Path.home() / ".aws" / "cli-agent-orchestrator" / "agent-context",
    ]

    def _snapshot():
        snap = {}
        for d in watched:
            if d.exists():
                snap[d] = d.stat().st_mtime_ns
                for child in d.rglob("*"):
                    try:
                        snap[child] = child.stat().st_mtime_ns
                    except OSError:
                        pass
        return snap

    before = _snapshot()
    yield
    after = _snapshot()
    # New or modified real-home paths => a test wrote outside the test home.
    changed = [str(p) for p in set(before) | set(after) if before.get(p) != after.get(p)]
    assert not changed, (
        "F549: a test mutated the user's REAL home dirs (expected all writes under "
        f"the test home): {changed[:10]}"
    )


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
    # F550 (#406): the rest of the worker-terminal identity block that CAO
    # injects alongside CAO_TERMINAL_ID (clients/tmux.py:889-891,
    # backends/herdr_backend.py:1131-1134). Stripping the id but leaving its
    # siblings made the isolation half-done: several providers merge
    # CAO_TERMINAL_TOKEN out of os.environ into the MCP server env they write
    # (omp.py:199-203, claude_code.py:958, kimi_cli.py:412, cursor_cli.py:528),
    # so test_omp_unit's expected-env assertion picked up the token of whatever
    # worker terminal the suite happened to run inside and failed there while
    # passing in a clean shell (observed 2026-08-28, kiro_dev-e08be272).
    # Tests that want these set them explicitly after fixture setup.
    monkeypatch.delenv("CAO_TERMINAL_TOKEN", raising=False)
    monkeypatch.delenv("CAO_PROCESS_INCARNATION", raising=False)
    monkeypatch.delenv("CAO_ARTIFACTS_DIR", raising=False)
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
    # F703 (#558): pin the codex home into the test tree, the codex analogue of
    # the CAO_HOME_DIR / CAO_AGENTS_DIR pins at import (F549 #405). Without it,
    # persona_context.resolve_codex_home falls back to provider_home("codex"),
    # i.e. the operator's REAL ~/.codex, for every terminal with no persona plan
    # — which is every unit test that exercises CodexProvider.launch. The F597
    # pre-trust writer then appended a [projects."…"] trust table there on each
    # such test; 52 junk entries built up in the live config on 2026-09-01.
    # setenv (not delenv): resolve_codex_home consults CODEX_HOME only for that
    # production fallback, so pinning it redirects exactly the leaking path and
    # leaves live/retained persona homes resolving as they do in production.
    # Tests that need their own codex home still set it after fixture setup.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


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


# ---------------------------------------------------------------------------
# F767 (#624): FIFO reader thread leak — deterministic teardown + leak guard
# ---------------------------------------------------------------------------
# A test that exercised the real create_terminal() flow (or called
# fifo_manager.create_reader directly) started a daemon reader thread but often
# never called stop_reader. Those daemon threads do not block interpreter exit,
# but at process shutdown — while they are still spinning in select()/os.read()
# — the interpreter tears their module globals down underneath them and each
# raises, printing an "Exception ignored in thread" traceback to stderr. A full
# suite that leaks many readers compounded this into ~42 MB of stderr (WP-ARCH
# 3b build lane, grok-box-004 2026-09-05) that swamped the pytest reporting
# stream and cost two wasted A/B arms until stderr was redirected to its own
# file. Two guards below:
#   (1) per-test autouse teardown: drain any leaked FIFO readers deterministically
#       so none survive into shutdown;
#   (2) session-scoped guard: fail the run if any NON-DAEMON thread the suite
#       started outlives it (a genuinely un-collectable thread — every CAO
#       application thread is daemon=True by construction).


@pytest.fixture(autouse=True)
def _drain_leaked_fifo_readers() -> Iterator[None]:
    """Stop any FIFO reader/watchdog threads a test left running (issue #624).

    Reads sys.modules rather than importing fifo_reader unconditionally: the
    module is only present once a test has touched the terminal/FIFO stack, so
    a trivial test that never imports it pays nothing and its process state is
    unchanged (same discipline as _isolate_terminal_service_registries above).

    Teardown surfaces failures rather than swallowing them (issue #624 §Gate
    blocker 3): a blanket ``except Exception: pass`` erased exactly the cleanup
    failures this fixture exists to catch. Instead:

    - if ``stop_all_readers`` RAISES, the exception propagates and pytest
      reports the test in error (nothing is masked);
    - if ``stop_all_readers`` returns a non-empty survivor list (readers that
      refused to die within the join bound), the fixture fails the test with the
      survivor ids, so a genuine teardown leak is loud instead of silent.

    Running in teardown (after ``yield``), a failure here is reported against
    the just-finished test without erasing that test's own body result — a
    passing body still surfaces the leak, a failing body still surfaces its own
    assertion.
    """
    yield
    module = sys.modules.get("cli_agent_orchestrator.services.fifo_reader")
    if module is None:
        return
    fifo_manager = getattr(module, "fifo_manager", None)
    if fifo_manager is None:
        return
    survivors = fifo_manager.stop_all_readers()
    if survivors:
        pytest.fail(
            "FIFO reader thread(s) survived teardown drain: "
            f"{', '.join(survivors)}. stop_all_readers() could not join them "
            "within the bound — a reader was leaked (issue #624)."
        )


@pytest.fixture(scope="session", autouse=True)
def _no_leaked_non_daemon_threads() -> Iterator[None]:
    """Fail the session if a non-daemon thread the suite started survives it.

    Baselines the non-daemon threads alive at session start (pytest's own
    machinery, the interpreter's) and asserts no NEW non-daemon thread is still
    alive at session end. A leaked reader thread is daemon, so this does not
    catch it directly — guard (1) handles that class — but it is the durable
    backstop the issue asks for: any thread that could actually block a clean
    interpreter exit is a hard failure, surfaced by name.

    Third-party test-machinery threads are excluded by name prefix: the
    ``pytest-timeout`` plugin runs a NON-daemon per-item ``Timer`` thread named
    after the current test node id, and the timer for the final test can still
    be alive at session teardown — that is the plugin's business, not a CAO
    leak, so it must not fail the run.
    """
    # Prefixes of threads owned by test machinery / third-party plugins, not by
    # CAO code. pytest-timeout names its Timer thread "<plugin> <nodeid>".
    _IGNORED_THREAD_PREFIXES = ("pytest_timeout", "pytest-timeout")

    baseline = {t.ident for t in threading.enumerate()}
    yield
    survivors = [
        t
        for t in threading.enumerate()
        if t.ident not in baseline
        and t is not threading.main_thread()
        and t.is_alive()
        and not t.daemon
        and not t.name.startswith(_IGNORED_THREAD_PREFIXES)
    ]
    if survivors:
        names = ", ".join(sorted(f"{t.name}(id={t.ident})" for t in survivors))
        pytest.fail(
            "Non-daemon thread(s) leaked past the test session: "
            f"{names}. A surviving non-daemon thread blocks a clean interpreter "
            "exit and points at a service/fixture that started a thread without "
            "joining it (issue #624)."
        )
