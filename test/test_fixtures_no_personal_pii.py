# ABOUTME: Guard test that fails if personal PII leaks into test fixtures.
# ABOUTME: Rejects personal emails, real home paths, and secret-token strings.
"""Fail-closed guard against personal PII in test fixtures.

Provider status-detection fixtures are captured from *live* CLI TUIs. Three
classes of personal data have leaked through such captures and must never be
committed:

1. **Personal email** — a login banner prints the authenticated account's
   address (PR #436 shipped a maintainer's Gmail; an older claude_code capture
   carried another contributor's address).
2. **Real home paths** — ``/home/<name>`` / ``/Users/<name>`` embed a real OS
   username (F582 slice-A empirical gate B1: the kiro busy-marker captures
   shipped ``/home/chao/...`` tool-call display lines).
3. **Secret tokens** — a captured pane or scrollback can carry an API key,
   OAuth/PAT token, JWT, or ``Bearer`` credential.

This test makes any recurrence a hard, pre-merge CI failure rather than
something a human has to catch by eye in a diff.

Scope: any file under a ``fixtures/`` directory within ``test/``.

Redaction convention when capturing a new fixture (all width-preserving so pane
geometry is unchanged):

* Personal email → ``user@example.com``.
* Home path → ``/home/user`` / ``/Users/user`` (or another allowlisted synthetic
  name below). ``chao`` → ``user`` is 4→4 chars.
* Token / secret → drop or neutralise the sentence carrying it.

Non-personal, obviously-synthetic values are intentionally NOT flagged so
legitimate sample data stays usable: ``example.com`` / ``noreply`` /
``git@github.com`` emails, and the synthetic home usernames in
``_SYNTHETIC_HOME_NAMES`` (``user``, ``test``, ``example``, CI runner names …).
"""

import re
from pathlib import Path

# --- 1. Personal email ------------------------------------------------------
# Personal mail providers whose presence in a fixture is almost certainly a real
# person's address captured from a live login banner. Deliberately narrow to
# avoid flagging synthetic sample data (``@example.com``) or tooling banners
# (``git@github.com``), which are legitimate in fixtures.
_PERSONAL_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@(?:gmail|googlemail|yahoo|ymail|hotmail|outlook|live|msn|"
    r"icloud|me|mac|aol|proton|protonmail|pm|gmx|zoho|yandex|mail)\.[A-Za-z]{2,}",
    re.IGNORECASE,
)

# --- 2. Real home paths -----------------------------------------------------
# ``/home/<name>`` or ``/Users/<name>``. The username is the PII. Synthetic /
# CI-standard names are allowlisted so redacted fixtures and CI-captured
# fixtures stay usable. Matching is case-insensitive on the ``home``/``Users``
# root; the captured name group is compared against the allowlist verbatim.
_HOME_PATH_RE = re.compile(r"/(?:home|Users)/([A-Za-z0-9._-]+)")
_SYNTHETIC_HOME_NAMES = frozenset(
    {
        "user",
        "users",
        "test",
        "tester",
        "example",
        "sample",
        "runner",  # GitHub Actions
        "ubuntu",  # common CI/cloud image
        "ec2-user",
        "vscode",  # devcontainer
        "root",
        "node",
    }
)

# --- 3. Secret tokens -------------------------------------------------------
# Well-known credential shapes. Anchored to concrete prefixes / structures so
# ordinary fixture identifiers (``release_token``, 8-hex session ids, 40-hex git
# SHAs) do not match. Each alternative is a shape a real secret takes.
_TOKEN_RE = re.compile(
    r"""(?x)
    \b(?:
        sk-[A-Za-z0-9]{20,}                       # OpenAI-style secret key
      | rk_(?:live|test)_[A-Za-z0-9]{16,}          # Stripe-style restricted key
      | gh[pousr]_[A-Za-z0-9]{20,}                 # GitHub PAT / OAuth / refresh
      | github_pat_[A-Za-z0-9_]{20,}               # GitHub fine-grained PAT
      | xox[baprs]-[A-Za-z0-9-]{10,}               # Slack token
      | AKIA[0-9A-Z]{16}                           # AWS access key id
      | AIza[0-9A-Za-z_-]{30,}                     # Google API key
      | ya29\.[0-9A-Za-z_-]{20,}                   # Google OAuth access token
      | eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}  # JWT
    )\b
    | Bearer\s+[A-Za-z0-9._~+/=-]{24,}             # Authorization: Bearer <cred>
    """,
    re.IGNORECASE,
)

_TEST_ROOT = Path(__file__).resolve().parent


# Generated / compiled artifacts that live under a ``fixtures/`` package dir but
# are not committed fixture data. Python bytecode caches embed the absolute build
# path (``/home/<builder>/...``); they are build output, never reviewed data.
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo", ".so"})
_IGNORED_DIR_PARTS = frozenset({"__pycache__"})


def _fixture_files() -> list[Path]:
    """Every committed fixture file under a ``fixtures/`` directory in ``test/``.

    Excludes compiled/generated artifacts (``__pycache__``/``*.pyc``): those are
    build output that embeds the builder's absolute path and are not reviewable
    fixture data.
    """

    files: list[Path] = []
    for p in _TEST_ROOT.rglob("fixtures/**/*"):
        if not p.is_file():
            continue
        if p.suffix in _IGNORED_SUFFIXES:
            continue
        if _IGNORED_DIR_PARTS.intersection(p.parts):
            continue
        files.append(p)
    return files


def _read(path: Path) -> str | None:
    """Read a fixture leniently (raw captures may not be valid UTF-8)."""

    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def test_no_personal_email_addresses_in_fixtures() -> None:
    """No test fixture may contain a personal-provider email address."""

    offenders: dict[str, set[str]] = {}
    for path in _fixture_files():
        text = _read(path)
        if text is None:
            continue
        matches = {m.group(0) for m in _PERSONAL_EMAIL_RE.finditer(text)}
        if matches:
            offenders[str(path.relative_to(_TEST_ROOT))] = matches

    assert not offenders, (
        "Personal email addresses (PII) found in test fixtures — scrub the login "
        "banner to `user@example.com` or delete the orphaned fixture:\n"
        + "\n".join(f"  {f}: {', '.join(sorted(addrs))}" for f, addrs in sorted(offenders.items()))
    )


def test_no_real_home_paths_in_fixtures() -> None:
    """No fixture may embed a real ``/home/<name>`` or ``/Users/<name>`` username.

    F582 slice-A empirical gate B1: the kiro busy-marker captures shipped
    ``/home/chao/...`` display lines. Redact to a same-width synthetic name
    (``/home/user``); CI/synthetic names are allowlisted.
    """

    offenders: dict[str, set[str]] = {}
    for path in _fixture_files():
        text = _read(path)
        if text is None:
            continue
        matches = {
            m.group(0)
            for m in _HOME_PATH_RE.finditer(text)
            if m.group(1).lower() not in _SYNTHETIC_HOME_NAMES
        }
        if matches:
            offenders[str(path.relative_to(_TEST_ROOT))] = matches

    assert not offenders, (
        "Real home paths (PII — a real OS username) found in test fixtures. "
        "Redact the username to a same-width synthetic name (`/home/user`, "
        f"`/Users/user`) — allowlisted synthetic names: {sorted(_SYNTHETIC_HOME_NAMES)}:\n"
        + "\n".join(f"  {f}: {', '.join(sorted(paths))}" for f, paths in sorted(offenders.items()))
    )


def test_no_secret_tokens_in_fixtures() -> None:
    """No fixture may contain an API key, OAuth/PAT token, JWT, or Bearer cred."""

    offenders: dict[str, set[str]] = {}
    for path in _fixture_files():
        text = _read(path)
        if text is None:
            continue
        matches = {m.group(0)[:16] + "…" for m in _TOKEN_RE.finditer(text)}
        if matches:
            offenders[str(path.relative_to(_TEST_ROOT))] = matches

    assert not offenders, (
        "Secret-token-shaped strings found in test fixtures — drop or neutralise "
        "the sentence carrying the credential before committing "
        "(matches are truncated below to avoid re-committing the secret):\n"
        + "\n".join(f"  {f}: {', '.join(sorted(m))}" for f, m in sorted(offenders.items()))
    )
