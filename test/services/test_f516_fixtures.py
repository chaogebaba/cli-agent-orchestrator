"""F516 commit 1: frozen fixtures + injectable clock seam.

Validates that the frozen incident frame-sequences replay deterministically
through the real pyte compositor and that the ``_clock`` seams exist on both
auto_responder and status_monitor (the deterministic-backoff foundation the D4
ACs build on).
"""

import time

from test.helpers.dialog_replay import DialogReplay
from test.helpers.fake_clock import FakeClock

from cli_agent_orchestrator.services import auto_responder as ar
from cli_agent_orchestrator.services import status_monitor as sm
from cli_agent_orchestrator.services.auto_responder import dialog_region

CHOOSER_INCIDENTS = ["resume-chooser-61e1b848", "resume-chooser-c8c767ca"]
BANNER_INCIDENT = "content-policy-banner-f02ce13e"


def test_clock_seam_defaults_to_monotonic_on_both_modules():
    assert ar._clock is time.monotonic
    assert sm._clock is time.monotonic


def test_chooser_fixtures_render_the_resume_cwd_dialog_in_region():
    for incident in CHOOSER_INCIDENTS:
        replay = DialogReplay(incident)
        region = dialog_region(replay.final_rows())
        assert "Choose working directory to resume this session" in region.normalized
        assert "Press enter to continue" in region.normalized


def test_replay_advances_the_fake_clock_by_frame_offsets():
    replay = DialogReplay(CHOOSER_INCIDENTS[0])
    clock = FakeClock(start=0.0)
    observed_offsets = []

    def sink(_rows):
        observed_offsets.append(round(clock.monotonic(), 4))

    replay.play(clock, sink)

    manifest_offsets = [round(f.offset_s, 4) for f in replay.frames]
    assert observed_offsets == manifest_offsets
    # Deterministic: replaying again yields identical offsets.
    clock2 = FakeClock(start=0.0)
    again = []
    replay.play(clock2, lambda _r: again.append(round(clock2.monotonic(), 4)))
    assert again == observed_offsets


def test_banner_fixture_is_still_scrolling_at_its_final_frame():
    """AC2(b) delivery arm precondition: the banner fixture ends mid-motion."""
    replay = DialogReplay(BANNER_INCIDENT)
    frames = replay.frames
    assert len(frames) >= 2
    last = dialog_region(frames[-1].rows).normalized
    prev = dialog_region(frames[-2].rows).normalized
    assert last != prev, "banner fixture must end mid-motion, not settled"
