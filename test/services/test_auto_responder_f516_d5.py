"""F516 commit 5: D5 match-then-consume + per-rule reload reset.

Named outside the test_f516_*.py AC7-lint scope so it may drive the consume
internals directly (AC4/AC5 are about the consume-digest state machine).
"""

from unittest.mock import MagicMock

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import auto_responder as ar


def _metadata(**overrides):
    md = {
        "id": "term1",
        "tmux_session": "cao-sess",
        "tmux_window": "win",
        "provider": "codex",
        "provider_session_id": None,
        "lifecycle_generation": 7,
    }
    md.update(overrides)
    return md


class _WaitingProvider:
    def get_status_from_screen(self, _lines):
        return TerminalStatus.WAITING_USER_ANSWER


def _wire(monkeypatch, metadata, backend):
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata", lambda _t: metadata
    )
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.seam_activation.receiver_state_active", lambda _op: False
    )


def _backend(screen):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.capture_viewport.return_value = "\n".join(screen)
    return backend


def test_rule_body_hash_ignores_enabled_but_tracks_body():
    a = ar.Rule("r", True, "contains", "q", ["o"], ["Enter"])
    b = ar.Rule("r", False, "contains", "q", ["o"], ["Enter"])
    c = ar.Rule("r", True, "contains", "q2", ["o"], ["Enter"])
    assert a.body_hash == b.body_hash
    assert a.body_hash != c.body_hash


def test_ac4_consumed_digest_blocks_identical_redraw_refire(monkeypatch):
    screen = ["Do you trust this?", "1. Yes", "Press enter"]
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    engine = ar.AutoResponder()
    rule = ar.Rule("trust", True, "contains", "Do you trust this?", ["Yes"], ["Enter"])
    region = ar.dialog_region(screen)
    digest = ar._digest_normalized(region.normalized)
    pending = region.with_digests(settle=digest, consume=digest)

    assert engine._effect_barrier("term1", metadata, _WaitingProvider(), rule, pending)
    engine._record_consumed("term1", rule, digest)
    assert not engine._effect_barrier("term1", metadata, _WaitingProvider(), rule, pending)

    changed = ["Do you trust this?", "1. Yes", "2. No", "Press enter"]
    backend.capture_viewport.return_value = "\n".join(changed)
    changed_region = ar.dialog_region(changed)
    changed_digest = ar._digest_normalized(changed_region.normalized)
    changed_pending = changed_region.with_digests(settle=changed_digest, consume=changed_digest)
    assert engine._effect_barrier("term1", metadata, _WaitingProvider(), rule, changed_pending)


def test_ac5_changed_rule_body_resets_consume_state(monkeypatch):
    screen = ["Do you trust this?", "1. Yes", "Press enter"]
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    engine = ar.AutoResponder()
    region = ar.dialog_region(screen)
    digest = ar._digest_normalized(region.normalized)
    pending = region.with_digests(settle=digest, consume=digest)

    old_rule = ar.Rule("trust", True, "contains", "Do you trust this?", ["Yes"], ["Enter"])
    engine._record_consumed("term1", old_rule, digest)
    assert not engine._effect_barrier("term1", metadata, _WaitingProvider(), old_rule, pending)

    new_rule = ar.Rule("trust", True, "contains", "Do you trust this?", ["Yes"], ["y", "Enter"])
    assert new_rule.body_hash != old_rule.body_hash
    assert engine._effect_barrier("term1", metadata, _WaitingProvider(), new_rule, pending)


def test_clear_terminal_purges_consumed_digests():
    engine = ar.AutoResponder()
    rule = ar.Rule("r", True, "contains", "q", ["o"], ["Enter"])
    engine._record_consumed("term1", rule, "abc")
    engine._record_consumed("other", rule, "def")
    engine.clear_terminal("term1")
    assert ("other", rule.name, rule.body_hash) in engine._consumed_digests
    assert ("term1", rule.name, rule.body_hash) not in engine._consumed_digests


def test_consumed_digest_hit_does_not_request_retry(monkeypatch):
    screen = ["Do you trust this?", "Press enter"]
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    retries = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.schedule_detection_retry",
        lambda tid, *a, **k: retries.append(tid),
    )
    engine = ar.AutoResponder()
    rule = ar.Rule("trust", True, "contains", "Do you trust this?", ["enter"], ["Enter"])
    region = ar.dialog_region(screen)
    digest = ar._digest_normalized(region.normalized)
    pending = region.with_digests(settle=digest, consume=digest)
    engine._record_consumed("term1", rule, digest)

    assert not engine._effect_barrier("term1", metadata, _WaitingProvider(), rule, pending)
    assert retries == []
