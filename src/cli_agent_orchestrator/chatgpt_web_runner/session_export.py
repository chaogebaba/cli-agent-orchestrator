"""F970 (#819) step 3 — export the logged-in session so reads can leave the page.

F862 read the conversation through an in-page ``fetch`` because that was the
only proven path at the time. r4 then measured the fact this module rests on:
**the authoritative conversation GET succeeds from a plain Python process on
the exported session alone** — cookies plus the bearer, no sentinel, no
proof-of-work, no Turnstile (probes §7; findings §3 verified it two ways). Only
the SEND needs the page.

So the export exists to move the *read* off the browser: the poll no longer
depends on a live page, a crashed or closed browser no longer loses a delivered
turn, and the page is free to be closed between turns.

Boundaries this module keeps:

* **It is not a credential store.** The bundle is written mode-0600 inside the
  same private profile directory the browser already keeps its cookie database
  in — nothing moves into env, a keyring or the repo, and the file lives under
  the approved profile root or the write is refused.
* **Values never leave by accident.** ``summary()`` is what logs and reports
  print: counts, booleans and ages, never a cookie, bearer or device id.
* **It is read-scope only.** The session class was certified by r4 as
  "eligible for reuse, READ routes only" (cap table); the send classes (prepare,
  challenge, turnstile) are one-use and are deliberately not exported.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)

logger = logging.getLogger(__name__)

#: File name inside the profile directory.
SESSION_EXPORT_FILENAME = "session-export.json"

#: The in-page read that produces the bundle's non-cookie half. It reads the
#: same ``/api/auth/session`` the in-page transport already uses (D3's permitted
#: read) plus the device id the app keeps in local storage, which the backend
#: expects on ``oai-device-id``.
EXPORT_JS = """
async () => {
  const out = {};
  try {
    const r = await fetch('/api/auth/session', {credentials:'include'});
    out.session_status = r.status;
    const j = await r.json();
    out.bearer = (j && j.accessToken) || '';
    out.user_id = (j && j.user && j.user.id) || '';
    out.expires = (j && j.expires) || '';
  } catch (e) { out.session_error = String(e && e.name); }
  let device_id = '';
  try { device_id = localStorage.getItem('oai-did') || ''; } catch (e) {}
  out.device_id = device_id;
  out.user_agent = navigator.userAgent;
  out.language = navigator.language || 'en-US';
  return out;
}
"""


@dataclass(frozen=True)
class SessionBundle:
    """A read-scope session: cookies + bearer + the client identity headers."""

    cookies: Tuple[Dict[str, Any], ...] = ()
    bearer: str = ""
    user_agent: str = ""
    device_id: str = ""
    language: str = "en-US"
    user_id: str = ""
    exported_at: float = 0.0
    profile: str = ""
    #: Free-form non-secret notes (e.g. the session endpoint's HTTP status).
    facts: Dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """True when the bundle can authorize a conversation GET.

        Both halves are required: r4's cookie-only control returned 404 on the
        conversation route, so a cookie jar without a bearer is not a session.
        """
        return bool(self.cookies) and bool(self.bearer)

    def age_s(self, now: Optional[float] = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - (self.exported_at or 0.0))

    def summary(self) -> Dict[str, Any]:
        """Counts and booleans only — never a value. This is what gets logged."""
        return {
            "cookie_count": len(self.cookies),
            "httponly_cookie_count": sum(1 for c in self.cookies if c.get("httpOnly")),
            "bearer_present": bool(self.bearer),
            "device_id_present": bool(self.device_id),
            "user_agent_present": bool(self.user_agent),
            "age_s": round(self.age_s(), 1),
            "usable": self.usable,
            **{k: v for k, v in self.facts.items() if not isinstance(v, str) or len(v) < 40},
        }

    def to_json(self) -> Dict[str, Any]:
        return {
            "cookies": [dict(c) for c in self.cookies],
            "bearer": self.bearer,
            "user_agent": self.user_agent,
            "device_id": self.device_id,
            "language": self.language,
            "user_id": self.user_id,
            "exported_at": self.exported_at,
            "profile": self.profile,
            "facts": dict(self.facts),
        }

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "SessionBundle":
        cookies = raw.get("cookies")
        return cls(
            cookies=(
                tuple(c for c in cookies if isinstance(c, dict))
                if isinstance(cookies, list)
                else ()
            ),
            bearer=str(raw.get("bearer") or ""),
            user_agent=str(raw.get("user_agent") or ""),
            device_id=str(raw.get("device_id") or ""),
            language=str(raw.get("language") or "en-US"),
            user_id=str(raw.get("user_id") or ""),
            exported_at=float(raw.get("exported_at") or 0.0),
            profile=str(raw.get("profile") or ""),
            facts=raw.get("facts") if isinstance(raw.get("facts"), dict) else {},
        )


async def export_session(context: Any, page: Any, *, profile: str = "") -> SessionBundle:
    """Export the live session from an open browser context.

    ``context``/``page`` are duck-typed (the Playwright context and one of its
    pages) so the offline tests drive this with doubles.
    """
    exported = await page.evaluate(EXPORT_JS)
    exported = exported if isinstance(exported, dict) else {}
    cookies = await context.cookies()
    bundle = SessionBundle(
        cookies=tuple(c for c in (cookies or []) if isinstance(c, dict)),
        bearer=str(exported.get("bearer") or ""),
        user_agent=str(exported.get("user_agent") or ""),
        device_id=str(exported.get("device_id") or ""),
        language=str(exported.get("language") or "en-US"),
        user_id=str(exported.get("user_id") or ""),
        exported_at=time.time(),
        profile=profile,
        facts={"session_status": exported.get("session_status")},
    )
    logger.info("chatgpt_web session exported: %s", bundle.summary())
    return bundle


def _assert_under_profile_root(path: Path) -> Path:
    """Refuse to write the bundle anywhere but the approved private profile root."""
    from cli_agent_orchestrator.chatgpt_web_runner.runtime import _APPROVED_PROFILE_ROOT

    resolved = Path(path).resolve()
    try:
        resolved.relative_to(_APPROVED_PROFILE_ROOT.resolve())
    except ValueError:
        raise RunnerError(
            RunnerErrorCode.ACCESS_DENIED,
            "refusing to write a session export outside the approved profile root",
            delivery_state=DeliveryState.NOTHING_SENT,
        ) from None
    return resolved


def save_session(bundle: SessionBundle, path: Path) -> Path:
    """Write the bundle 0600 under the profile root (never elsewhere)."""
    target = _assert_under_profile_root(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the start: a world-readable window between write and
    # chmod is exactly the window that matters for a cookie jar.
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(bundle.to_json(), fh)
    os.chmod(target, 0o600)
    return target


def load_session(path: Path) -> Optional[SessionBundle]:
    """Read a previously exported bundle, or None when absent/unreadable."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return SessionBundle.from_json(raw)


def default_export_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / SESSION_EXPORT_FILENAME


def cookie_header_names(bundle: SessionBundle) -> List[str]:
    """Cookie NAMES only — for a probe report or a log line, never the values."""
    return sorted({str(c.get("name") or "") for c in bundle.cookies if c.get("name")})
