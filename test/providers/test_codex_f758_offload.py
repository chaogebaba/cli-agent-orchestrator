"""F758 #615: codex over-long paste offload to a brief file + pointer.

codex-cli 0.153.x renders a long bracketed paste INLINE (no paste chip), so the
F435 verify cannot confirm/recover the submit and deferred-init tears the worker
down. Mechanize the file-pointer workaround: when the outgoing TASK BODY exceeds
CODEX_INLINE_PASTE_MAX bytes, write the full body to a brief file under
CAO_HOME_DIR and paste a short pointer; below the threshold the message is
byte-identical to today. The pasted pointer keeps the callback footer unchanged.

These tests drive the provider's real ``prepare_delivery_body`` with a fake pane
(no tmux, no real codex): they assert below/above-threshold behaviour, exact file
bytes, pointer shape (< 2 KB, contains the path), footer preservation, and that
the path is under CAO_HOME_DIR (never /tmp).
"""

from __future__ import annotations

import os

import pytest

import cli_agent_orchestrator.providers.codex as codex_mod
from cli_agent_orchestrator.providers.codex import (
    CODEX_INLINE_PASTE_MAX,
    CodexProvider,
)

_FOOTER = (
    "\n\n[Assigned by terminal d1203cc7. When done, send results back to terminal "
    "d1203cc7 using the cao-mcp-server send_message MCP tool — never a built-in "
    "collaboration.send_message]"
)


@pytest.fixture()
def cao_home(tmp_path, monkeypatch):
    """Point CAO_HOME_DIR (as codex.py imported it) at a tmp dir."""
    monkeypatch.setattr(codex_mod, "CAO_HOME_DIR", tmp_path)
    return tmp_path


def _provider(tid="term1234"):
    return CodexProvider(tid, "sess", "win")


def test_below_threshold_is_byte_identical(cao_home):
    p = _provider()
    body = "short task" + "x" * 100  # well under 1500 B
    msg = body + _FOOTER
    assert len(body.encode()) <= CODEX_INLINE_PASTE_MAX
    out = p.prepare_delivery_body(msg)
    assert out == msg  # byte-identical, no rewrite
    # No brief file written.
    assert not (cao_home / "briefs").exists()


def test_above_threshold_writes_file_and_pastes_pointer(cao_home):
    p = _provider("abcd1234")
    body = "GATE BRIEF\n" + ("payload line\n" * 300)  # > 1500 B
    assert len(body.encode()) > CODEX_INLINE_PASTE_MAX
    msg = body + _FOOTER
    out = p.prepare_delivery_body(msg)

    # A brief file exists under CAO_HOME_DIR/briefs/<terminal-id>/<msg-id>.md
    briefs_dir = cao_home / "briefs" / "abcd1234"
    written = list(briefs_dir.glob("*.md"))
    assert len(written) == 1, written
    brief = written[0]

    # Exact bytes: the file holds the full task body (footer NOT in the file).
    assert brief.read_text(encoding="utf-8") == body

    # Pasted text is a short pointer that names the absolute path + byte count.
    assert out != msg
    assert str(brief) in out
    assert f"({len(body.encode())} bytes)" in out
    assert out.startswith("Read and follow ")
    assert "Start now." in out
    # Pointer (incl. the preserved footer) is small — well under 2 KB.
    assert len(out.encode()) < 2048

    # Callback footer preserved UNCHANGED.
    assert out.endswith(_FOOTER)

    # Path is under CAO_HOME_DIR, never /tmp.
    assert str(brief).startswith(str(cao_home))
    assert "/tmp/" not in str(brief)


def test_file_permissions_0600_dir_0700(cao_home):
    p = _provider("perm5678")
    body = "y" * 4000
    p.prepare_delivery_body(body + _FOOTER)
    briefs_dir = cao_home / "briefs" / "perm5678"
    brief = next(briefs_dir.glob("*.md"))
    assert oct(brief.stat().st_mode)[-3:] == "600"
    assert oct(briefs_dir.stat().st_mode)[-3:] == "700"


def test_above_threshold_without_footer_still_offloads(cao_home):
    # A body with no callback footer (e.g. a plain send_message) offloads too;
    # the pointer then has no trailing footer.
    p = _provider("nofoot12")
    body = "z" * 3000
    out = p.prepare_delivery_body(body)
    brief = next((cao_home / "briefs" / "nofoot12").glob("*.md"))
    assert brief.read_text(encoding="utf-8") == body
    assert out.startswith("Read and follow ")
    assert out.endswith("Start now.")  # no footer appended


def test_threshold_measures_body_not_footer(cao_home):
    # A body just under the threshold must NOT offload even though body+footer
    # exceeds it (the threshold is on the TASK BODY, per the workaround).
    p = _provider("edge9999")
    body = "a" * (CODEX_INLINE_PASTE_MAX - 10)  # under threshold
    msg = body + _FOOTER  # body+footer is over threshold
    assert len(msg.encode()) > CODEX_INLINE_PASTE_MAX
    out = p.prepare_delivery_body(msg)
    assert out == msg  # unchanged: body alone is under threshold
    assert not (cao_home / "briefs").exists()


def test_body_exactly_at_cap_is_inline_no_brief(cao_home):
    # F758 stage-B F2: a body of EXACTLY CODEX_INLINE_PASTE_MAX bytes is INLINE
    # (the threshold is inclusive: `<=` → inline). Pins the inclusive cap so a
    # `<=`→`<` mutant that would offload the boundary body is killed.
    p = _provider("capexact")
    body = "a" * CODEX_INLINE_PASTE_MAX
    assert len(body.encode()) == CODEX_INLINE_PASTE_MAX
    msg = body + _FOOTER
    out = p.prepare_delivery_body(msg)
    assert out == msg  # byte-identical inline delivery
    assert not (cao_home / "briefs").exists()  # no brief written at the cap


def test_body_one_over_cap_offloads(cao_home):
    # F758 stage-B F2: cap+1 byte body DOES offload (paired boundary assertion).
    p = _provider("capplus1")
    body = "a" * (CODEX_INLINE_PASTE_MAX + 1)
    assert len(body.encode()) == CODEX_INLINE_PASTE_MAX + 1
    out = p.prepare_delivery_body(body + _FOOTER)
    brief = next((cao_home / "briefs" / "capplus1").glob("*.md"))
    assert brief.read_text(encoding="utf-8") == body  # full body persisted
    assert out.startswith("Read and follow ")
    assert out.endswith(_FOOTER)  # footer preserved


def test_msg_id_is_deterministic_content_hash(cao_home):
    # Same body → same <msg-id> filename (deterministic content hash).
    p = _provider("det12345")
    body = "b" * 3000
    p.prepare_delivery_body(body + _FOOTER)
    first = {f.name for f in (cao_home / "briefs" / "det12345").glob("*.md")}
    p.prepare_delivery_body(body + _FOOTER)  # re-deliver identical body
    second = {f.name for f in (cao_home / "briefs" / "det12345").glob("*.md")}
    assert first == second and len(second) == 1


def test_offload_failure_is_fail_open(cao_home, monkeypatch):
    # If the brief write fails, prepare_delivery_body returns the ORIGINAL
    # message (never worse than today) rather than raising into the send path.
    p = _provider("failopen")
    body = "c" * 3000
    msg = body + _FOOTER

    import pathlib

    orig_write = pathlib.Path.write_text

    def boom(self, *a, **k):
        if self.suffix == ".md" and "briefs" in str(self):
            raise OSError("disk full (simulated)")
        return orig_write(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    out = p.prepare_delivery_body(msg)
    assert out == msg  # fail-open to the original inline paste
