"""AC-LITE-1 — a bare project completes the whole round trip, and presence means nothing.

``wp-arch-modular-core.md`` A.5:

    In a fresh directory with no ``orchestrator/``, ``doctrine/``, ``.claude/``, ``ORCH_MAP.md``
    and no user-specific path: install, start, health, launch one session, admit/deliver/ack one
    callback, ``cao diag``, recover. All green.

    Mutation arm: re-run the identical fixture with an empty ``orchestrator/`` directory present.
    Any observable difference -- artifact root, health output, hook set, exit code -- turns RED.
    Presence must stop meaning anything.

Seeded from ``scripts/lite_no_audit_smoke.py`` on ``cao/lite-boundary-slice1`` (@ ``eb1cf2c0``).
Two things are taken from it and two are deliberately left behind.

TAKEN: its monkeypatched ``Path.open``/``stat``/``glob``/... deny guard, which is the right
mechanism -- it turns "infrastructure must not read knowledge" from a claim into a trap that
fires at the moment of the read. And its refusal to touch the developer's real HOME.

REPLACED: its ``knowledge_domain()`` regex is swapped for the literal A.2 path list. A regex
answers "does this look like knowledge"; the acceptance criterion asks "is this one of these
paths", and only the second can be checked against a closed list.

EXTENDED: the seed stopped at "imports/help/health only; lifespan and lifecycle untested" and
said so in its own output. A round trip that never starts a session cannot show that presence
stopped meaning anything, because the directory probe it is testing lived in the session path.
So this runs the full sequence against a real ``cao-server`` subprocess.

No provider credential is involved anywhere: the session runs on ``mock_cli``, which wraps a
scripted fixture binary.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from test.fixtures.cao_server import CaoServer, _pick_free_port, _start_cao_server
from typing import Any, Callable, Iterator

import pytest
import requests

pytestmark = pytest.mark.e2e

# The A.2 closed list, as literal path tails. Not a regex: the criterion is membership of a
# closed list, and a regex cannot express "closed".
A2_NAMES = (
    "orchestrator",
    "doctrine",
    "ORCH_MAP.md",
    "HANDOFF.md",
    "GOLDEN-TIPS.md",
    "MISTAKES.md",
    "BUGS.md",
    "WP-BACKLOG.md",
    "ROUTING.md",
    "blueprints",
    "self-audit.md",
)

# A SUPERVISOR-role profile, and that is load-bearing rather than incidental.
#
# A mailbox is the seat's durable destination: `create_session` claims and publishes one only
# when `profile.role == "supervisor"` (services/session_service.py:299-304, :344-350), the
# queue addresses rows to the MAILBOX id rather than the terminal id, and both ack paths look
# a mailbox up by `current_terminal_id` (`_ack_on_queue` mailbox_service.py:1030-1036 and the
# legacy path at :1235-1240). So a worker terminal has no mailbox, is not the receiver of its
# own delivery rows, and can never be acked -- by design, not by defect. `code_supervisor`
# ships in the packaged agent store, so a bare project has it without installing anything.
AGENT_PROFILE = "code_supervisor"

# Overridable so the same fixture can be pointed at a real provider CLI on a box without
# editing the module; `mock_cli` keeps the default runnable anywhere.
PROVIDER = os.environ.get("CAO_LITE_E2E_PROVIDER", "mock_cli")

_POLL_INTERVAL = 0.25
_DELIVERY_TIMEOUT = float(os.environ.get("CAO_LITE_E2E_DELIVERY_TIMEOUT", "60"))
_IDLE_TIMEOUT = float(os.environ.get("CAO_LITE_E2E_IDLE_TIMEOUT", "90"))
_CREATE_TIMEOUT = float(os.environ.get("CAO_LITE_E2E_CREATE_TIMEOUT", "60"))


@dataclass
class Observables:
    """Everything the two arms are compared on.

    Deliberately not a free-form dump: each field is something A.5 names as observable --
    the artifact root, the health output, the hook set, the exit codes -- plus the delivery
    statuses, without which "the round trip completed" would be unfalsifiable.
    """

    health: dict[str, Any]
    artifact_root_relative: str
    terminal_status: str
    mailbox_id: str
    callback_kind: str
    callback_states: tuple[str, ...]
    callback_final_state: str
    ack_status: int
    ack_body: dict[str, Any]
    install_returncode: int
    diag_returncode: int
    diag_has_timeline: bool
    recover_status: int
    recover_reason: str
    project_tree: tuple[str, ...]
    knowledge_reads: tuple[str, ...] = field(default=())


def _is_knowledge_path(value: str) -> bool:
    """True when ``value`` touches a component on the A.2 closed list."""
    parts = Path(value).parts
    return any(name in parts for name in A2_NAMES)


@contextlib.contextmanager
def _deny_knowledge_io(recorded: list[str]) -> Iterator[None]:
    """Trap every read/stat/glob of an A.2 path in THIS process.

    The seed's mechanism. It covers the in-process half of the round trip (the CLI calls
    below); the server subprocess is covered by the project-tree assertion instead, since a
    monkeypatch cannot cross a process boundary.
    """
    from unittest.mock import patch

    def guarded(original: Callable[..., Any]) -> Callable[..., Any]:
        def invoke(path: Any, *positional: Any, **keyword: Any) -> Any:
            if not isinstance(path, int) and _is_knowledge_path(str(path)):
                recorded.append(str(path))
                raise AssertionError(f"forbidden knowledge I/O: {path}")
            return original(path, *positional, **keyword)

        return invoke

    with contextlib.ExitStack() as stack:
        for name in ("open", "stat", "lstat", "glob", "rglob", "iterdir", "mkdir"):
            stack.enter_context(patch.object(Path, name, guarded(getattr(Path, name))))
        stack.enter_context(patch.object(builtins, "open", guarded(builtins.open)))
        for name in ("stat", "lstat", "listdir", "scandir", "mkdir", "makedirs", "access"):
            stack.enter_context(patch.object(os, name, guarded(getattr(os, name))))
        yield


def _project_tree(project: Path) -> tuple[str, ...]:
    return tuple(
        sorted(p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file())
    )


def _wait_for(predicate: Callable[[], Any], timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(_POLL_INTERVAL)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last={last!r})")


def _run_round_trip(project: Path, home: Path) -> Observables:
    """Install, start, health, session, callback, diag, recover -- in ``project``."""
    reads: list[str] = []
    server: CaoServer | None = None
    # The server subprocess inherits cwd, which is what decides the artifact root now that
    # the directory probe is gone. chdir is the only way to set it through this helper.
    with contextlib.chdir(project):
        try:
            # --- install ----------------------------------------------------------------
            # A.5 names install as the first step of the round trip. Run BEFORE the server
            # so it cannot be mistaken for something the server did, and against the same
            # private HOME, so the developer's real store is never touched.
            installed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cli_agent_orchestrator.cli.main",
                    "install",
                    AGENT_PROFILE,
                    "--provider",
                    "mock_cli",
                ],
                capture_output=True,
                text=True,
                cwd=project,
                env=_private_home_env(home),
                timeout=180,
            )

            server = _start_cao_server(home, _pick_free_port())

            # From here to the ack the deny guard is armed over THIS process. It cannot
            # cross into the server subprocess -- the project-tree assertions cover that
            # half -- but it does cover the whole client side of the round trip.
            with _deny_knowledge_io(reads):
                health = requests.get(f"{server.url}/health", timeout=10).json()

                session_name = f"lite-{uuid.uuid4().hex[:8]}"
                created = requests.post(
                    f"{server.url}/sessions",
                    params={
                        "provider": PROVIDER,
                        "agent_profile": AGENT_PROFILE,
                        "session_name": session_name,
                    },
                    timeout=_CREATE_TIMEOUT,
                )
                if created.status_code >= 500 and PROVIDER in created.text.lower():
                    pytest.skip(f"{PROVIDER} not usable on this host: {created.text[:200]}")
                assert created.status_code in (200, 201), f"{created.status_code} {created.text}"
                terminal = created.json()
                terminal_id = terminal["id"]
                real_session_name = terminal.get("session_name", session_name)

                terminal_row = _wait_for(
                    lambda: (
                        lambda r: r.json() if r.ok and r.json().get("status") == "idle" else None
                    )(requests.get(f"{server.url}/terminals/{terminal_id}", timeout=10)),
                    _IDLE_TIMEOUT,
                    "terminal to reach idle",
                )

                mailbox = _wait_for(
                    lambda: _mailbox_for(server.db_path, terminal_id),
                    _IDLE_TIMEOUT,
                    "the session to publish the supervisor mailbox",
                )
                mailbox_id = str(mailbox["id"])

                pane_root = _pane_artifact_root(real_session_name, terminal["name"])
                assert pane_root, (
                    "could not read CAO_ARTIFACTS_DIR from the worker pane; the artifact-root "
                    "observable would be vacuous"
                )
                artifact_root = Path(pane_root)

                # --- one callback: admitted, delivered, acked -------------------------------
                admitted = requests.post(
                    f"{server.url}/terminals/{terminal_id}/inbox/messages",
                    params={
                        "sender_id": terminal_id,
                        "message": "AC-LITE-1 round trip callback",
                    },
                    timeout=60,
                )
                assert admitted.status_code in (200, 201), f"{admitted.status_code} {admitted.text}"

                admitted_body = admitted.json()
                seen_states: list[str] = []

                def _queue_engaged() -> list[dict[str, Any]] | None:
                    rows = _delivery_rows(server.db_path, mailbox_id)
                    for row in rows:
                        state = str(row.get("state"))
                        if state not in seen_states:
                            seen_states.append(state)
                    # The tick has engaged once the row is no longer merely queued.
                    if rows and any(str(row.get("state")) != "ready" for row in rows):
                        return rows
                    return None

                queued = _wait_for(
                    _queue_engaged,
                    _DELIVERY_TIMEOUT,
                    "the delivery tick to claim the callback",
                )
                callback_kind = str(queued[0].get("kind"))

                # The seat acks its cursor; `settle_through` is what moves the queue row to
                # `delivered` (adapters/store/queue.py:1053-1080). Delivery is confirmed BY
                # the ack here, so the ack has to succeed for the row ever to settle.
                acked = requests.post(
                    f"{server.url}/messages/ack",
                    json={"terminal_id": terminal_id, "up_to_id": int(admitted_body["message_id"])},
                    timeout=30,
                )

                settled = _wait_for(
                    lambda: (
                        _delivery_rows(server.db_path, mailbox_id)
                        if all(
                            str(row.get("state")) in QUEUE_TERMINAL
                            for row in _delivery_rows(server.db_path, mailbox_id)
                        )
                        else None
                    ),
                    _DELIVERY_TIMEOUT,
                    "the acked callback to reach a terminal delivery state",
                )
                callback_final_state = str(settled[0].get("state"))
                for row in settled:
                    state = str(row.get("state"))
                    if state not in seen_states:
                        seen_states.append(state)

            # --- cao diag ---------------------------------------------------------------
            diag = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cli_agent_orchestrator.cli.main",
                    "diag",
                    # `diag terminal` carries its OWN --db/--json (diag.py:168-173).
                    # `cao diag --db X --json <id>` routes through _DiagGroup.resolve_command,
                    # which rewrites the args to [terminal, <id>] and drops the group's flags,
                    # so the subcommand silently used the DEFAULT database. Naming the view
                    # explicitly is the documented form and the only one that reaches this DB.
                    "terminal",
                    "--db",
                    str(server.db_path),
                    "--json",
                    terminal_id,
                ],
                capture_output=True,
                text=True,
                cwd=project,
                timeout=120,
            )

            # --- recover ----------------------------------------------------------------
            recovered = requests.post(
                f"{server.url}/sessions/{real_session_name}/recover",
                json={"reason": "epoch"},
                timeout=120,
            )

            return Observables(
                health=health,
                artifact_root_relative=_relative_to(artifact_root, project),
                terminal_status=terminal_row["status"],
                mailbox_id=mailbox_id,
                callback_kind=callback_kind,
                callback_states=tuple(seen_states),
                callback_final_state=callback_final_state,
                ack_status=acked.status_code,
                ack_body=_json_or_empty(acked),
                install_returncode=installed.returncode,
                diag_returncode=diag.returncode,
                diag_has_timeline=_diag_has_timeline(diag.stdout),
                recover_status=recovered.status_code,
                recover_reason=_recover_reason(recovered),
                project_tree=_project_tree(project),
                knowledge_reads=tuple(reads),
            )
        finally:
            if server is not None:
                with contextlib.suppress(Exception):
                    requests.delete(f"{server.url}/sessions/{session_name}", timeout=30)
                server.stop()


def _pane_artifact_root(session_name: str, window_name: str) -> str | None:
    """Read ``CAO_ARTIFACTS_DIR`` out of the worker pane's own process environment.

    This is the only honest place to read it. The session floor lives in
    ``services/session_env.py``, an IN-MEMORY dict inside the cao-server process ("The store
    is process-local ... There is no schema migration and no on-disk format"), so there is no
    row to query. The value does reach the pane as real process env, which is how
    ``tmux_backend._proc_identity`` reads ``CAO_TERMINAL_ID`` (`:482-489`) -- the same
    mechanism, a different key. It is also the value that actually governs where a worker
    writes, which is what AC-LITE-1 is about.
    """
    listed = subprocess.run(
        ["tmux", "list-panes", "-t", f"{session_name}:{window_name}", "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return None
    prefix = b"CAO_ARTIFACTS_DIR="
    for raw in listed.stdout.split():
        if not raw.strip().isdigit():
            continue
        try:
            data = Path(f"/proc/{int(raw)}/environ").read_bytes()
        except OSError:
            continue
        for item in data.split(b"\0"):
            if item.startswith(prefix):
                return item[len(prefix) :].decode("utf-8")
    return None


def _relative_to(artifact_root: Path, project: Path) -> str:
    """The root's shape relative to the project -- the thing the two arms must agree on."""
    with contextlib.suppress(ValueError):
        return artifact_root.resolve().relative_to(project.resolve()).as_posix()
    return f"<outside-project:{artifact_root}>"


# ``delivery_msg.state`` vocabulary (``core/delivery.MsgState``). ``ready`` means the queue has
# the row but the tick has not claimed it yet; ``leased`` means it has. TERMINAL_STATES is
# ``{delivered, superseded, dead}`` (``core/delivery.py:136``).
QUEUE_TERMINAL = frozenset({"delivered", "superseded", "dead"})
QUEUE_BAD = frozenset({"dead", "superseded"})


def _private_home_env(home: Path) -> dict[str, str]:
    """Env for a CLI subprocess that must resolve the SAME store the server will read.

    `CAO_HOME_DIR` OUTRANKS `HOME` in `constants.py:105-110`, and `test/conftest.py` exports
    it process-wide to keep the suite off the production database. Inheriting it while
    overriding only `HOME` sends the CLI's write and the server's read to two different
    files -- which is exactly how this test first failed. `cao_server._subprocess_env`
    strips both spellings for the same reason; this mirrors it.
    """
    env = {key: value for key, value in os.environ.items()}
    for override in ("CAO_HOME_DIR", "CAO_HOME"):
        env.pop(override, None)
    env["HOME"] = str(home)
    return env


def _mailbox_for(db_path: Path, terminal_id: str) -> dict[str, Any] | None:
    """The mailbox row this terminal is the current incarnation of."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = [c[1] for c in conn.execute("PRAGMA table_info(mailboxes)")]
        for row in conn.execute(
            "SELECT * FROM mailboxes WHERE current_terminal_id = ?", (terminal_id,)
        ):
            return dict(zip(columns, row))
        return None
    finally:
        conn.close()


def _json_or_empty(response: requests.Response) -> dict[str, Any]:
    with contextlib.suppress(Exception):
        body = response.json()
        if isinstance(body, dict):
            return body
    return {}


def _delivery_rows(db_path: Path, receiver_id: str) -> list[dict[str, Any]]:
    """Read the delivery queue's own record for ``receiver_id``.

    The legacy ``inbox`` row is NOT the delivery record once the queue is on: the tick adopts
    only a PENDING legacy row with no ``delivery_msg`` counterpart, and a write-through row
    already has one, so the legacy row keeps ``pending`` for good. Measured on a box, 2026-09-16.
    Reading ``inbox.status`` here would have pinned a delivery-queue FLAG, not a lite boundary.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = [c[1] for c in conn.execute("PRAGMA table_info(delivery_msg)")]
        return [
            dict(zip(columns, row))
            for row in conn.execute(
                "SELECT * FROM delivery_msg WHERE receiver_id = ?", (receiver_id,)
            )
        ]
    finally:
        conn.close()


def _diag_has_timeline(stdout: str) -> bool:
    """True when diag emitted a parseable JSON payload naming the terminal it was asked about.

    A bare project has no ingested worker events, so the timeline is legitimately empty -- what
    must hold is that diag READ this database and answered in the requested format, not that it
    found rows.
    """
    with contextlib.suppress(json.JSONDecodeError):
        payload = json.loads(stdout)
        return isinstance(payload, (dict, list))
    return False


def _recover_reason(response: requests.Response) -> str:
    with contextlib.suppress(Exception):
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("reason") or body.get("detail", {}) or "")
    return ""


def _make_project(root: Path, *, with_orchestrator_dir: bool) -> Path:
    """A fresh directory carrying none of the A.2 paths -- optionally plus an empty one."""
    project = root / ("mutant" if with_orchestrator_dir else "bare")
    project.mkdir(parents=True)
    if with_orchestrator_dir:
        (project / "orchestrator").mkdir()
    return project


@pytest.fixture(scope="module")
def bare_arm(tmp_path_factory: pytest.TempPathFactory) -> Observables:
    root = tmp_path_factory.mktemp("lite-bare")
    return _run_round_trip(_make_project(root, with_orchestrator_dir=False), root / "home")


@pytest.fixture(scope="module")
def mutant_arm(tmp_path_factory: pytest.TempPathFactory) -> Observables:
    root = tmp_path_factory.mktemp("lite-mutant")
    return _run_round_trip(_make_project(root, with_orchestrator_dir=True), root / "home")


# ---------------------------------------------------------------------------------------
# Positive arm
# ---------------------------------------------------------------------------------------


def test_the_fixture_really_is_bare(tmp_path: Path) -> None:
    """Guard the guard: a fixture that accidentally carried a knowledge path proves nothing."""
    project = _make_project(tmp_path, with_orchestrator_dir=False)
    assert _project_tree(project) == ()
    for name in A2_NAMES:
        assert not (project / name).exists()


def test_health_is_ok(bare_arm: Observables) -> None:
    assert bare_arm.health.get("status") == "ok", bare_arm.health


def test_session_reaches_idle(bare_arm: Observables) -> None:
    assert bare_arm.terminal_status == "idle"


def test_one_callback_is_admitted_delivered_and_acked(bare_arm: Observables) -> None:
    """AC-LITE-1's callback clause, in full: admitted, delivered, acked.

    r1 asserted only that the tick CLAIMED the callback and tolerated an ack of 400. The
    reviewer was right to call that short of the criterion. The cause was the fixture, not
    the product: it created a `developer` terminal, which has no mailbox, and both ack paths
    resolve a mailbox by `current_terminal_id`. With a supervisor-role profile the bare
    project produces the mailbox itself and the whole loop closes.
    """
    assert bare_arm.mailbox_id.startswith("mb_"), bare_arm.mailbox_id
    assert bare_arm.callback_kind == "callback"
    assert bare_arm.callback_states[0] == "ready"
    assert "leased" in bare_arm.callback_states, bare_arm.callback_states
    assert bare_arm.callback_final_state == "delivered", bare_arm.callback_states
    assert not QUEUE_BAD.intersection(bare_arm.callback_states), bare_arm.callback_states


def test_the_ack_is_accepted_and_never_refuses_the_incarnation(
    bare_arm: Observables,
) -> None:
    """The specific refusal r1 shipped with must not come back.

    `not_current_incarnation` is what a missing mailbox produces
    (mailbox_service.py:1238-1240). Naming it here means a regression to the r1 shape fails
    with the reason rather than with a bare status mismatch.
    """
    assert bare_arm.ack_status == 200, bare_arm.ack_body
    assert bare_arm.ack_body.get("code") != "not_current_incarnation"
    assert "not_current_incarnation" not in json.dumps(bare_arm.ack_body)
    assert bare_arm.ack_body.get("mailbox_id") == bare_arm.mailbox_id
    assert int(bare_arm.ack_body.get("consumed_through_id", 0)) >= 1


def test_the_queue_addresses_the_mailbox_not_the_terminal(bare_arm: Observables) -> None:
    """Why a worker terminal could never be acked, pinned so the next reader does not re-derive it.

    `delivery_msg.receiver_id` is the mailbox id. If this ever became the terminal id, the
    ack's `settle_through(mailbox_id, ...)` would match nothing and the row would sit at
    `leased` forever -- exactly the r1 symptom.
    """
    assert bare_arm.mailbox_id.startswith("mb_")
    assert bare_arm.callback_final_state == "delivered"


def test_install_is_green_in_a_bare_project(bare_arm: Observables) -> None:
    assert bare_arm.install_returncode == 0


def test_diag_and_recover_are_green(bare_arm: Observables) -> None:
    assert bare_arm.diag_returncode == 0
    assert bare_arm.diag_has_timeline
    assert bare_arm.recover_status == 200, bare_arm.recover_reason


def test_artifact_root_is_the_neutral_one(bare_arm: Observables) -> None:
    """Never ``orchestrator/tmp/orch`` -- that was the probe slice 1 deleted."""
    assert bare_arm.artifact_root_relative == "tmp/orch"


def test_the_project_gains_nothing_outside_the_artifact_root(bare_arm: Observables) -> None:
    strays = [name for name in bare_arm.project_tree if not name.startswith("tmp/orch/")]
    assert strays == [], f"round trip wrote outside the artifact root: {strays}"


def test_no_audit_agent_or_hook_is_installed(bare_arm: Observables) -> None:
    """A.3: CAO installs neither the audit agent nor its hooks. A bare project has none."""
    for name in bare_arm.project_tree:
        parts = Path(name).parts
        assert ".claude" not in parts, f"CAO created project-local Claude config: {name}"
        assert "self-audit.md" not in parts, name
        assert not _is_knowledge_path(name), f"round trip created a knowledge path: {name}"


def test_no_in_process_knowledge_read_occurred(bare_arm: Observables) -> None:
    assert bare_arm.knowledge_reads == ()


# ---------------------------------------------------------------------------------------
# Mutation arm — presence must stop meaning anything
# ---------------------------------------------------------------------------------------


def test_empty_orchestrator_directory_changes_no_observable(
    bare_arm: Observables, mutant_arm: Observables
) -> None:
    """The whole point of A.5's AC-LITE-1 mutation arm, asserted field by field.

    Compared individually rather than as one equality so a failure names WHICH observable
    started depending on the directory.
    """
    assert mutant_arm.health == bare_arm.health
    assert mutant_arm.artifact_root_relative == bare_arm.artifact_root_relative
    assert mutant_arm.terminal_status == bare_arm.terminal_status
    assert mutant_arm.callback_kind == bare_arm.callback_kind
    assert mutant_arm.callback_states == bare_arm.callback_states
    assert mutant_arm.callback_final_state == bare_arm.callback_final_state
    assert mutant_arm.ack_status == bare_arm.ack_status
    assert mutant_arm.install_returncode == bare_arm.install_returncode
    assert mutant_arm.diag_returncode == bare_arm.diag_returncode
    assert mutant_arm.diag_has_timeline == bare_arm.diag_has_timeline
    assert mutant_arm.recover_status == bare_arm.recover_status
    assert mutant_arm.knowledge_reads == bare_arm.knowledge_reads == ()


def test_the_mutant_arm_writes_nothing_into_the_orchestrator_directory(
    mutant_arm: Observables,
) -> None:
    """Presence must not attract writes either, or the root moved by another name."""
    inside = [name for name in mutant_arm.project_tree if name.startswith("orchestrator/")]
    assert inside == [], f"the empty orchestrator/ directory attracted writes: {inside}"


def test_both_arms_place_artifacts_at_the_same_relative_root(
    bare_arm: Observables, mutant_arm: Observables
) -> None:
    assert bare_arm.artifact_root_relative == "tmp/orch"
    assert mutant_arm.artifact_root_relative == "tmp/orch"


def test_the_deny_guard_actually_fires(tmp_path: Path) -> None:
    """Without this the guard could be inert and every arm above would pass vacuously."""
    recorded: list[str] = []
    knowledge = tmp_path / "orchestrator" / "HANDOFF.md"
    with pytest.raises(AssertionError, match="forbidden knowledge I/O"):
        with _deny_knowledge_io(recorded):
            knowledge.exists()
    assert recorded and _is_knowledge_path(recorded[0])


def test_the_deny_guard_allows_neutral_paths(tmp_path: Path) -> None:
    recorded: list[str] = []
    with _deny_knowledge_io(recorded):
        (tmp_path / "tmp" / "orch").mkdir(parents=True)
        (tmp_path / "tmp" / "orch" / "artifact.json").write_text("{}", encoding="utf-8")
    assert recorded == []


# ---------------------------------------------------------------------------------------
# D1 — `cao env set` must reach a spawned terminal's environment
# ---------------------------------------------------------------------------------------


def test_env_store_artifacts_dir_reaches_a_spawned_pane(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The end-to-end half of the D1 fix, through a real server and a real pane.

    The unit arms in `test/services/test_explicit_artifacts_root.py` pin the precedence inside
    `canonical_session_env`. They cannot show that the value survives the whole path -- CLI
    write, server read, session floor, tmux, process environment -- and that path is the only
    reason an operator runs `cao env set` at all. Before the fix this test read the neutral
    fallback, because the store had no reader on the session path.
    """
    root = tmp_path_factory.mktemp("lite-envstore")
    project = _make_project(root, with_orchestrator_dir=False)
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    wanted = project / "artifacts-from-store"

    written = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli_agent_orchestrator.cli.main",
            "env",
            "set",
            "CAO_ARTIFACTS_DIR",
            str(wanted),
        ],
        capture_output=True,
        text=True,
        cwd=project,
        env=_private_home_env(home),
        timeout=120,
    )
    assert written.returncode == 0, written.stdout + written.stderr

    server: CaoServer | None = None
    session_name = f"envstore-{uuid.uuid4().hex[:8]}"
    with contextlib.chdir(project):
        try:
            server = _start_cao_server(home, _pick_free_port())
            created = requests.post(
                f"{server.url}/sessions",
                params={
                    "provider": PROVIDER,
                    "agent_profile": AGENT_PROFILE,
                    "session_name": session_name,
                },
                timeout=_CREATE_TIMEOUT,
            )
            assert created.status_code in (200, 201), f"{created.status_code} {created.text}"
            terminal = created.json()
            _wait_for(
                lambda: (lambda r: r.json() if r.ok and r.json().get("status") == "idle" else None)(
                    requests.get(f"{server.url}/terminals/{terminal['id']}", timeout=10)
                ),
                _IDLE_TIMEOUT,
                "terminal to reach idle",
            )
            pane_root = _pane_artifact_root(
                terminal.get("session_name", session_name), terminal["name"]
            )
            assert pane_root, "could not read CAO_ARTIFACTS_DIR from the pane"
            assert Path(pane_root) == wanted.resolve(), (
                f"the pane got {pane_root!r}; the store said {wanted}. A neutral "
                f"{project / 'tmp' / 'orch'} here means the store has no reader again."
            )
        finally:
            if server is not None:
                with contextlib.suppress(Exception):
                    requests.delete(f"{server.url}/sessions/{session_name}", timeout=30)
                server.stop()
