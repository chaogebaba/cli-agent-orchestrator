"""get_provider_defaults() parses providers.toml once per file version."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import settings_service as ss


@pytest.fixture
def toml_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "providers.toml"
    monkeypatch.setattr(ss, "PROVIDER_DEFAULTS_FILE", path)
    monkeypatch.setattr(ss, "_provider_defaults_cache", None)
    return path


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    real = ss.tomllib.loads

    def counting(text: str) -> dict:
        calls[0] += 1
        return real(text)

    monkeypatch.setattr(ss.tomllib, "loads", counting)
    return calls


def test_repeated_calls_parse_once(toml_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml_file.write_text('[logs]\nmax_file_mb = 7\n[codex]\nmodel = "m"\n')
    parses = _count_parses(monkeypatch)
    assert ss.get_provider_defaults("logs") == {"max_file_mb": 7}
    assert ss.get_provider_defaults("codex") == {"model": "m"}
    assert ss.get_provider_defaults("missing") == {}
    assert ss.get_logs_settings()["max_file_mb"] == 7
    assert parses[0] == 1


def test_rewrite_is_picked_up(toml_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml_file.write_text("[codex]\nmodel = 'a'\n")
    assert ss.get_provider_defaults("codex") == {"model": "a"}
    toml_file.write_text("[codex]\nmodel = 'bb'\n")
    # Same size would defeat a size-only key; force a distinct mtime too.
    st = toml_file.stat()
    os.utime(toml_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert ss.get_provider_defaults("codex") == {"model": "bb"}


def test_missing_and_invalid_file(toml_file: Path) -> None:
    assert ss.get_provider_defaults("codex") == {}
    toml_file.write_text("this is = not [valid toml")
    assert ss.get_provider_defaults("codex") == {}
    toml_file.write_text("codex = 'not a table'\n")
    st = toml_file.stat()
    os.utime(toml_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert ss.get_provider_defaults("codex") == {}


def test_callers_get_independent_copies(toml_file: Path) -> None:
    toml_file.write_text("[codex]\n[codex.profiles.dev]\nmodel = 'x'\n")
    first = ss.get_provider_defaults("codex")
    first["profiles"]["dev"]["model"] = "mutated"
    first["extra"] = 1
    assert ss.get_provider_defaults("codex") == {"profiles": {"dev": {"model": "x"}}}


def test_path_swap_misses_cache(
    toml_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml_file.write_text("[codex]\nmodel = 'a'\n")
    assert ss.get_provider_defaults("codex") == {"model": "a"}
    other = tmp_path / "other.toml"
    other.write_text("[codex]\nmodel = 'b'\n")
    monkeypatch.setattr(ss, "PROVIDER_DEFAULTS_FILE", other)
    assert ss.get_provider_defaults("codex") == {"model": "b"}
