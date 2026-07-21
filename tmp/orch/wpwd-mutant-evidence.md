# WPWD Mutant Evidence

Replay base: `8b0eca4e961de1dd661fae5fe632ddc25544e3ae` (the production source is unchanged by this fix round).
Authority: `blueprints/wp-watchdog-delegation.md`
Authority SHA-256: `d7caebe8a53c26931337a6e335c487421871e15a6d8940e9536e26e9071d8403`

Every mutation is a complete tracked unified diff under `tmp/orch/wpwd-patches/` with a tracked raw selector log. Apply the patch, run the command shown in its log, then restore with the durable reverse-apply command. The red output below is verbatim from that run.

## B5 decision-seam proof

The fixed selector leaves `_fresh_frame_decides_running` real, supplies captured viewport/provider metadata, and calls `collect_due_notifications` once. The discriminator mutant (`return True, None` at the start of the real decision) is red; the restored source hash is recorded below.

- Green selector log: `tmp/orch/wpwd-patches/B5-decision-green.log`
- Mutant patch: `tmp/orch/wpwd-patches/B5-decision.patch`
- Mutant patch SHA-256: `b1bb648ae7ca6e2e35fcd61e4120a5d23a2703e04d79eec4fff26d52367a409f`
- Restore: `git apply -R tmp/orch/wpwd-patches/B5-decision.patch`
- Restored source SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08`

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_positive_grok_sample_keeps_existing_alarm_class
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
_____________ test_positive_grok_sample_keeps_existing_alarm_class _____________

    def test_positive_grok_sample_keeps_existing_alarm_class():
        sample = (
            Path(__file__).parents[1]
            / "fixtures/error-pane-samples/2026-07-20-grok-roster-flap-d86a724d.txt"
        )
        captured = sample.read_bytes()
        rows = captured.decode("utf-8").splitlines()
        provider = _watchdog_grok_provider()
        classification = provider.classify_screen(rows)
        assert classification.status == TerminalStatus.UNKNOWN
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "worker1", "caller1", inbound_at=0, idle_since=10, sampled=True)
        backend = MagicMock()
        backend.capture_viewport.return_value = captured.decode("utf-8")
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.terminal_exists",
                side_effect=lambda terminal_id: terminal_id in {"worker1", "caller1"},
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value={
                    "id": "worker1",
                    "provider": "grok_cli",
                    "tmux_session": "cao-test",
                    "tmux_window": "worker1",
                },
            ),
            patch("cli_agent_orchestrator.services.seam_activation.receiver_state_active", return_value=False),
            patch("cli_agent_orchestrator.providers.manager.provider_manager.get_provider", return_value=provider),
        ):
            notices = svc.collect_due_notifications(now=13)
>       assert notices[0].message == "[watchdog] worker worker1 (developer) idle 3s without callback"
               ^^^^^^^^^^
E       IndexError: list index out of range

test/services/test_stalled_callback_watchdog.py:1196: IndexError
------------------------------ Captured log call -------------------------------
ERROR    cli_agent_orchestrator.clients.tmux:tmux.py:586 Failed to get working directory for cao-test:worker1:
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_positive_grok_sample_keeps_existing_alarm_class
1 failed in 0.30s

```

- Restored selector output:

```text
.                                                                        [100%]
1 passed in 0.20s
```

## M1-M16 replay matrix

### M1

- Patch: `tmp/orch/wpwd-patches/M1.patch`
- Patch SHA-256: `191bbe96431b7542eead9b13336fb34248524178bbe8414fcd16140bef9e6295`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M1.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M1.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..3eb0a89 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -253,14 +253,7 @@ class StalledCallbackWatchdog:
             }

     def _blockers_locked(self, worker_id: str) -> list[tuple[str, _Episode]]:
-        return [
-            (terminal_id, episode)
-            for terminal_id, episode in self._episodes.items()
-            if episode.caller_id == worker_id
-            and not episode.callback_seen
-            and terminal_id not in self._paused
-            and terminal_exists(terminal_id)
-        ]
+        return []

     def record_callback_if_to_caller(self, sender_id: str, receiver_id: str) -> None:
         meta = get_terminal_metadata(sender_id)
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_waiting_blocker_suppression_resolution_preserves_original_clock
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
_____ test_waiting_blocker_suppression_resolution_preserves_original_clock _____

    def test_waiting_blocker_suppression_resolution_preserves_original_clock():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=10)
        with _relational_watchdog_fakes({"W", "T", "C"}):
>           assert svc.collect_due_notifications(now=20) == []
E           AssertionError: assert [WatchdogNoti...kind='stall')] == []
E
E             Left contains one more item: WatchdogNotice(terminal_id='W', caller_id='C', message='[watchdog] worker W (developer) idle 20s without callback', idle_reason=None, source_generation=1, kind='stall')
E             Use -v to get more diff

test/services/test_stalled_callback_watchdog.py:1027: AssertionError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_waiting_blocker_suppression_resolution_preserves_original_clock
1 failed in 0.26s
```

### M2

- Patch: `tmp/orch/wpwd-patches/M2.patch`
- Patch SHA-256: `6a813befd49434e62f7afcbe1fb352833da1ba98a47c69280363fdcbdb55d794`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M2.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M2.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..2501e63 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -577,7 +577,7 @@ class StalledCallbackWatchdog:
                 if candidate.phase_p_waiting and not blockers:
                     continue
                 if blockers:
-                    oldest_inbound_at = min(episode.inbound_at for _, episode in blockers)
+                    oldest_inbound_at = current_episode.idle_since
                     last_push = current_episode.waiting_last_push_at
                     if (
                         now - oldest_inbound_at >= WATCHDOG_WAITING_ESCALATE_S
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_waiting_safety_net_repeats_on_oldest_inbound_clock
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
___________ test_waiting_safety_net_repeats_on_oldest_inbound_clock ____________

    def test_waiting_safety_net_repeats_on_oldest_inbound_clock():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        # The waiter became idle recently, but its blocker has been outstanding
        # since epoch zero.  The escalation age must use the blocker clock.
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=900, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=0)
        with _relational_watchdog_fakes({"W", "T", "C"}):
>           notice = svc.collect_due_notifications(now=1000)[0]
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E           IndexError: list index out of range

test/services/test_stalled_callback_watchdog.py:1068: IndexError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_waiting_safety_net_repeats_on_oldest_inbound_clock
1 failed in 0.21s
```

### M3

- Patch: `tmp/orch/wpwd-patches/M3.patch`
- Patch SHA-256: `e0e1358ae51109d562a8eb0631463a9710b67d90f9299cc54e24110fe5badfea`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M3.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M3.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..10bbd4d 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -604,6 +604,7 @@ class StalledCallbackWatchdog:
                                 kind="waiting",
                             )
                         )
+                    current_episode.idle_since = now
                     continue
                 if callback_status in {
                     MessageStatus.PENDING,
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_waiting_blocker_suppression_resolution_preserves_original_clock
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
_____ test_waiting_blocker_suppression_resolution_preserves_original_clock _____

    def test_waiting_blocker_suppression_resolution_preserves_original_clock():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=10)
        with _relational_watchdog_fakes({"W", "T", "C"}):
            assert svc.collect_due_notifications(now=20) == []
>           assert svc._episodes["W"].idle_since == 0
E           AssertionError: assert 20 == 0
E            +  where 20 = _Episode(caller_id='C', profile='developer', inbound_at=0, episode_started_wall_at=datetime.datetime(2026, 7, 21, 1, 2...n=1, revision=0, auto_resumed=False, resume_reserved_at=None, auto_resume_attempted_at=None, waiting_last_push_at=None).idle_since

test/services/test_stalled_callback_watchdog.py:1028: AssertionError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_waiting_blocker_suppression_resolution_preserves_original_clock
1 failed in 0.24s
```

### M4

- Patch: `tmp/orch/wpwd-patches/M4.patch`
- Patch SHA-256: `3da07209e27f464bfb2c03f416f1e615730fd2735daa1f0f00ea5a3b4d039f11`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M4.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M4.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..3b99ed1 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -541,26 +541,25 @@ class StalledCallbackWatchdog:
             suppress = False
             fallback_idle_reason = None
             second_callback_status = None
-            if not candidate.phase_p_waiting:
-                callback_status = get_callback_status_since(
+            callback_status = get_callback_status_since(
+                candidate.terminal_id,
+                candidate.caller_id,
+                candidate.episode_started_wall_at,
+            )
+            if (
+                metadata is not None
+                and callback_status is None
+                and (not auto_resume_applicable or candidate.episode.auto_resumed)
+            ):
+                frame_decides_running, fallback_idle_reason = self._fresh_frame_decides_running(
+                    candidate.terminal_id
+                )
+                suppress = frame_decides_running and not auto_resume_applicable
+                second_callback_status = get_callback_status_since(
                     candidate.terminal_id,
                     candidate.caller_id,
                     candidate.episode_started_wall_at,
                 )
-                if (
-                    metadata is not None
-                    and callback_status is None
-                    and (not auto_resume_applicable or candidate.episode.auto_resumed)
-                ):
-                    frame_decides_running, fallback_idle_reason = self._fresh_frame_decides_running(
-                        candidate.terminal_id
-                    )
-                    suppress = frame_decides_running and not auto_resume_applicable
-                    second_callback_status = get_callback_status_since(
-                        candidate.terminal_id,
-                        candidate.caller_id,
-                        candidate.episode_started_wall_at,
-                    )
             action: AutoResumeAction | None = None
             with self._lock:
                 current_episode = self._episodes.get(candidate.terminal_id)
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_phase_p_waiting_skips_frame_and_defers_when_phase_a_clears
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
_______ test_phase_p_waiting_skips_frame_and_defers_when_phase_a_clears ________

self = <MagicMock name='_fresh_frame_decides_running' id='139937906845280'>

    def assert_not_called(self):
        """assert that the mock was never called.
        """
        if self.call_count != 0:
            msg = ("Expected '%s' to not have been called. Called %s times.%s"
                   % (self._mock_name or 'mock',
                      self.call_count,
                      self._calls_repr()))
>           raise AssertionError(msg)
E           AssertionError: Expected '_fresh_frame_decides_running' to not have been called. Called 1 times.
E           Calls: [call('W')].

../../../.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py:940: AssertionError

During handling of the above exception, another exception occurred:

    def test_phase_p_waiting_skips_frame_and_defers_when_phase_a_clears():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=1)
        live = {"W", "T", "C"}
        flipped = False

        def metadata(terminal_id):
            nonlocal flipped
            if terminal_id == "W" and not flipped:
                flipped = True
                svc._episodes["T"].callback_seen = True
            return {"id": terminal_id, "provider": "grok_cli"}

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.terminal_exists",
                side_effect=lambda terminal_id: terminal_id in live,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=metadata,
            ),
            patch.object(
                StalledCallbackWatchdog,
                "_fresh_frame_decides_running",
                return_value=(False, None),
            ) as fresh,
        ):
            assert svc.collect_due_notifications(now=20) == []
>           fresh.assert_not_called()
E           AssertionError: Expected '_fresh_frame_decides_running' to not have been called. Called 1 times.
E           Calls: [call('W')].
E
E           pytest introspection follows:
E
E           Args:
E           assert ('W',) == ()
E
E             Left contains one more item: 'W'
E             Use -v to get more diff

test/services/test_stalled_callback_watchdog.py:1105: AssertionError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_phase_p_waiting_skips_frame_and_defers_when_phase_a_clears
1 failed in 0.23s
```

### M5

- Patch: `tmp/orch/wpwd-patches/M5.patch`
- Patch SHA-256: `4c56395f0e87384253128ee4769b28078b6507fffa83e7f02922ef4c049ceec8`
- Target start/restored SHA-256: `f8be540c14b393441e18876c19ed40db471ec90d2396838360928725fbb3b1c4` (`src/cli_agent_orchestrator/services/inbox_service.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M5.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M5.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/inbox_service.py b/src/cli_agent_orchestrator/services/inbox_service.py
index f310a4a..40b4aad 100644
--- a/src/cli_agent_orchestrator/services/inbox_service.py
+++ b/src/cli_agent_orchestrator/services/inbox_service.py
@@ -749,7 +749,7 @@ class InboxService:
         if sender_id.startswith("watchdog:"):
             return
         stalled_callback_watchdog.record_callback_if_to_caller(sender_id, terminal_id)
-        if metadata.get("caller_id") and not park_warm and (
+        if metadata.get("caller_id") and (
             orchestration_type == OrchestrationType.ASSIGN
             or (
                 orchestration_type == OrchestrationType.SEND_MESSAGE
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_parked_commit_still_settles_sender_and_never_clears_existing_episode
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
__ test_parked_commit_still_settles_sender_and_never_clears_existing_episode ___

self = <MagicMock name='stalled_callback_watchdog.record_inbound_task' id='140511998154064'>

    def assert_not_called(self):
        """assert that the mock was never called.
        """
        if self.call_count != 0:
            msg = ("Expected '%s' to not have been called. Called %s times.%s"
                   % (self._mock_name or 'mock',
                      self.call_count,
                      self._calls_repr()))
>           raise AssertionError(msg)
E           AssertionError: Expected 'record_inbound_task' to not have been called. Called 1 times.
E           Calls: [call('worker1', 'caller1', 'developer')].

../../../.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py:940: AssertionError

During handling of the above exception, another exception occurred:

    def test_parked_commit_still_settles_sender_and_never_clears_existing_episode():
        from cli_agent_orchestrator.services.inbox_service import InboxService

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog"
        ) as watchdog:
            InboxService()._commit_watchdog_ops(
                "worker1",
                "caller1",
                OrchestrationType.SEND_MESSAGE,
                {"caller_id": "caller1", "agent_profile": "developer"},
                park_warm=True,
            )
        watchdog.record_callback_if_to_caller.assert_called_once_with("caller1", "worker1")
>       watchdog.record_inbound_task.assert_not_called()
E       AssertionError: Expected 'record_inbound_task' to not have been called. Called 1 times.
E       Calls: [call('worker1', 'caller1', 'developer')].
E
E       pytest introspection follows:
E
E       Args:
E       assert ('worker1', '..., 'developer') == ()
E
E         Left contains 3 more items, first extra item: 'worker1'
E         Use -v to get more diff

test/services/test_stalled_callback_watchdog.py:339: AssertionError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_parked_commit_still_settles_sender_and_never_clears_existing_episode
1 failed in 0.30s
```

### M6

- Patch: `tmp/orch/wpwd-patches/M6.patch`
- Patch SHA-256: `bc29e873e48a1d2e4ac7f57137b9445a1d57e2898f0c234751947c13c4bcac33`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M6.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M6.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..caca7ed 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -258,6 +258,7 @@ class StalledCallbackWatchdog:
             for terminal_id, episode in self._episodes.items()
             if episode.caller_id == worker_id
             and not episode.callback_seen
+            and not episode.fired
             and terminal_id not in self._paused
             and terminal_exists(terminal_id)
         ]
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py test/services/test_wp_watchdog_production_paths.py
EXIT_CODE=1
........................................................FF........       [100%]
=================================== FAILURES ===================================
_________ test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b _________

    def test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=0, idle_since=0, sampled=True)
        rows = []

        with (
            _relational_watchdog_fakes({"W", "T", "C"}),
            patch.object(svc, "_persist_notice", side_effect=rows.append),
            patch("cli_agent_orchestrator.services.inbox_service.inbox_service") as inbox,
        ):
            svc.notify_due()
            svc.notify_due()
>       assert [notice.kind for notice in rows] == ["stall", "chain"]
E       AssertionError: assert ['waiting', 'stall', 'stall'] == ['stall', 'chain']
E
E         At index 0 diff: 'waiting' != 'stall'
E         Left contains one more item: 'stall'
E         Use -v to get more diff

test/services/test_stalled_callback_watchdog.py:1219: AssertionError
__ test_trigger_a_rollover_is_stale_and_insert_failure_rolls_back_reservation __

    def test_trigger_a_rollover_is_stale_and_insert_failure_rolls_back_reservation():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=0, idle_since=0, sampled=True)
        with _relational_watchdog_fakes({"W", "T", "C"}):
            original_collect = svc.collect_due_notifications

            def collect_and_rearm(*, now=None):
                result = original_collect(now=now)
                svc._episodes["T"].generation += 1
                return result

            with (
                patch.object(svc, "collect_due_notifications", side_effect=collect_and_rearm),
                patch.object(svc, "_persist_notice") as persist,
                patch("cli_agent_orchestrator.services.inbox_service.inbox_service"),
            ):
                svc.notify_due()
            assert [call.args[0].kind for call in persist.call_args_list] == ["waiting", "stall"]
            assert not svc._chain_notified

        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=0, idle_since=0, sampled=True)
        with (
            _relational_watchdog_fakes({"W", "T", "C"}),
            patch.object(svc, "_persist_notice", side_effect=[None, RuntimeError("db")]),
            patch("cli_agent_orchestrator.services.inbox_service.inbox_service"),
        ):
            svc.notify_due()
        assert not svc._chain_notified

        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=0, idle_since=0, sampled=True)
        persisted = []
        with (
            _relational_watchdog_fakes({"W", "T", "C"}),
            patch.object(svc, "_persist_notice", side_effect=persisted.append),
            patch(
                "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
                side_effect=RuntimeError("delivery"),
            ),
        ):
            svc.notify_due()
            svc.notify_due()
>       assert [notice.kind for notice in persisted] == ["stall", "chain"]
E       AssertionError: assert ['waiting', 'stall', 'stall'] == ['stall', 'chain']
E
E         At index 0 diff: 'waiting' != 'stall'
E         Left contains one more item: 'stall'
E         Use -v to get more diff

test/services/test_stalled_callback_watchdog.py:1270: AssertionError
------------------------------ Captured log call -------------------------------
ERROR    cli_agent_orchestrator.services.stalled_callback_watchdog:stalled_callback_watchdog.py:920 Failed to push stalled-callback watchdog notification
Traceback (most recent call last):
  File "/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py", line 918, in notify_due
    self._persist_notice(notice)
    ~~~~~~~~~~~~~~~~~~~~^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1169, in __call__
    return self._mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1173, in _mock_call
    return self._execute_mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1238, in _execute_mock_call
    raise result
RuntimeError: db
ERROR    cli_agent_orchestrator.services.stalled_callback_watchdog:stalled_callback_watchdog.py:937 Failed to deliver stalled-callback watchdog notification
Traceback (most recent call last):
  File "/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py", line 935, in notify_due
    inbox_service.deliver_pending(notice.caller_id, registry=registry)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1169, in __call__
    return self._mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1173, in _mock_call
    return self._execute_mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1234, in _execute_mock_call
    raise effect
RuntimeError: delivery
ERROR    cli_agent_orchestrator.services.stalled_callback_watchdog:stalled_callback_watchdog.py:937 Failed to deliver stalled-callback watchdog notification
Traceback (most recent call last):
  File "/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py", line 935, in notify_due
    inbox_service.deliver_pending(notice.caller_id, registry=registry)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1169, in __call__
    return self._mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1173, in _mock_call
    return self._execute_mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1234, in _execute_mock_call
    raise effect
  File "/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py", line 935, in notify_due
    inbox_service.deliver_pending(notice.caller_id, registry=registry)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1169, in __call__
    return self._mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1173, in _mock_call
    return self._execute_mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1234, in _execute_mock_call
    raise effect
RuntimeError: delivery
ERROR    cli_agent_orchestrator.services.stalled_callback_watchdog:stalled_callback_watchdog.py:937 Failed to deliver stalled-callback watchdog notification
Traceback (most recent call last):
  File "/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py", line 935, in notify_due
    inbox_service.deliver_pending(notice.caller_id, registry=registry)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1169, in __call__
    return self._mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1173, in _mock_call
    return self._execute_mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1234, in _execute_mock_call
    raise effect
  File "/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py", line 935, in notify_due
    inbox_service.deliver_pending(notice.caller_id, registry=registry)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1169, in __call__
    return self._mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1173, in _mock_call
    return self._execute_mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1234, in _execute_mock_call
    raise effect
  File "/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py", line 935, in notify_due
    inbox_service.deliver_pending(notice.caller_id, registry=registry)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1169, in __call__
    return self._mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1173, in _mock_call
    return self._execute_mock_call(*args, **kwargs)
           ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/home/chao/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/lib/python3.13/unittest/mock.py", line 1234, in _execute_mock_call
    raise effect
RuntimeError: delivery
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.venv/lib/python3.13/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323
.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323
  /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.11/migration/
    warnings.warn(DEPRECATION_MESSAGE, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b
FAILED test/services/test_stalled_callback_watchdog.py::test_trigger_a_rollover_is_stale_and_insert_failure_rolls_back_reservation
2 failed, 64 passed, 3 warnings in 0.89s
```

### M7

- Patch: `tmp/orch/wpwd-patches/M7.patch`
- Patch SHA-256: `053ddf30392b86e64c41eab741c0c0ab5446815baa843a8c7377fde865e326c5`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M7.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M7.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..2a3e17c 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -855,8 +855,6 @@ class StalledCallbackWatchdog:
                 notice.terminal_id,
                 notice.source_generation,
             )
-            if key in self._chain_notified:
-                return None
             self._chain_notified.add(key)
             chain_notice = WatchdogNotice(
                 terminal_id=worker_id,
```

```text
$ uv run pytest -q test/services/test_wp_watchdog_production_paths.py::test_notify_replaying_current_stall_persists_one_durable_chain_row
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
______ test_notify_replaying_current_stall_persists_one_durable_chain_row ______

prod_db = sessionmaker(class_='Session', bind=Engine(sqlite:////tmp/pytest-of-chao/pytest-287/test_notify_replaying_current_0/wpwd.sqlite), autoflush=True, expire_on_commit=False)

    def test_notify_replaying_current_stall_persists_one_durable_chain_row(prod_db):
        with prod_db.begin() as db:
            _terminal(db, "caller")
            _terminal(db, "worker", caller_id="caller")
            _terminal(db, "target", caller_id="worker")
        svc = StalledCallbackWatchdog(grace_seconds=3)
        svc.record_inbound_task("worker", "caller", "developer")
        svc.record_inbound_task("target", "worker", "developer")
        svc.record_status("worker", TerminalStatus.IDLE, now=0)
        notice = WatchdogNotice("target", "worker", "stall", None, source_generation=1)
        with (
            patch.object(svc, "collect_due_notifications", return_value=[notice]),
            patch("cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending"),
        ):
            svc.notify_due()
            svc.notify_due()
        with prod_db() as db:
            rows = db.query(InboxModel).filter(InboxModel.message.like("%chain stalled%" )).all()
>           assert len(rows) == 1
E           assert 2 == 1
E            +  where 2 = len([<cli_agent_orchestrator.clients.database.InboxModel object at 0x7ff4fdc3e3f0>, <cli_agent_orchestrator.clients.database.InboxModel object at 0x7ff4fc441c70>])

test/services/test_wp_watchdog_production_paths.py:197: AssertionError
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.venv/lib/python3.13/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323
.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323
  /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.11/migration/
    warnings.warn(DEPRECATION_MESSAGE, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED test/services/test_wp_watchdog_production_paths.py::test_notify_replaying_current_stall_persists_one_durable_chain_row
1 failed, 3 warnings in 0.77s
```

### M8

- Patch: `tmp/orch/wpwd-patches/M8.patch`
- Patch SHA-256: `f7da35255382b9482bb8544e666cd4799eb939c8589208f124626a355e7d59a1`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M8.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M8.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..9e5e1b2 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -860,7 +860,7 @@ class StalledCallbackWatchdog:
             self._chain_notified.add(key)
             chain_notice = WatchdogNotice(
                 terminal_id=worker_id,
-                caller_id=worker_episode.caller_id,
+                caller_id=notice.caller_id,
                 message=(
                     f"[watchdog] chain stalled: worker {worker_id} "
                     f"({worker_episode.profile}) has been waiting "
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
_________ test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b _________

    def test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=0, idle_since=0, sampled=True)
        rows = []

        with (
            _relational_watchdog_fakes({"W", "T", "C"}),
            patch.object(svc, "_persist_notice", side_effect=rows.append),
            patch("cli_agent_orchestrator.services.inbox_service.inbox_service") as inbox,
        ):
            svc.notify_due()
            svc.notify_due()
>       assert [notice.kind for notice in rows] == ["stall", "chain"]
E       AssertionError: assert ['waiting', 'stall', 'chain'] == ['stall', 'chain']
E
E         At index 0 diff: 'waiting' != 'stall'
E         Left contains one more item: 'chain'
E         Use -v to get more diff

test/services/test_stalled_callback_watchdog.py:1219: AssertionError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b
1 failed in 0.27s
```

### M9

- Patch: `tmp/orch/wpwd-patches/M9.patch`
- Patch SHA-256: `804424fd2d5513a308bf8aa9335f90ef8ceceffc01473ec5bd35acbf2a1142d7`
- Target start/restored SHA-256: `f8be540c14b393441e18876c19ed40db471ec90d2396838360928725fbb3b1c4` (`src/cli_agent_orchestrator/services/inbox_service.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M9.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M9.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/inbox_service.py b/src/cli_agent_orchestrator/services/inbox_service.py
index f310a4a..8761cf5 100644
--- a/src/cli_agent_orchestrator/services/inbox_service.py
+++ b/src/cli_agent_orchestrator/services/inbox_service.py
@@ -1290,7 +1290,6 @@ class InboxService:
                             key=lambda item: (
                                 item.sender_id,
                                 item.orchestration_type,
-                                bool(getattr(item, "park_warm", False)),
                             ),
                         )
                     )
@@ -1331,15 +1330,15 @@ class InboxService:
             # all pending messages (num_messages=0) a batch can span multiple groups,
             # so each run is sent separately to keep attribution and shaping correct.
             sent_count = 0
-            for (sender_id, orchestration_type, park_warm), group in groupby(
+            for (sender_id, orchestration_type), group in groupby(
                 messages,
                 key=lambda m: (
                     m.sender_id,
                     m.orchestration_type,
-                    bool(getattr(m, "park_warm", False)),
                 ),
             ):
                 batch = list(group)
+                park_warm = bool(getattr(batch[0], "park_warm", False))
                 combined = "\n".join(m.message for m in batch)
                 attempt_uuid = None
                 submit_observation = None
```

```text
$ uv run pytest -q test/services/test_inbox_service.py::TestDeliverPending::test_mixed_park_warm_batches_are_homogeneous_and_only_normal_arms
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
_ TestDeliverPending.test_mixed_park_warm_batches_are_homogeneous_and_only_normal_arms _

self = <test.services.test_inbox_service.TestDeliverPending object at 0x7fc4d871ed50>
mock_get = <MagicMock name='get_pending_messages' id='140483421310992'>
mock_metadata = <MagicMock name='get_terminal_metadata' id='140483421311664'>
mock_monitor = <MagicMock name='status_monitor' id='140483421312000'>
mock_term_svc = <MagicMock id='140483421312336'>
_mock_update = <MagicMock name='update_message_status' id='140483421313344'>

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch(
        "cli_agent_orchestrator.services.inbox_service.terminal_service",
        new_callable=_terminal_service_mock,
    )
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_mixed_park_warm_batches_are_homogeneous_and_only_normal_arms(
        self, mock_get, mock_metadata, mock_monitor, mock_term_svc, _mock_update
    ):
        mock_get.return_value = [
            _make_message(id=1, message="normal", park_warm=False),
            _make_message(id=2, message="parked-a", park_warm=True),
            _make_message(id=3, message="parked-b", park_warm=True),
        ]
        mock_metadata.return_value = {
            "provider": "event",
            "caller_id": "sender-1",
            "agent_profile": "developer",
        }
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        def settle(*_args, **kwargs):
            if callback := kwargs.get("on_confirmed"):
                callback()

        with (
            patch(
                "cli_agent_orchestrator.services.inbox_service.settle_delivery_attempt",
                side_effect=settle,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "stalled_callback_watchdog"
            ) as watchdog,
        ):
            InboxService().deliver_pending("term-1", num_messages=0)

>       assert [call.args[1] for call in mock_term_svc.send_prepared_input.call_args_list] == [
            "normal",
            "parked-a\nparked-b",
        ]
E       AssertionError: assert ['normal\nparked-a\nparked-b'] == ['normal', 'p...-a\nparked-b']
E
E         At index 0 diff: 'normal\nparked-a\nparked-b' != 'normal'
E         Right contains one more item: 'parked-a\nparked-b'
E         Use -v to get more diff

test/services/test_inbox_service.py:407: AssertionError
------------------------------ Captured log call -------------------------------
WARNING  cli_agent_orchestrator.services.message_trace_service:message_trace_service.py:406 No authoritative session transcript is resolvable for terminal unknown; deliveries will be recorded as send_returned_unverified
=========================== short test summary info ============================
FAILED test/services/test_inbox_service.py::TestDeliverPending::test_mixed_park_warm_batches_are_homogeneous_and_only_normal_arms
1 failed in 0.29s
```

### M10

- Patch: `tmp/orch/wpwd-patches/M10.patch`
- Patch SHA-256: `6a813befd49434e62f7afcbe1fb352833da1ba98a47c69280363fdcbdb55d794`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M10.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M10.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..2501e63 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -577,7 +577,7 @@ class StalledCallbackWatchdog:
                 if candidate.phase_p_waiting and not blockers:
                     continue
                 if blockers:
-                    oldest_inbound_at = min(episode.inbound_at for _, episode in blockers)
+                    oldest_inbound_at = current_episode.idle_since
                     last_push = current_episode.waiting_last_push_at
                     if (
                         now - oldest_inbound_at >= WATCHDOG_WAITING_ESCALATE_S
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_waiting_safety_net_repeats_on_oldest_inbound_clock
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
___________ test_waiting_safety_net_repeats_on_oldest_inbound_clock ____________

    def test_waiting_safety_net_repeats_on_oldest_inbound_clock():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        # The waiter became idle recently, but its blocker has been outstanding
        # since epoch zero.  The escalation age must use the blocker clock.
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=900, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=0)
        with _relational_watchdog_fakes({"W", "T", "C"}):
>           notice = svc.collect_due_notifications(now=1000)[0]
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E           IndexError: list index out of range

test/services/test_stalled_callback_watchdog.py:1068: IndexError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_waiting_safety_net_repeats_on_oldest_inbound_clock
1 failed in 0.29s
```

### M11

- Patch: `tmp/orch/wpwd-patches/M11.patch`
- Patch SHA-256: `db719acaebd06f07b88dc1be4c1f007de521fa81f354d5a61fe6ed6c6dff2f21`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M11.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M11.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..b8b56d9 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -574,8 +574,6 @@ class StalledCallbackWatchdog:
                 if int(now - current_episode.idle_since) < self.grace_seconds:
                     continue
                 blockers = self._blockers_locked(candidate.terminal_id)
-                if candidate.phase_p_waiting and not blockers:
-                    continue
                 if blockers:
                     oldest_inbound_at = min(episode.inbound_at for _, episode in blockers)
                     last_push = current_episode.waiting_last_push_at
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py
EXIT_CODE=1
...................................................F......               [100%]
=================================== FAILURES ===================================
_______ test_phase_p_waiting_skips_frame_and_defers_when_phase_a_clears ________

    def test_phase_p_waiting_skips_frame_and_defers_when_phase_a_clears():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=1)
        live = {"W", "T", "C"}
        flipped = False

        def metadata(terminal_id):
            nonlocal flipped
            if terminal_id == "W" and not flipped:
                flipped = True
                svc._episodes["T"].callback_seen = True
            return {"id": terminal_id, "provider": "grok_cli"}

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.terminal_exists",
                side_effect=lambda terminal_id: terminal_id in live,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=metadata,
            ),
            patch.object(
                StalledCallbackWatchdog,
                "_fresh_frame_decides_running",
                return_value=(False, None),
            ) as fresh,
        ):
>           assert svc.collect_due_notifications(now=20) == []
E           AssertionError: assert [WatchdogNoti...kind='stall')] == []
E
E             Left contains one more item: WatchdogNotice(terminal_id='W', caller_id='C', message='[watchdog] worker W (developer) idle 20s without callback', idle_reason=None, source_generation=1, kind='stall')
E             Use -v to get more diff

test/services/test_stalled_callback_watchdog.py:1104: AssertionError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_phase_p_waiting_skips_frame_and_defers_when_phase_a_clears
1 failed, 57 passed in 0.38s
```

### M12

- Patch: `tmp/orch/wpwd-patches/M12.patch`
- Patch SHA-256: `6dc90eb4e25d9a9a4b41ceaa1ec65b236a7987cf6432057df20bc494aff18f0f`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M12.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M12.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..b658f7d 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -899,7 +899,7 @@ class StalledCallbackWatchdog:

         now = time.monotonic()
         notices = self.collect_due_notifications(now=now)
-        jobs = [(notice, self._reserve_chain_notice(notice, now)) for notice in notices]
+        jobs = [(notice, None) for notice in notices]
         chain_pairs = {
             (reservation.notice.terminal_id, reservation.notice.caller_id)
             for _, reservation in jobs
@@ -915,6 +915,7 @@ class StalledCallbackWatchdog:
         for notice, reservation in jobs:
             try:
                 self._persist_notice(notice)
+                reservation = self._reserve_chain_notice(notice, now)
             except Exception:
                 logger.exception("Failed to push stalled-callback watchdog notification")
                 if reservation is not None:
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
_________ test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b _________

    def test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=0, idle_since=0, sampled=True)
        rows = []

        with (
            _relational_watchdog_fakes({"W", "T", "C"}),
            patch.object(svc, "_persist_notice", side_effect=rows.append),
            patch("cli_agent_orchestrator.services.inbox_service.inbox_service") as inbox,
        ):
            svc.notify_due()
            svc.notify_due()
>       assert [notice.kind for notice in rows] == ["stall", "chain"]
E       AssertionError: assert ['waiting', 'stall', 'chain'] == ['stall', 'chain']
E
E         At index 0 diff: 'waiting' != 'stall'
E         Left contains one more item: 'chain'
E         Use -v to get more diff

test/services/test_stalled_callback_watchdog.py:1219: AssertionError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b
1 failed in 0.24s
```

### M13

- Patch: `tmp/orch/wpwd-patches/M13.patch`
- Patch SHA-256: `d3e82da881ace43e8d13caed125b6bb88e7c159d85f4f8c64bc117662ebca78f`
- Target start/restored SHA-256: `cd49bec816f1fc8c8ac62b7ebc9f76b2fdbb537538692caaa97eac8f60287687` (`src/cli_agent_orchestrator/services/mailbox_service.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M13.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M13.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/mailbox_service.py b/src/cli_agent_orchestrator/services/mailbox_service.py
index e637e39..d076183 100644
--- a/src/cli_agent_orchestrator/services/mailbox_service.py
+++ b/src/cli_agent_orchestrator/services/mailbox_service.py
@@ -500,7 +500,6 @@ def create_logical_inbox_message(
                     message=message,
                     orchestration_type=orchestration_type,
                     dispatch_barrier=dispatch_barrier,
-                    park_warm=park_warm,
                 )
                 db.commit()
                 db.refresh(row)
```

```text
$ uv run pytest -q test/services/test_wp_watchdog_production_paths.py::test_http_send_persists_park_warm_through_raw_and_logical_entry[True-True]
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
__ test_http_send_persists_park_warm_through_raw_and_logical_entry[True-True] __

prod_db = sessionmaker(class_='Session', bind=Engine(sqlite:////tmp/pytest-of-chao/pytest-288/test_http_send_persists_park_w0/wpwd.sqlite), autoflush=True, expire_on_commit=False)
logical = True, park_warm = True

    @pytest.mark.parametrize("park_warm", [False, True])
    @pytest.mark.parametrize("logical", [False, True])
    def test_http_send_persists_park_warm_through_raw_and_logical_entry(prod_db, logical, park_warm):
        with prod_db.begin() as db:
            _terminal(db, "abcdef01")
            if logical:
                db.add(
                    MailboxModel(
                        id="mb_abcdef01",
                        session_name="wpwd",
                        role="supervisor",
                        current_terminal_id="abcdef01",
                        generation=1,
                        consumed_through_id=0,
                        created_at=datetime.now(),
                        updated_at=datetime.now(),
                    )
                )

        client = TestClient(app)
        with (
            patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value={
                "id": "abcdef01", "tmux_session": "wpwd", "tmux_window": "abcdef01",
            }),
            patch("cli_agent_orchestrator.api.main.require_input_allowed"),
            patch("cli_agent_orchestrator.api.main.get_backend") as backend,
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
            patch("cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed"),
        ):
            backend.return_value.session_exists.return_value = True
            backend.return_value.get_history.return_value = ""
            response = client.post(
                f"/terminals/{'mb_abcdef01' if logical else 'abcdef01'}/inbox/messages",
                params={"sender_id": "sender", "message": "wire", "park_warm": park_warm},
                headers={"Host": "localhost"},
            )
        assert response.status_code == 200, response.text
        with prod_db() as db:
            row = db.query(InboxModel).order_by(InboxModel.id.desc()).first()
>           assert row is not None and bool(row.park_warm) is park_warm
E           assert (<cli_agent_orchestrator.clients.database.InboxModel object at 0x7f204b746990> is not None and False is True)
E            +  where False = bool(False)
E            +    where False = <cli_agent_orchestrator.clients.database.InboxModel object at 0x7f204b746990>.park_warm

test/services/test_wp_watchdog_production_paths.py:98: AssertionError
------------------------------ Captured log call -------------------------------
WARNING  cli_agent_orchestrator.api.main:main.py:3254 Immediate delivery attempt failed for abcdef01: 'State' object has no attribute 'plugin_registry'
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.venv/lib/python3.13/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323
.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323
  /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.11/migration/
    warnings.warn(DEPRECATION_MESSAGE, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED test/services/test_wp_watchdog_production_paths.py::test_http_send_persists_park_warm_through_raw_and_logical_entry[True-True]
1 failed, 3 warnings in 0.77s
```

### M14

- Patch: `tmp/orch/wpwd-patches/M14.patch`
- Patch SHA-256: `c45fd832dc9e979097402965b3ab4ab0799cfe5cfe97cc010e6655d1fa6895a5`
- Target start/restored SHA-256: `f8be540c14b393441e18876c19ed40db471ec90d2396838360928725fbb3b1c4` (`src/cli_agent_orchestrator/services/inbox_service.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M14.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M14.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/inbox_service.py b/src/cli_agent_orchestrator/services/inbox_service.py
index f310a4a..418b1ab 100644
--- a/src/cli_agent_orchestrator/services/inbox_service.py
+++ b/src/cli_agent_orchestrator/services/inbox_service.py
@@ -2377,7 +2377,7 @@ class InboxService:
                         attempt["sender_id"],
                         OrchestrationType(attempt["orchestration_type"]),
                         metadata,
-                        get_park_warm_for_message_ids(message_ids),
+                        False,
                     )
                 return
             recovered_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
@@ -2472,7 +2472,7 @@ class InboxService:
                             attempt["sender_id"],
                             OrchestrationType(attempt["orchestration_type"]),
                             metadata,
-                            get_park_warm_for_message_ids(message_ids),
+                            False,
                         ),
                     ),
                 )
```

```text
$ uv run pytest -q test/services/test_wp_watchdog_production_paths.py::test_recovery_reads_persisted_member_park_warm[True]
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
_____________ test_recovery_reads_persisted_member_park_warm[True] _____________

prod_db = sessionmaker(class_='Session', bind=Engine(sqlite:////tmp/pytest-of-chao/pytest-289/test_recovery_reads_persisted_0/wpwd.sqlite), autoflush=True, expire_on_commit=False)
park_warm = True

    @pytest.mark.parametrize("park_warm", [False, True])
    def test_recovery_reads_persisted_member_park_warm(prod_db, park_warm):
        with prod_db.begin() as db:
            _terminal(db, "receiver")
            row = InboxModel(
                sender_id="sender", receiver_id="receiver", message="stale",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.DELIVERING.value, park_warm=park_warm, created_at=datetime.now(),
            )
            db.add(row)
            db.flush()
            message_id = row.id

        module = __import__("cli_agent_orchestrator.services.inbox_service", fromlist=["x"])
        with prod_db() as db:
            message = database._inbox_message_from_row(db.get(InboxModel, message_id))
        with (
            patch.object(module, "list_stale_delivering_messages", return_value=[message]),
            patch.object(module, "get_message_trace", return_value={"attempts": [{
                "attempt_uuid": "a", "payload_hash": "h", "started_at": None,
                "evidence": {}, "sender_id": "sender", "orchestration_type": "send_message",
            }]}),
            patch.object(module, "list_attempt_member_ids", return_value=[message_id]),
            patch.object(module, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}),
            patch.object(module, "resolve_session_transcript", return_value=Path("/trace")),
            patch.object(module, "transcript_lookup", return_value=("hit", {})),
            patch("cli_agent_orchestrator.backends.registry.get_backend") as backend,
            patch.object(module, "settle_delivery_attempt", side_effect=lambda *a, **kw: kw["on_confirmed"]()),
            patch.object(InboxService, "_commit_watchdog_ops") as commit,
        ):
            backend.return_value.get_history.return_value = ""
            InboxService().recover_stale_deliveries()
>       assert commit.call_args.args[-1] is park_warm
E       assert False is True

test/services/test_wp_watchdog_production_paths.py:176: AssertionError
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.venv/lib/python3.13/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323
.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323
  /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.venv/lib/python3.13/site-packages/pydantic/_internal/_config.py:323: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.11/migration/
    warnings.warn(DEPRECATION_MESSAGE, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED test/services/test_wp_watchdog_production_paths.py::test_recovery_reads_persisted_member_park_warm[True]
1 failed, 3 warnings in 0.67s
```

### M15

- Patch: `tmp/orch/wpwd-patches/M15.patch`
- Patch SHA-256: `7009130147e707770e51d78c85cef5c1b685735bf296a0f49478de7a4b5cca81`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M15.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M15.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..fd09f76 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -832,10 +832,7 @@ class StalledCallbackWatchdog:
             return None
         with self._lock:
             target_episode = self._episodes.get(notice.terminal_id)
-            if (
-                target_episode is None
-                or target_episode.generation != notice.source_generation
-            ):
+            if target_episode is None:
                 return None
             worker_id = target_episode.caller_id
             worker_episode = self._episodes.get(worker_id)
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_trigger_a_rollover_is_stale_and_insert_failure_rolls_back_reservation
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
__ test_trigger_a_rollover_is_stale_and_insert_failure_rolls_back_reservation __

    def test_trigger_a_rollover_is_stale_and_insert_failure_rolls_back_reservation():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=0, idle_since=0, sampled=True)
        with _relational_watchdog_fakes({"W", "T", "C"}):
            original_collect = svc.collect_due_notifications

            def collect_and_rearm(*, now=None):
                result = original_collect(now=now)
                svc._episodes["T"].generation += 1
                return result

            with (
                patch.object(svc, "collect_due_notifications", side_effect=collect_and_rearm),
                patch.object(svc, "_persist_notice") as persist,
                patch("cli_agent_orchestrator.services.inbox_service.inbox_service"),
            ):
                svc.notify_due()
>           assert [call.args[0].kind for call in persist.call_args_list] == ["waiting", "stall"]
E           AssertionError: assert ['stall', 'chain'] == ['waiting', 'stall']
E
E             At index 0 diff: 'stall' != 'waiting'
E             Use -v to get more diff

test/services/test_stalled_callback_watchdog.py:1242: AssertionError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_trigger_a_rollover_is_stale_and_insert_failure_rolls_back_reservation
1 failed in 0.27s
```

### M16

- Patch: `tmp/orch/wpwd-patches/M16.patch`
- Patch SHA-256: `0023f1cc9bdca56a781339bc0e87ee4b4e43daf2120186b9bbcd9b3e408e850d`
- Target start/restored SHA-256: `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` (`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`)
- Replay: `git apply tmp/orch/wpwd-patches/M16.patch`
- Restore: `git apply -R tmp/orch/wpwd-patches/M16.patch`

```diff
diff --git a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
index 9bbccb1..1920d8d 100644
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -905,13 +905,6 @@ class StalledCallbackWatchdog:
             for _, reservation in jobs
             if reservation is not None
         }
-        jobs = [
-            (notice, reservation)
-            for notice, reservation in jobs
-            if notice.kind != "waiting"
-            or (notice.terminal_id, notice.caller_id) not in chain_pairs
-        ]
-
         for notice, reservation in jobs:
             try:
                 self._persist_notice(notice)
```

```text
$ uv run pytest -q test/services/test_stalled_callback_watchdog.py::test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b
EXIT_CODE=1
F                                                                        [100%]
=================================== FAILURES ===================================
_________ test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b _________

    def test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b():
        svc = StalledCallbackWatchdog(grace_seconds=3)
        _arm_watchdog_episode(svc, "W", "C", inbound_at=0, idle_since=0, sampled=True)
        _arm_watchdog_episode(svc, "T", "W", inbound_at=0, idle_since=0, sampled=True)
        rows = []

        with (
            _relational_watchdog_fakes({"W", "T", "C"}),
            patch.object(svc, "_persist_notice", side_effect=rows.append),
            patch("cli_agent_orchestrator.services.inbox_service.inbox_service") as inbox,
        ):
            svc.notify_due()
            svc.notify_due()
>       assert [notice.kind for notice in rows] == ["stall", "chain"]
E       AssertionError: assert ['waiting', 'stall', 'chain'] == ['stall', 'chain']
E
E         At index 0 diff: 'waiting' != 'stall'
E         Left contains one more item: 'chain'
E         Use -v to get more diff

test/services/test_stalled_callback_watchdog.py:1219: AssertionError
=========================== short test summary info ============================
FAILED test/services/test_stalled_callback_watchdog.py::test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b
1 failed in 0.22s
```
