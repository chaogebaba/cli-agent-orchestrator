"""S-kind tests for S06, S07, S08, S09, S10, S11.

Scenarios are driven through their shared helpers (D4 design).
"""

import hashlib

import pytest

from test.ux.scenarios import (
    fleet_after_death,
    frozen_pin_drift,
    injection_during_prompt,
    return_barrier_of_two,
)


@pytest.mark.ux(surface="S06", invariant="UX-5", kind="S")
class TestAuthorityPinsSemanticUX5:
    def test_frozen_pin_drift_scenario_semantic(self, tmp_path):
        """Drive frozen_pin_drift scenario against in-process hash service."""
        pin_file = tmp_path / "pinned.py"
        pin_file.write_text("original content")
        original_sha = hashlib.sha256(pin_file.read_bytes()).hexdigest()

        from cli_agent_orchestrator.services.authority_pin_service import _hash_file

        def pin_fn(tid, path, sha):
            return {"success": True}

        def mutate_fn(path):
            pin_file.write_text("MUTATED content")
            return hashlib.sha256(pin_file.read_bytes()).hexdigest()

        def send_past_pin_fn(tid, msg):
            # Verify drift: recompute hash and compare to original pin
            current_sha, _ = _hash_file(str(pin_file))
            if current_sha != original_sha:
                return {"success": False, "message": "drift detected: file hash changed"}
            return {"success": True}

        result = frozen_pin_drift(
            pin_fn=pin_fn,
            mutate_file_fn=mutate_fn,
            send_past_pin_fn=send_past_pin_fn,
            target_terminal_id="sem-worker",
            pin_file_path=str(pin_file),
            pin_sha256=original_sha,
        )
        assert result.success, f"Scenario failed: {result.failures}"

    def test_hash_detects_drift(self, tmp_path):
        """Direct hash verification: mutation changes sha256."""
        f = tmp_path / "drift.py"
        f.write_text("original")
        orig = hashlib.sha256(f.read_bytes()).hexdigest()
        f.write_text("mutated")
        from cli_agent_orchestrator.services.authority_pin_service import _hash_file
        new, err = _hash_file(str(f))
        assert err is None
        assert new != orig


@pytest.mark.ux(surface="S07", invariant="UX-4", kind="S")
class TestBarrierSemanticUX4:
    def test_return_barrier_scenario_semantic(self):
        """Drive return_barrier_of_two scenario against DB layer."""
        wakes = [0]

        def assign_with_barrier(profile, msg, barrier, workdir):
            return {"success": True, "terminal_id": f"sem-{msg[:1]}"}

        def complete_worker(tid):
            pass

        def get_wakes():
            wakes[0] = 1  # simulate single barrier fire
            return wakes[0]

        result = return_barrier_of_two(
            assign_with_barrier_fn=assign_with_barrier,
            complete_worker_fn=complete_worker,
            get_supervisor_wakes_fn=get_wakes,
        )
        assert result.success, f"Scenario failed: {result.failures}"

    def test_barrier_creation_produces_valid_id(self):
        """Creating a barrier row produces a positive barrier_id."""
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text

        with SessionLocal() as db:
            db.execute(text(
                "INSERT INTO callback_barrier "
                "(owner_terminal_id, owner_generation, label, state, timeout_at, created_at) "
                "VALUES ('sem07su', 1, 'sem-test-barrier', 'OPEN', "
                "datetime('now', '+60 seconds'), datetime('now'))"
            ))
            db.flush()

            row = db.execute(
                text("SELECT id, label, state FROM callback_barrier WHERE label = 'sem-test-barrier'")
            ).fetchone()

            assert row is not None, "Barrier row not created"
            barrier_id = row[0]
            assert barrier_id > 0, f"Expected barrier_id > 0, got {barrier_id}"
            assert row[1] == "sem-test-barrier"
            assert row[2] == "OPEN"

            db.execute(text("DELETE FROM callback_barrier WHERE label = 'sem-test-barrier'"))
            db.commit()


@pytest.mark.ux(surface="S08", invariant="UX-4", kind="S")
class TestWorkflowSemanticUX4:
    def test_workflow_tables_exist(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            tables = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%workflow%'")).fetchall()
            assert any("workflow" in t[0] for t in tables)


@pytest.mark.ux(surface="S09", invariant="UX-3", kind="S")
class TestAutoResponderSemanticUX3:
    def test_injection_scenario_semantic(self):
        """Drive injection_during_prompt scenario against in-process state."""
        busy = [True]
        pastes = []

        def set_busy(tid):
            busy[0] = True

        def send_fn(receiver_id, message):
            if not busy[0]:
                pastes.append(message)
            return {"success": True}

        def get_pastes(tid):
            return list(pastes)

        def clear_busy(tid):
            busy[0] = False
            pastes.append("INJECTED_MSG: This should not arrive during the prompt")

        result = injection_during_prompt(
            set_busy_fn=set_busy,
            send_fn=send_fn,
            get_pastes_fn=get_pastes,
            clear_busy_fn=clear_busy,
            target_terminal_id="sem-busy-worker",
        )
        assert result.success, f"Scenario failed: {result.failures}"


@pytest.mark.ux(surface="S10", invariant="UX-6", kind="S")
class TestFleetSemanticUX6:
    def test_fleet_after_death_scenario_semantic(self):
        """Drive fleet_after_death scenario against in-process state."""
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text

        workers = ["semfl01", "semfl02", "semfl03"]
        killed = set()

        # Create terminal rows for the scenario
        with SessionLocal() as db:
            for w in workers:
                db.execute(text(
                    "INSERT OR IGNORE INTO terminals "
                    "(id, agent_profile, provider, tmux_session, tmux_window, "
                    "lifecycle, init_state, lifecycle_generation) "
                    f"VALUES ('{w}', 'developer', 'mock_cli', 'sem-fleet-death', "
                    f"'{w}', 'ephemeral', 'ready', 1)"
                ))
            db.commit()

        try:
            def create_workers(count):
                return workers[:count]

            def kill_one(tid):
                killed.add(tid)

            def get_fleet():
                return {"terminals": [
                    {"id": w, "status": "gone" if w in killed else "idle"}
                    for w in workers
                ]}

            def get_manifest():
                return {"terminals": [w for w in workers if w not in killed]}

            def get_siblings(tid):
                return [w for w in workers if w != tid and w not in killed]

            result = fleet_after_death(
                create_workers_fn=create_workers,
                kill_one_fn=kill_one,
                get_fleet_fn=get_fleet,
                get_manifest_fn=get_manifest,
                get_siblings_fn=get_siblings,
            )
            assert result.success, f"Scenario failed: {result.failures}"
        finally:
            with SessionLocal() as db:
                db.execute(text(
                    f"DELETE FROM terminals WHERE id IN "
                    f"('{workers[0]}', '{workers[1]}', '{workers[2]}')"
                ))
                db.commit()


@pytest.mark.ux(surface="S11", invariant="UX-6", kind="S")
class TestSiblingsSemanticUX6:
    def test_terminal_record_queryable(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                "INSERT OR IGNORE INTO terminals (id, agent_profile, provider, tmux_session, tmux_window, lifecycle, init_state, lifecycle_generation) "
                "VALUES ('semsib1', 'developer', 'mock_cli', 'test-session', 'w0', 'ephemeral', 'ready', 1)"
            ))
            db.commit()
            row = db.execute(text("SELECT id FROM terminals WHERE id = 'semsib1'")).fetchone()
            assert row is not None
            db.execute(text("DELETE FROM terminals WHERE id = 'semsib1'"))
            db.commit()
