"""Claude native-stash guard strategy tests."""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.services import draft_guard, terminal_service


class StashProvider:
    composer_stash_keys = ["C-s"]
    composer_clear_keys = ["C-u"]
    composer_stashed_chip_pattern = re.compile("CHIP")
    blocks_orchestrated_input_while_waiting_user_answer = True
    paste_enter_count = 1
    paste_submit_delay = 0.3

    def read_composer_draft(self, lines):
        for index, line in enumerate(lines):
            if line.startswith("DRAFT="):
                return "\n".join([line.removeprefix("DRAFT="), *lines[index + 1 :]])
        return None

    def mark_input_received(self):
        pass


def _setup(monkeypatch, tmp_path, frames):
    backend = type("Backend", (), {})()
    calls = []
    backend.send_special_key = lambda *args: calls.append(args)
    backend.get_history = lambda *args, **kwargs: next(frames)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)
    monkeypatch.setattr(draft_guard, "DRAFT_LOG_DIR", tmp_path)
    monkeypatch.setattr(draft_guard, "_wait_for_stable_draft", lambda *args: args[-1])
    return calls


def test_native_stash_confirms_without_restore_or_repasting(monkeypatch, tmp_path):
    calls = _setup(
        monkeypatch,
        tmp_path,
        iter(["DRAFT=hello", "DRAFT=hello", "DRAFT=hello", "CHIP\nDRAFT="]),
    )
    chip_present = draft_guard.stash_draft_before_send(
        "t", {"tmux_session": "s", "tmux_window": "w"}, StashProvider()
    )
    assert calls == [("s", "w", "C-s")]
    assert chip_present is True
    assert "hello" in (tmp_path / "t.log").read_text()


def test_chip_with_new_draft_clears_only_new_draft(monkeypatch, tmp_path):
    calls = _setup(
        monkeypatch,
        tmp_path,
        iter(["CHIP\nDRAFT=B", "CHIP\nDRAFT=B", "CHIP\nDRAFT=B", "CHIP\nDRAFT=B", "CHIP\nDRAFT="]),
    )
    chip_present = draft_guard.stash_draft_before_send(
        "t", {"tmux_session": "s", "tmux_window": "w"}, StashProvider()
    )
    assert calls == [("s", "w", "C-u")]
    assert chip_present is True
    assert "B" in (tmp_path / "t.log").read_text()


def test_chip_with_empty_composer_sends_no_composer_keys(monkeypatch, tmp_path):
    calls = _setup(
        monkeypatch,
        tmp_path,
        iter(["CHIP\nDRAFT=", "CHIP\nDRAFT="]),
    )
    chip_present = draft_guard.stash_draft_before_send(
        "t", {"tmux_session": "s", "tmux_window": "w"}, StashProvider()
    )
    assert calls == []
    assert chip_present is True


def test_unconfirmed_stash_never_repastes(monkeypatch, tmp_path):
    calls = _setup(
        monkeypatch,
        tmp_path,
        iter(["DRAFT=A", "DRAFT=A", "DRAFT=A", "DRAFT=A", "DRAFT=A", "DRAFT="]),
    )
    with pytest.raises(draft_guard.DeliveryDeferredError):
        draft_guard.stash_draft_before_send(
            "t", {"tmux_session": "s", "tmux_window": "w"}, StashProvider()
        )
    assert calls == [("s", "w", "C-s")]


def test_clear_is_bounded_by_draft_line_count_plus_three(monkeypatch, tmp_path):
    calls = _setup(
        monkeypatch,
        tmp_path,
        iter(
            ["CHIP\nDRAFT=one\ntwo", "CHIP\nDRAFT=one\ntwo", "CHIP\nDRAFT=one\ntwo"]
            + ["CHIP\nDRAFT=one\ntwo"] * 5
        ),
    )
    with pytest.raises(draft_guard.DeliveryDeferredError):
        draft_guard.stash_draft_before_send("t", {"tmux_session": "s", "tmux_window": "w"}, StashProvider())
    assert calls == [("s", "w", "C-u")] * 5


def test_changed_snapshots_fall_back_without_composer_keys(monkeypatch, tmp_path):
    calls = _setup(
        monkeypatch,
        tmp_path,
        iter([item for _ in range(3) for item in ("DRAFT=A", "DRAFT=B")]),
    )
    with pytest.raises(draft_guard.DeliveryDeferredError):
        draft_guard.stash_draft_before_send("t", {"tmux_session": "s", "tmux_window": "w"}, StashProvider())
    assert calls == []


def test_blank_and_indented_rows_log_verbatim_and_set_clear_bound(monkeypatch, tmp_path):
    draft = "first\n\n    code"

    def frame():
        return "\n".join(["› stashed", "─" * 20, "❯ first", "  ", "      code", "─" * 20])

    backend = type("Backend", (), {})()
    calls = []
    backend.send_special_key = lambda *args: calls.append(args)
    backend.get_history = lambda *args, **kwargs: frame()
    provider = ClaudeCodeProvider("t", "s", "w")
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)
    monkeypatch.setattr(draft_guard, "DRAFT_LOG_DIR", tmp_path)
    monkeypatch.setattr(draft_guard, "_wait_for_stable_draft", lambda *args: args[-1])

    with pytest.raises(draft_guard.DeliveryDeferredError):
        draft_guard.stash_draft_before_send("t", {"tmux_session": "s", "tmux_window": "w"}, provider)

    assert draft in (tmp_path / "t.log").read_text()
    assert calls == [("s", "w", "C-u")] * 6


@pytest.mark.parametrize(
    "fixture_name",
    ["fx3-dialog-capture.txt", "fx3-dialog-capture-sgr.txt"],
)
def test_dialog_raw_capture_defers_before_any_composer_key(
    monkeypatch, tmp_path, fixture_name
):
    capture = (
        Path(__file__).parents[1] / "fixtures" / "fx3" / fixture_name
    ).read_text()
    calls = _setup(monkeypatch, tmp_path, iter([capture]))

    with pytest.raises(draft_guard.DeliveryDeferredError):
        draft_guard.stash_draft_before_send(
            "t",
            {"tmux_session": "s", "tmux_window": "w"},
            StashProvider(),
            defer_on_dialog=True,
        )

    assert calls == []


def test_normal_snapshot_does_not_defer(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, iter(["DRAFT=", "DRAFT="]))

    assert (
        draft_guard.stash_draft_before_send(
            "t",
            {"tmux_session": "s", "tmux_window": "w"},
            StashProvider(),
            defer_on_dialog=True,
        )
        is False
    )
    assert calls == []


def test_send_input_default_does_not_defer_on_dialog(monkeypatch, tmp_path):
    capture = (
        Path(__file__).parents[1] / "fixtures" / "fx3" / "fx3-dialog-capture.txt"
    ).read_text()
    backend = MagicMock()
    backend.get_history.return_value = capture
    provider = StashProvider()
    metadata = {"tmux_session": "s", "tmux_window": "w"}
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _: metadata)
    monkeypatch.setattr(
        terminal_service.provider_manager, "get_provider", lambda _: provider
    )
    monkeypatch.setattr(terminal_service, "inject_memory_context", lambda message, _: message)
    monkeypatch.setattr(terminal_service, "update_last_active", lambda _: None)
    monkeypatch.setattr(terminal_service.status_monitor, "notify_input_sent", lambda _: None)
    monkeypatch.setattr(
        terminal_service.status_monitor, "clear_rolling_buffer", lambda _: None
    )

    with pytest.raises(draft_guard.DeliveryDeferredError):
        terminal_service.send_input("t", "message")

    backend.send_keys.assert_not_called()
    backend.send_special_key.assert_not_called()
