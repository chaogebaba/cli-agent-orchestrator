"""F993 (#841): the ``cao_terminal`` host-capability gate must not swallow defects.

These live in their own module rather than in ``test_cao_server.py`` because
that file is ``pytestmark = pytest.mark.e2e`` and so is deselected by the
working ``-m "not e2e and not slow"`` selection. A guard against a false green
that itself only runs under ``--run-live`` would repeat the very mistake it
exists to prevent, so this module carries no marker and runs by default.

r2 (merge-review r1): the r1 fix kept a NARROW substring classifier over the 5xx
body, and the review drove a real server defect through it —

    status=500
    body=Internal database open failed: No such file or directory: '/data/cao.db'
    provider_missing_from_host=True            → ``pytest.skip``, reported green

— because that phrase does not identify a missing provider executable; a missing
database, working directory, profile, socket or config file all read the same
way. The classifier is DELETED. Host capability is decided pre-flight only (the
``--run-live`` gate and the ``shutil.which`` probe), and every non-2xx response
is a failure.

There is no structured server-side field to classify on instead: the create
route renders an unexpected exception as ``detail=f"Failed to create session:
{str(e)}"`` (``api/main.py``), so a missing binary arrives as the OS's bare
``FileNotFoundError`` text — byte-identical in shape to any other missing path.

The tests below therefore drive the REAL fixture function (``cao_terminal``'s
unwrapped generator, not a private helper): the r1 defect was precisely that the
classifier was unit-tested in isolation while the skip site did whatever it liked.
"""

from __future__ import annotations

import shutil
from test.fixtures import cao_server as cao_server_fixture
from types import SimpleNamespace
from typing import Any

import pytest
import requests

#: Exactly the body the review used to prove the r1 classifier wrong.
_DB_FILENOTFOUND_BODY = "Internal database open failed: No such file or directory: '/data/cao.db'"


class _FakeConfig:
    def __init__(self, run_live: bool = True) -> None:
        self._run_live = run_live

    def getoption(self, name: str, default: object = None) -> object:
        if name == "--run-live":
            return self._run_live
        return default


class _FakeRequest:
    """Only what ``cao_terminal`` reads: ``.param`` and ``.config.getoption``."""

    def __init__(self, run_live: bool = True) -> None:
        self.param = None
        self.config = _FakeConfig(run_live=run_live)


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def _cao_terminal_body() -> Any:
    """The unwrapped ``cao_terminal`` generator function (past the fixture wrapper).

    ``pytest.fixture`` wraps the function in a ``FixtureFunctionDefinition`` that
    mypy cannot index into, so the unwrap is done once here and typed as Any.
    """
    return getattr(cao_server_fixture.cao_terminal, "__wrapped__")


def _drive_fixture(
    monkeypatch: "pytest.MonkeyPatch", response: _FakeResponse, *, run_live: bool = True
) -> "tuple[str, BaseException | None]":
    """Call the REAL ``cao_terminal`` fixture through to its response handling.

    The fixture is a generator that raises or skips before ever yielding, so
    ``next()`` is the whole body up to the POST.

    Returns ``(outcome, exception)`` with outcome ``"raised" | "skipped" |
    "yielded"`` — it does NOT let the fixture's own ``pytest.skip`` propagate.
    That is deliberate and load-bearing: a skip raised inside a fixture skips the
    CALLING test, so the pre-r2 classifier silenced the very test written to
    catch it (measured: the mutant run reported ``1 skipped`` instead of a
    failure). Converting the skip into an outcome value keeps the guard loud.
    """
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    generator: Any = _cao_terminal_body()(
        cao_server=SimpleNamespace(url="http://127.0.0.1:1"),
        request=_FakeRequest(run_live=run_live),
    )
    try:
        next(generator)
    except pytest.skip.Exception as exc:
        return ("skipped", exc)
    except BaseException as exc:  # noqa: BLE001 - the outcome IS the assertion
        return ("raised", exc)
    finally:
        generator.close()
    return ("yielded", None)


def test_database_filenotfound_5xx_fails_instead_of_skipping(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """THE r1 mutant: a server defect that merely says "No such file or directory".

    ``shutil.which("kiro-cli")`` is stubbed present so the ONLY thing that could
    downgrade this response is the deleted post-response classifier. Before r2
    this body skipped; it must now raise.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/kiro-cli")

    outcome, exc = _drive_fixture(monkeypatch, _FakeResponse(500, _DB_FILENOTFOUND_BODY))

    assert outcome == "raised", (
        f"the fixture took the {outcome!r} path for a database FileNotFoundError "
        f"({exc!r}) — pre-r2 it skipped here and the run reported green"
    )
    assert isinstance(exc, RuntimeError)
    assert _DB_FILENOTFOUND_BODY in str(exc)
    assert "HTTP 500" in str(exc)


@pytest.mark.parametrize(
    "body",
    [
        _DB_FILENOTFOUND_BODY,
        # Every one of these is a defect, not a host fact, and every one of the
        # r1 markers plus the old provider-name catch-all used to match at least
        # one of them.
        "Failed to create session: kiro-cli is not installed",
        "Failed to create session: kiro-cli: command not found",
        "[Errno 2] No such file or directory: 'kiro-cli'",
        "Failed to create session: kiro_cli initialization timed out after 15 seconds",
        "Failed to create terminal: provider_launch_failed",
        "Internal Server Error",
        "kiro_cli",
        "Terminal metadata not found for terminal_id: ab8cfc5f",
        "session not found",
        "Internal database open failed: unable to open database file",
        "No such file or directory: '/home/chao/.aws/cli-agent-orchestrator/providers.toml'",
    ],
)
def test_no_5xx_body_ever_skips(monkeypatch: "pytest.MonkeyPatch", body: str) -> None:
    """No post-response signal may downgrade a 5xx: the whole class, not one body."""
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/kiro-cli")

    outcome, exc = _drive_fixture(monkeypatch, _FakeResponse(500, body))

    assert outcome == "raised", f"body {body!r} took the {outcome!r} path ({exc!r})"
    assert isinstance(exc, RuntimeError)


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422, 499])
def test_non_5xx_also_fails_whatever_the_body_says(
    monkeypatch: "pytest.MonkeyPatch", status: int
) -> None:
    """A 4xx is a contract breach by the caller, never a host-capability fact."""
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/kiro-cli")

    outcome, exc = _drive_fixture(monkeypatch, _FakeResponse(status, "kiro-cli is not installed"))

    assert outcome == "raised", f"HTTP {status} took the {outcome!r} path ({exc!r})"
    assert isinstance(exc, RuntimeError)


def test_the_preflight_run_live_gate_is_the_only_skip_left(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """Without ``--run-live`` the fixture skips BEFORE the request — the real gate.

    This is the gate the deleted classifier always was too late to replace: by
    the time a 5xx comes back, a real CLI may already have opened a browser.
    """
    called: list[int] = []

    def _no_post(*_a: Any, **_k: Any) -> None:
        called.append(1)

    monkeypatch.setattr(requests, "post", _no_post)

    outcome, exc = _drive_fixture(
        monkeypatch, _FakeResponse(500, _DB_FILENOTFOUND_BODY), run_live=False
    )

    assert outcome == "skipped", f"the run-live gate did not skip (outcome={outcome!r})"
    assert isinstance(exc, pytest.skip.Exception)
    assert called == [], "the request must not be sent once the run-live gate refuses"


def test_the_preflight_which_probe_is_the_other_skip(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """A binary genuinely absent from PATH skips pre-flight, before any request."""
    called: list[int] = []

    def _no_post(*_a: Any, **_k: Any) -> None:
        called.append(1)

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(requests, "post", _no_post)

    outcome, exc = _drive_fixture(monkeypatch, _FakeResponse(500, _DB_FILENOTFOUND_BODY))

    assert outcome == "skipped", f"the which() probe did not skip (outcome={outcome!r})"
    assert isinstance(exc, pytest.skip.Exception)
    assert called == [], "the request must not be sent once the which() probe refuses"


def test_the_substring_classifier_is_gone() -> None:
    """Fails if someone re-introduces a post-response body classifier.

    Not a tautology over the module's own constants: the r1 fix shipped
    ``provider_missing_from_host`` plus a three-marker set, and this asserts the
    whole shape is absent — a re-introduced helper under a new name still trips
    the second assertion because the fixture body no longer skips at all.
    """
    assert not hasattr(cao_server_fixture, "provider_missing_from_host")
    assert not hasattr(cao_server_fixture, "_HOST_CANNOT_RUN_MARKERS")

    import inspect

    source = inspect.getsource(_cao_terminal_body())
    assert (
        "pytest.skip"
        not in source.split("resp = requests.post", 1)[1].split("data = resp.json()", 1)[0]
    ), (
        "a skip reappeared between the failed POST /sessions and its response "
        "handling; the only sanctioned skips are the pre-flight --run-live gate "
        "and the shutil.which probe (F993 #841 r2)"
    )
