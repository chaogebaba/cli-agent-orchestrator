"""AC-3a over a live multi-lane session (WP-ARCH phase 3a, F728 #584).

Three criteria, all of them differences between arms rather than green runs:

1. **the shadow arm** — over one live multi-lane session each id's terminal state
   in the shadow queue matches the legacy inbox outcome, with disagreements
   classified ``queue-early``, ``legacy-early`` or ``genuine`` in the shape
   phase 1's agreement report used;
2. **the off arm** — with ``CAO_DELIVERY_QUEUE`` unset the count of
   ``delivery_msg`` rows is NIL.  Rows there are a failure rather than a
   curiosity;
3. **the bounce** — a restart while the shadow deployment holds in-flight
   non-terminal rows resolves back to ``shadow``, writes no
   ``DIAG-QUEUE-ORPHAN-GUARD``, and attributes no injection to the queue.  A run
   that resolves to ``drain``, or injects even one row, fails the case, because
   that is #506 arriving through the guard added to prevent it.

Written as a test rather than as a script beside the build report, so the gate
can re-run the acceptance record instead of reading someone's transcript of it.
Each arm spawns its own ``cao-server`` with its own ``$HOME``, so the arms cannot
contaminate each other through a shared database — which matters most for the off
arm, where the criterion is a count of zero.

Live, and therefore ``--run-live`` gated: these terminals are real provider CLIs
driven through a real server, which is the only way an outcome mapping can be
tested against what legacy actually does rather than against what we think it
does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from test.fixtures.cao_server import CaoServer, _pick_free_port, _start_cao_server

import pytest
import requests

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

#: How many messages the shadow arm sends.  The agreement report's own content
#: floor is 20 shadow rows over 2 receivers with 5 terminal rows on each side;
#: this sends comfortably past it so a couple of slow deliveries cannot make the
#: run INVALID rather than failing or passing on its merits.
MESSAGE_COUNT = 24

#: Providers to open lanes with, in preference order.  Multi-lane is not
#: decoration: D9's occupancy predicate, the mailbox addressing rule and the
#: report's own floor all read across receivers, and a single-lane run would
#: exercise none of them.
#:
#: "Lane" here means RECEIVER, not provider.  Two terminals of one provider give
#: the two receivers the criterion needs, and they are markedly cheaper and more
#: reliable to bring up than one of each — a second provider doubles the chance
#: that a slow first-run authentication turns an acceptance run into a timeout
#: that says nothing about the code under test.  So the strategy is: find the
#: first provider that boots, open both terminals with it, and only mix if a
#: second terminal of that provider will not come up.
LANE_PROVIDERS = (
    ("claude_code", "developer", "claude"),
    ("kiro_cli", "developer", "kiro-cli"),
    ("codex", "developer", "codex"),
)

#: Provider credentials the shared e2e fixture does not link into the redirected
#: HOME.
#:
#: ``_seed_provider_home_prerequisites`` links the dot-directories providers need
#: to BOOT — ``.bun`` for the binaries, ``.local`` for the kiro runtime — but not
#: the ones they need to AUTHENTICATE.  With HOME redirected and these absent,
#: every provider starts its own login: codex answers 401 and kiro cannot find
#: its agent, so a run that is really about delivery fails at bring-up and says
#: nothing about the code.  Seeded here rather than added to the shared list,
#: because widening that list changes every e2e module's environment and this
#: phase has no business doing that.
AUTH_SEEDS = (".claude", ".claude.json", ".codex", ".config")

#: Terminal creation launches a real CLI, which on a cold box can include a
#: first-run cache warm.  The old 180 s was tuned for a warm laptop and turned an
#: acceptance run into a ReadTimeout that said nothing about the code.
TERMINAL_CREATE_TIMEOUT_S = 600


class Arm:
    """One server, its database, and the CLI that reads it."""

    def __init__(self, home: Path, server: CaoServer) -> None:
        self.home = home
        self.server = server
        self.url = server.url
        self.db = server.db_path

    def diag(self, *args: str) -> tuple[int, str]:
        """Run ``cao diag`` against this arm's database, read-only."""
        completed = subprocess.run(
            [sys.executable, "-m", "cli_agent_orchestrator.cli.main", "diag", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode, completed.stdout

    def delivery_report(self) -> dict:
        code, out = self.diag("delivery", "--db", str(self.db), "--json")
        return json.loads(out) if out.strip() else {"valid": False, "raw_exit": code}

    def queue_rows(self) -> list[dict]:
        import sqlite3

        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM delivery_msg")]
        finally:
            conn.close()

    def attempt_count(self) -> int:
        import sqlite3

        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM delivery_attempt").fetchone()[0])
        finally:
            conn.close()

    def findings(self, code: str) -> int:
        import sqlite3

        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM finding WHERE code = ? AND state = 'open'", (code,)
                ).fetchone()[0]
            )
        finally:
            conn.close()


def _seed_auth(home: Path) -> None:
    """Link the auth files into an arm's HOME before its server starts.

    Idempotent and best-effort: ``_seed_provider_home_prerequisites`` skips any
    target that already exists, so seeding first and letting it fill the rest is
    the composition that works.  A missing source is silently skipped — the run
    then fails at bring-up and SKIPS, which is the honest outcome for a host with
    no authenticated provider.

    Symlinks rather than copies, matching the shared fixture's choice: a provider
    that refreshes a token writes through to the real file, which on the
    disposable boxes these runs use is what you want.
    """
    real_home = Path(os.environ.get("_CAO_REAL_HOME", "").strip() or Path.home())
    for name in AUTH_SEEDS:
        source = real_home / name
        target = home / name
        if source.exists() and not target.exists():
            try:
                target.symlink_to(source)
            except OSError as exc:
                print(f"[p3a] could not seed {name}: {exc!r}")


def _arm(tmp_path_factory: pytest.TempPathFactory, name: str, switch: str | None) -> Arm:
    home = tmp_path_factory.mktemp(f"p3a_{name}")
    _seed_auth(home)
    port = _pick_free_port()
    extra = {} if switch is None else {"CAO_DELIVERY_QUEUE": switch}
    server = _start_cao_server(home, port, extra_env=extra, deadline=60.0)
    return Arm(home, server)


def _create(arm: Arm, session: str | None, provider: str, profile: str) -> tuple[str, str] | None:
    """One terminal as ``(terminal_id, actual_session_name)``, or ``None``.

    **The returned session name is the one to use for every later terminal**, and
    it is not the one that was asked for: the e2e fixture exports
    ``CAO_SESSION_PREFIX=cao-test-`` so test sessions are distinguishable from
    production ones during incident triage, and the server applies it. Posting
    later terminals to the REQUESTED name 404s, which is what made the first
    live run open one lane and skip — an hour of box time spent proving that a
    name was wrong.

    Failures are printed rather than raised because bring-up is a precondition,
    not the thing under test: a run that cannot open two lanes must say why and
    skip, so nobody reads a provider timeout as a delivery defect.
    """
    endpoint = (
        f"{arm.url}/sessions" if session is None else f"{arm.url}/sessions/{session}/terminals"
    )
    params: dict[str, str] = {"provider": provider, "agent_profile": profile}
    if session is None:
        params["session_name"] = f"p3a-{uuid.uuid4().hex[:8]}"
    try:
        response = requests.post(endpoint, params=params, timeout=TERMINAL_CREATE_TIMEOUT_S)
    except requests.RequestException as exc:
        print(f"[p3a] {provider} terminal did not come up: {exc!r}")
        return None
    if response.status_code not in (200, 201):
        print(f"[p3a] {provider} terminal refused: {response.status_code} {response.text[:200]}")
        return None
    payload = response.json()
    return str(payload["id"]), str(payload.get("session_name") or session or "")


def _open_lanes(arm: Arm, count: int = 2) -> list[str]:
    """Open ``count`` receivers, preferring two terminals of ONE provider.

    Skips rather than fails when fewer than two come up: a box without a usable
    provider cannot produce the evidence AC-3a asks for, and reporting that as a
    FAILURE would put a red run against a build whose behaviour was never
    exercised.  The server log tail is printed on the way out so the reason is in
    the run's own output rather than in a file nobody fetches.
    """
    import shutil

    available = [
        (provider, profile)
        for provider, profile, binary in LANE_PROVIDERS
        if shutil.which(binary) is not None
    ]
    terminals: list[str] = []
    session: str | None = None
    for provider, profile in available:
        opened = _create(arm, session, provider, profile)
        if opened is None:
            continue
        terminal_id, session = opened
        terminals.append(terminal_id)
        while len(terminals) < count:
            more = _create(arm, session, provider, profile)
            if more is None:
                break
            terminals.append(more[0])
        if len(terminals) >= count:
            break

    if len(terminals) < count:
        log = arm.home / "server.log"
        if log.exists():
            print("[p3a] server log tail:")
            print("\n".join(log.read_text(errors="replace").splitlines()[-40:]))
    return terminals


def _send(arm: Arm, sender: str, receiver: str, body: str, **options: object) -> requests.Response:
    return requests.post(
        f"{arm.url}/terminals/{receiver}/inbox/messages",
        params={"sender_id": sender, "message": body, **options},
        timeout=60,
    )


def _drive_traffic(arm: Arm, terminals: list[str]) -> int:
    """Send the arm's messages, including the per-message options D8 carries.

    The traffic is deliberately not uniform.  A run of identical plain sends
    would exercise one cell of the mapping; ``expire_after_s`` and
    ``supersede_key`` are the two F578 siblings D8 carries, and a shadow row that
    dropped either would look perfectly healthy in a report built from plain
    sends alone.
    """
    sent = 0
    for index in range(MESSAGE_COUNT):
        sender = terminals[index % len(terminals)]
        receiver = terminals[(index + 1) % len(terminals)]
        options: dict[str, object] = {}
        if index % 6 == 3:
            options["expire_after_s"] = 30
        if index % 6 == 5:
            options["supersede_key"] = f"p3a-supersede-{index // 6}"
        response = _send(arm, sender, receiver, f"[p3a] observation ping {index}", **options)
        if response.status_code == 200:
            sent += 1
        time.sleep(0.4)
    return sent


def _plant_unresolved_shadow_row(arm: Arm) -> None:
    """Write one ``ready`` shadow row straight into the arm's queue.

    Through the real store rather than raw SQL, so the row is shaped exactly like
    one the mirror would have written — the same ``dead_by`` stamp, the same
    ``mode``, the same defaults. A hand-rolled INSERT could get a column wrong in
    a way that made the guard's answer right for the wrong reason.

    Opened while the server is running, which WAL permits; the write is a single
    short transaction against a database the server is not contending for at this
    moment.
    """
    from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
    from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
    from cli_agent_orchestrator.core.delivery import EnqueueDraft, QueueMode

    pool = ConnectionPool(arm.db, busy_timeout_ms=10_000)
    try:
        SqliteQueueStore(pool).enqueue(
            EnqueueDraft(
                idempotency_key=f"legacy-inbox:unresolved-{uuid.uuid4().hex[:8]}",
                receiver_id="mb_bounce_probe",
                sender_id="p3a-bounce",
                payload="an outcome the mirror never observed",
                mode=QueueMode.SHADOW,
            )
        )
    finally:
        pool.close_all()


@pytest.fixture(scope="module")
def shadow_arm(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Arm]:
    arm = _arm(tmp_path_factory, "shadow", "shadow")
    try:
        yield arm
    finally:
        arm.server.stop()


@pytest.fixture(scope="module")
def off_arm(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Arm]:
    arm = _arm(tmp_path_factory, "off", None)
    try:
        yield arm
    finally:
        arm.server.stop()


def test_ac3a_the_off_arm_writes_no_queue_rows(off_arm: Arm) -> None:
    """Criterion 2, and the arm that makes the other two mean something.

    With the switch unset there is no code path from a hook to the queue, so this
    is not "few rows" — it is nil.  Anything else means the switch is not the
    only thing standing between a message and the queue, and every AC-3a number
    from the shadow arm would then be measuring something other than the switch.
    """
    terminals = _open_lanes(off_arm)
    if len(terminals) < 2:
        pytest.skip("fewer than two provider lanes are usable on this host")

    _drive_traffic(off_arm, terminals)
    time.sleep(10)

    assert off_arm.queue_rows() == [], "the off arm wrote delivery_msg rows"
    assert off_arm.attempt_count() == 0
    assert off_arm.findings("DIAG-QUEUE-ORPHAN-GUARD") == 0

    # And the report refuses to call an empty run a pass.
    report = off_arm.delivery_report()
    assert report["valid"] is False


def test_ac3a_the_shadow_arm_agrees_with_the_legacy_inbox(shadow_arm: Arm) -> None:
    """Criterion 1: the terminal state per id matches, disagreements classified.

    ``genuine`` is the count that fails this: it is the only class that says the
    mapping or the mirror is wrong.  ``legacy_early`` is tolerated and reported,
    because §7a accepts that an outcome the mirror never observed leaves the row
    ``ready`` — the flip sweeps those to ``superseded`` — and hiding them would
    make the mirror look perfect by declining to notice.
    """
    terminals = _open_lanes(shadow_arm)
    if len(terminals) < 2:
        pytest.skip("fewer than two provider lanes are usable on this host")

    sent = _drive_traffic(shadow_arm, terminals)
    assert sent >= 20, f"only {sent} sends were accepted; the report floor needs 20"

    # Let the legacy path finish delivering before the comparison is taken. The
    # mirror settles from the status it observes, so a comparison taken while
    # legacy is still delivering measures the wait, not the agreement.
    time.sleep(90)

    report = shadow_arm.delivery_report()
    assert report["valid"], report["invalid_reasons"]

    counts = report["classifications"]
    totals = report["totals"]
    assert totals["shadow_rows"] >= 20
    assert totals["receivers"] >= 2
    assert counts["genuine"] == 0, (
        "the shadow queue and the legacy inbox reached DIFFERENT terminal states: "
        f"{json.dumps(report['receivers'], indent=2)}"
    )
    # Every row is mode='shadow' — nothing in this sub-phase may write a live one.
    assert {row["mode"] for row in shadow_arm.queue_rows()} == {"shadow"}


def test_ac3a_the_bounce_resolves_back_to_shadow(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Criterion 3, and the defect an earlier draft of the guard would have shipped.

    A bounced sub-phase 3a deployment holds in-flight SHADOW rows.  Had the
    occupancy predicate counted them, this restart would resolve to ``drain``,
    whose tick would inject copies of messages the legacy path already delivered
    — a second carrier over one id, which is #506 reproduced by the guard added
    to prevent loss, in the first sub-phase to ship.
    """
    home = tmp_path_factory.mktemp("p3a_bounce")
    _seed_auth(home)
    port = _pick_free_port()
    server = _start_cao_server(
        home, port, extra_env={"CAO_DELIVERY_QUEUE": "shadow"}, deadline=60.0
    )
    arm = Arm(home, server)
    try:
        terminals = _open_lanes(arm)
        if len(terminals) < 2:
            pytest.skip("fewer than two provider lanes are usable on this host")
        for index in range(6):
            _send(
                arm,
                terminals[index % 2],
                terminals[(index + 1) % 2],
                f"[p3a] bounce ping {index}",
            )
        time.sleep(3)
        in_flight = [row for row in arm.queue_rows() if row["state"] == "ready"]
        if not in_flight:
            # The mirror settled every row before the restart, which is the
            # HEALTHY outcome and leaves nothing to bounce over. Rather than
            # racing the mirror with a shorter sleep — which would make the case
            # pass or fail on delivery latency — the unresolved row is created
            # directly. §7a describes exactly this row: "when the mirror writer
            # misses an outcome, because the server restarted mid-flight or the
            # legacy edge was lost, the shadow row stays ``ready``". That is the
            # row the guard must not count, and writing one is a faithful stand-in
            # for the condition, not a weakening of it.
            _plant_unresolved_shadow_row(arm)
            in_flight = [row for row in arm.queue_rows() if row["state"] == "ready"]
        assert in_flight, "the bounce case needs in-flight rows to be a case at all"
        attempts_before = arm.attempt_count()
    finally:
        server.stop()

    rebooted = _start_cao_server(
        home, _pick_free_port(), extra_env={"CAO_DELIVERY_QUEUE": "shadow"}, deadline=60.0
    )
    arm = Arm(home, rebooted)
    try:
        assert arm.findings("DIAG-QUEUE-ORPHAN-GUARD") == 0, (
            "the boot guard demoted a shadow deployment — it counted shadow rows "
            "as occupancy, which is #506 arriving through the guard"
        )
        # No injection is attributable to the queue: in shadow mode the only
        # writer of attempt rows is the mirror observing legacy, and nothing new
        # was sent after the restart.
        time.sleep(5)
        assert arm.attempt_count() == attempts_before
        assert {row["mode"] for row in arm.queue_rows()} == {"shadow"}
    finally:
        rebooted.stop()
