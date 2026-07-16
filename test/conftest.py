"""Repo-wide test fixtures."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Every clean-process suite run gets a private initialized schema before any
# test module can import the global database engine. This prevents tests from
# depending on (or migrating) the installed production database.
_TEST_CAO_HOME = Path(tempfile.mkdtemp(prefix="cao-pytest-"))
os.environ["CAO_HOME"] = str(_TEST_CAO_HOME)

from cli_agent_orchestrator.clients.database import engine, init_db  # noqa: E402

init_db()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Release the isolated suite database and remove its namespace."""
    engine.dispose()
    shutil.rmtree(_TEST_CAO_HOME, ignore_errors=True)


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
