"""F530 (#386) — a rule added to the yaml never fires on a stalled worker.

#386 accumulated twenty-odd occurrences under one reading: "the rule is present,
it textually matches the pane, the matcher said no match, so the matcher is
broken." The matcher is not broken — `TestTheMatcherIsNotTheFault` pins it
against the dialog text quoted in the issue (2026-08-29 04:20Z) and the rule the
reporter appended before spawning. Two other things were.

1. The store keyed its cache on `st_mtime` alone. A CAO seat on a box has an
   overlayfs home, and there a write and the append that follows carry the SAME
   mtime — the same `st_mtime_ns`, even — in 495 of 500 measured trials
   (grok-box-007); only `st_size` moves. So the appended rule was never loaded
   and the matcher walked a list that genuinely did not contain it. The rule
   "textually matches the pane" and is absent from the ruleset at the same time,
   which is exactly how the issue reads.

2. Nothing looks again. Rules are consulted only from `on_screen`, which runs on
   a detection tick; `status_monitor.schedule_detection_retry` allows six
   requests per silence episode and its only other reset edge is real pane
   output (`status_monitor.py:1565`). A pane stalled on a dialog produces
   neither, so about thirty seconds after it goes quiet the responder stops
   evaluating that terminal for good — the shape found live on 2026-08-30
   04:34Z: 81 decisions inside the six-second dialog-clear window, then nothing,
   the card rendering after it closed. `rearm_if_rules_changed` is that missing
   edge.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

import pytest

from cli_agent_orchestrator.services import auto_responder as ar

# The normalized dialog the reporter pasted into #386 (2026-08-29 04:20Z),
# with the elided paths filled back in — every rule substring is present.
RESUME_CWD_PANE: List[str] = [
    "7d3b8478 on  cao/7d3b8478 via 🐍 v3.14.7",
    "❯ codex resume 01a050dc -c model=gpt-5.1-codex",
    "",
    "  Choose working directory to resume this session",
    "  Session = latest cwd recorded in the resumed session",
    "  Current = your current working directory",
    "",
    "› 1. Use session directory (/home/chao/VScode_projects/cli-subagents)",
    "  2. Use current directory (/home/chao/VScode_projects/cli-subagents/.cao/"
    "worktrees/7e2ad242)",
    "  3. Always use session directory",
    "  4. Always use current directory",
    "",
    "  Press enter to continue",
]

# The rule appended to auto-answers/codex.yaml before the spawn, verbatim.
APPENDED_RULE = """\
- name: codex-resume-workdir-choice
  enabled: true
  match_mode: contains
  question: "Choose working directory to resume this session"
  options: ["Use session directory", "Use current directory"]
  answer: ["Down", "Enter"]
"""

BASELINE_RULE = """\
- name: codex-trust-dir
  enabled: true
  match_mode: contains
  question: "Do you trust the contents of this directory?"
  options: ["Yes, continue", "No, quit"]
  answer: ["Enter"]
"""


@pytest.fixture()
def rules_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated auto-answers dir holding only the baseline rule."""
    monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "auto-answers")
    monkeypatch.setattr(ar, "SEED_RULES", {})
    path = ar._rules_path("codex")
    path.write_text(BASELINE_RULE, encoding="utf-8")
    return path


class TestTheMatcherIsNotTheFault:
    """The claim twenty comments rested on, refuted and pinned."""

    def test_the_appended_rule_matches_the_issues_dialog(self, rules_file: Path) -> None:
        store = ar._RuleStore()
        rules_file.write_text(BASELINE_RULE + APPENDED_RULE, encoding="utf-8")
        rule = next(r for r in store.get_rules("codex") if r.name == "codex-resume-workdir-choice")
        region = ar.dialog_region(RESUME_CWD_PANE)
        assert rule.reject_reason(region) is None
        assert rule.matches(region) is True

    def test_an_append_is_picked_up_on_the_next_read(self, rules_file: Path) -> None:
        store = ar._RuleStore()
        assert [r.name for r in store.get_rules("codex")] == ["codex-trust-dir"]
        with rules_file.open("a", encoding="utf-8") as fh:
            fh.write(APPENDED_RULE)
        assert [r.name for r in store.get_rules("codex")] == [
            "codex-trust-dir",
            "codex-resume-workdir-choice",
        ]

    def test_an_append_under_an_unchanged_mtime_is_still_picked_up(self, rules_file: Path) -> None:
        """The store cannot key on mtime alone.

        On an overlayfs home — what a CAO seat on a box actually runs in — a
        write and the append that follows it carry the SAME `st_mtime` and the
        same `st_mtime_ns` in 495 of 500 measured trials (grok-box-007), because
        the filesystem's timestamp is coarser than the gap between two edits.
        Only `st_size` moved, in 500 of 500. Pinned here with `os.utime` so the
        collision is deterministic on any filesystem: an mtime-keyed cache goes
        on serving the ruleset from before the operator's edit, forever, and the
        rule they added is genuinely absent from the list the matcher walks.
        """
        store = ar._RuleStore()
        assert [r.name for r in store.get_rules("codex")] == ["codex-trust-dir"]
        frozen = rules_file.stat()

        with rules_file.open("a", encoding="utf-8") as fh:
            fh.write(APPENDED_RULE)
        os.utime(rules_file, ns=(frozen.st_atime_ns, frozen.st_mtime_ns))
        assert rules_file.stat().st_mtime_ns == frozen.st_mtime_ns, "mtime must be pinned"

        assert [r.name for r in store.get_rules("codex")] == [
            "codex-trust-dir",
            "codex-resume-workdir-choice",
        ]

    def test_a_same_size_rewrite_by_rename_is_picked_up(self, rules_file: Path) -> None:
        """An editor that saves by rename lands a new inode, and can land it
        with a backdated mtime and an identical size."""
        store = ar._RuleStore()
        assert [r.name for r in store.get_rules("codex")] == ["codex-trust-dir"]
        frozen = rules_file.stat()

        replacement = rules_file.with_suffix(".yaml.new")
        # Same byte length, different rule name — nothing but the inode moves.
        replacement.write_text(
            BASELINE_RULE.replace("codex-trust-dir", "codex-trust-dyr"), encoding="utf-8"
        )
        os.utime(replacement, ns=(frozen.st_atime_ns, frozen.st_mtime_ns))
        replacement.replace(rules_file)
        assert rules_file.stat().st_size == frozen.st_size

        assert [r.name for r in store.get_rules("codex")] == ["codex-trust-dyr"]

    def test_an_unchanged_file_is_not_re_read(self, rules_file: Path) -> None:
        store = ar._RuleStore()
        store.get_rules("codex")
        before = store.generation
        store.get_rules("codex")
        assert store.generation == before


class TestRulesChangedRearm:
    """The missing edge: a rule-file change makes something look again."""

    @staticmethod
    def _responder(monkeypatch: pytest.MonkeyPatch) -> tuple[ar.AutoResponder, list, list]:
        responder = ar.AutoResponder()
        refunded: list[str] = []
        ticked: list[str] = []
        monkeypatch.setattr(responder, "_request_detection_retry", ticked.append)

        class _Monitor:
            @staticmethod
            def reset_detection_retry_budget(terminal_id: str) -> None:
                refunded.append(terminal_id)

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor", _Monitor
        )
        return responder, refunded, ticked

    def test_first_sighting_is_not_a_change(
        self, rules_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A terminal that has never been evaluated has nothing to redo."""
        responder, refunded, ticked = self._responder(monkeypatch)
        assert responder.rearm_if_rules_changed("t1", "codex") is False
        assert (refunded, ticked) == ([], [])

    def test_an_edit_refunds_the_budget_and_asks_for_a_tick(
        self, rules_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        responder, refunded, ticked = self._responder(monkeypatch)
        responder.rearm_if_rules_changed("t1", "codex")  # establish the baseline
        with rules_file.open("a", encoding="utf-8") as fh:
            fh.write(APPENDED_RULE)

        assert responder.rearm_if_rules_changed("t1", "codex") is True
        assert refunded == ["t1"], "the spent retry budget must be refunded"
        assert ticked == ["t1"], "and a detection tick asked for"

    def test_a_quiet_file_never_re_arms(
        self, rules_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sweep runs every few seconds — an unchanged file must be silent,
        or this becomes a poll that re-evaluates every stalled pane forever."""
        responder, refunded, ticked = self._responder(monkeypatch)
        for _ in range(5):
            responder.rearm_if_rules_changed("t1", "codex")
        assert (refunded, ticked) == ([], [])

    def test_each_terminal_gets_its_own_re_arm(
        self, rules_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two seats stalled on the same dialog both need the edit."""
        responder, refunded, ticked = self._responder(monkeypatch)
        responder.rearm_if_rules_changed("t1", "codex")
        responder.rearm_if_rules_changed("t2", "codex")
        with rules_file.open("a", encoding="utf-8") as fh:
            fh.write(APPENDED_RULE)

        assert responder.rearm_if_rules_changed("t1", "codex") is True
        assert responder.rearm_if_rules_changed("t2", "codex") is True
        assert sorted(refunded) == ["t1", "t2"]

    def test_clear_terminal_forgets_the_generation(
        self, rules_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        responder, _, _ = self._responder(monkeypatch)
        responder.rearm_if_rules_changed("t1", "codex")
        responder.clear_terminal("t1")
        with rules_file.open("a", encoding="utf-8") as fh:
            fh.write(APPENDED_RULE)
        # A cleared terminal is a first sighting again, not a stale generation.
        assert responder.rearm_if_rules_changed("t1", "codex") is False
