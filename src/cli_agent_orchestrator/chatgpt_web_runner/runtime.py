"""Browser lifecycle: cloakbrowser launch, pinned seed, profile lock + lease (D5).

One browser owns one profile. A cross-process launch lock plus a review lease
serialize workers; only the explicit owner stops a browser (D5, AC-12/AC-13).
Playwright + cloakbrowser are imported LAZILY inside :meth:`launch` so the pure
logic (lock acquisition, lease ownership, seed pinning, attach identity checks)
is testable offline and the base install carries no browser dependency.

Anti-detection posture, verbatim from D5:
- cloakbrowser's PATCHED Chromium, HEADFUL only (no stock Chromium, no headless).
- A pinned 5-digit ``--fingerprint`` seed per profile (chatgpt.com dropped the
  session on cloakbrowser's default random-per-launch seed until it was pinned).
- ``humanize=false`` for r1 submit/attachment operations (humanize=true threw
  ElementTargetChangedError on a menu click, crashing a run pre-send).
- Never the user's regular Chrome dir nor grok-web's daemon profile (port 9337).
"""

from __future__ import annotations

import errno
import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)

CHATGPT_URL = "https://chatgpt.com/"

#: The approved private profile root. ``C2C_DRIVER_PROFILE`` must resolve to an
#: intended per-account directory UNDER this root (D5).
_APPROVED_PROFILE_ROOT = Path("/data/cao-scratch/chatgpt-web")

#: grok-web's daemon profile / CDP port — never attach here (D5).
_FORBIDDEN_CDP_PORTS: frozenset[int] = frozenset({9337})
_FORBIDDEN_PROFILE_SUBSTRINGS: tuple[str, ...] = (
    ".local/share/grok-web",
    "/.config/google-chrome",
    "/.config/chromium",
)

_SEED_RE = re.compile(r"^\d{5}$")


def resolve_profile_dir() -> Path:
    """Resolve and validate the per-account profile dir (D5).

    Fails closed if ``C2C_DRIVER_PROFILE`` is unset, is not under the approved
    root, or resolves to a forbidden profile (user Chrome / grok-web daemon).
    """
    raw = os.environ.get("C2C_DRIVER_PROFILE")
    if not raw:
        raise RunnerError(
            RunnerErrorCode.ACCESS_DENIED,
            "C2C_DRIVER_PROFILE is not set — refusing to guess a profile",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    profile = Path(raw).resolve()
    low = str(profile).lower()
    for bad in _FORBIDDEN_PROFILE_SUBSTRINGS:
        if bad in low:
            raise RunnerError(
                RunnerErrorCode.ACCESS_DENIED,
                "refusing a forbidden profile (user Chrome / grok-web daemon)",
                delivery_state=DeliveryState.NOTHING_SENT,
            )
    root = _APPROVED_PROFILE_ROOT.resolve()
    try:
        profile.relative_to(root)
    except ValueError:
        raise RunnerError(
            RunnerErrorCode.ACCESS_DENIED,
            f"profile {profile} is not under the approved root {root}",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    return profile


def pin_fingerprint_seed(profile_dir: Path) -> str:
    """Return the pinned 5-digit seed for ``profile_dir``, minting one (0600) if
    absent (D5, common.mjs:31-44). One seed per profile, stable across launches.
    """
    seed_file = profile_dir / "fingerprint-seed"
    try:
        seed = seed_file.read_text(encoding="utf-8").strip()
    except OSError:
        seed = ""
    if not _SEED_RE.match(seed):
        seed = str(random.randint(10000, 99999))
        profile_dir.mkdir(parents=True, exist_ok=True)
        seed_file.write_text(seed + "\n", encoding="utf-8")
        try:
            seed_file.chmod(0o600)
        except OSError:
            pass
    return seed


@dataclass(frozen=True)
class LeaseIdentity:
    """The owner identity recorded in the review lease (D5).

    Only the explicit owner (matching terminal_id + pid) may stop the browser; a
    cancelled worker releases only its OWN lease.
    """

    terminal_id: str
    pid: int
    acquired_at: float


class ProfileLock:
    """A cross-process launch lock + review lease over one profile dir (D5).

    Implemented as an atomic ``O_CREAT|O_EXCL`` lockfile carrying the owner
    identity JSON. Acquisition never deletes a live-owner lock (AC-13); a
    contending worker queues against a deadline and then reports busy (AC-12).
    """

    def __init__(self, profile_dir: Path, terminal_id: str) -> None:
        self.profile_dir = profile_dir
        self.terminal_id = terminal_id
        self.lock_path = profile_dir / ".cao-review.lock"
        self._identity: Optional[LeaseIdentity] = None

    def _write_identity(self, fd: int) -> LeaseIdentity:
        identity = LeaseIdentity(
            terminal_id=self.terminal_id, pid=os.getpid(), acquired_at=time.time()
        )
        os.write(
            fd,
            json.dumps(
                {
                    "terminal_id": identity.terminal_id,
                    "pid": identity.pid,
                    "acquired_at": identity.acquired_at,
                }
            ).encode("utf-8"),
        )
        return identity

    def read_owner(self) -> Optional[LeaseIdentity]:
        try:
            raw = self.lock_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
            return LeaseIdentity(
                terminal_id=str(data["terminal_id"]),
                pid=int(data["pid"]),
                acquired_at=float(data.get("acquired_at", 0.0)),
            )
        except (ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _owner_alive(identity: LeaseIdentity) -> bool:
        try:
            os.kill(identity.pid, 0)
            return True
        except OSError as exc:
            return exc.errno == errno.EPERM  # alive but not ours to signal

    def acquire(self, *, queue_deadline_s: float = 300.0, poll_s: float = 2.0) -> LeaseIdentity:
        """Acquire the lease, queueing up to ``queue_deadline_s`` (AC-12).

        A contending worker that never wins reports ``bot_flagged``-free busy via
        a typed ``submit_unknown``/nothing-sent error (no review turn consumed).
        A stale lock whose owner is provably dead is reclaimed WITHOUT deleting a
        live-owner lock (AC-13).
        """
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + queue_deadline_s
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                owner = self.read_owner()
                if owner is not None and not self._owner_alive(owner):
                    # Provably dead owner: reclaim atomically (never blind-delete
                    # a live-owner lock — AC-13). Re-check under a fresh O_EXCL.
                    try:
                        os.unlink(str(self.lock_path))
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise RunnerError(
                        RunnerErrorCode.SUBMIT_UNKNOWN,
                        "profile busy: another worker holds the review lease",
                        delivery_state=DeliveryState.NOTHING_SENT,
                    )
                time.sleep(poll_s)
                continue
            try:
                self._identity = self._write_identity(fd)
            finally:
                os.close(fd)
            return self._identity

    def owns(self) -> bool:
        """True iff THIS worker currently holds the lease (owner-only stop, D5)."""
        if self._identity is None:
            return False
        owner = self.read_owner()
        return (
            owner is not None
            and owner.terminal_id == self._identity.terminal_id
            and owner.pid == self._identity.pid
        )

    def release(self) -> None:
        """Release ONLY our own lease (a cancelled worker never stops a browser
        it does not own — D5/AC-12)."""
        if self.owns():
            try:
                os.unlink(str(self.lock_path))
            except OSError:
                pass
        self._identity = None


def assert_cdp_port_allowed(port: int) -> None:
    """AC-13: refuse a CDP port owned by another lane (grok-web daemon, D5)."""
    if port in _FORBIDDEN_CDP_PORTS:
        raise RunnerError(
            RunnerErrorCode.ACCESS_DENIED,
            f"CDP port {port} is owned by another lane — refusing attach",
            delivery_state=DeliveryState.NOTHING_SENT,
        )


def assert_attach_identity(
    *,
    recorded_pid: int,
    observed_pid: int,
    recorded_start_time: float,
    observed_start_time: float,
    recorded_profile: str,
    observed_profile: str,
) -> None:
    """AC-13: attach only when process identity, start time and canonical profile
    path all agree; a mismatch on any refuses attach (D5)."""
    if recorded_pid != observed_pid:
        raise RunnerError(
            RunnerErrorCode.ACCESS_DENIED,
            "attach refused: process pid mismatch",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    if abs(recorded_start_time - observed_start_time) > 1.0:
        raise RunnerError(
            RunnerErrorCode.ACCESS_DENIED,
            "attach refused: process start-time mismatch",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
    if Path(recorded_profile).resolve() != Path(observed_profile).resolve():
        raise RunnerError(
            RunnerErrorCode.ACCESS_DENIED,
            "attach refused: canonical profile path mismatch",
            delivery_state=DeliveryState.NOTHING_SENT,
        )


def build_launch_options(profile_dir: Path, seed: str) -> dict[str, Any]:
    """Assemble the cloakbrowser ``launch_persistent_context`` options (D5).

    HEADFUL, ``humanize=False`` (r1 submit/attach determinism), pinned
    ``--fingerprint=<seed>``. The exact cloakbrowser option surface is applied in
    :func:`launch`; this returns the validated intent so a test can assert the
    posture without a browser.
    """
    return {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "humanize": False,
        "fingerprint_seed": seed,
        "url": CHATGPT_URL,
    }


async def launch(options: dict[str, Any]) -> Any:
    """Launch the headful cloakbrowser persistent context (D5).

    cloakbrowser + Playwright are imported HERE so the module imports cleanly
    without them. Raises ``proc_exited`` if the browser cannot start.
    """
    try:
        from cloakbrowser import (  # type: ignore
            launch_persistent_context_async,
        )
    except Exception as exc:  # pragma: no cover - exercised only in the live lane
        raise RunnerError(
            RunnerErrorCode.PROC_EXITED,
            f"cloakbrowser is not available: {type(exc).__name__}",
            delivery_state=DeliveryState.NOTHING_SENT,
        ) from exc
    seed = options["fingerprint_seed"]
    try:
        context = await launch_persistent_context_async(
            user_data_dir=options["user_data_dir"],
            headless=False,
            humanize=False,
            stealth_args=False,
            args=[f"--fingerprint={seed}"],
        )
    except Exception as exc:  # pragma: no cover - live lane
        raise RunnerError(
            RunnerErrorCode.PROC_EXITED,
            f"cloakbrowser launch failed: {type(exc).__name__}",
            delivery_state=DeliveryState.NOTHING_SENT,
        ) from exc
    return context
