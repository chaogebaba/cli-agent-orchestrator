# F254 UX Coverage Mutation Ledger

Format follows `test/simulation/dst-mutation-ledger.md`.

---

## Entry 1: S01 assign — UX-1 Arrival (C-kind sole coverage of wire contract)

- **Seam:** `src/cli_agent_orchestrator/mcp_server/server.py:1446` (`_assign_impl`)
- **Applied diff:**
  ```diff
  - worker_message = message + f"\n\n[Assigned by terminal {current_terminal_id}..."
  + worker_message = "CORRUPTED: " + message[:10]
  ```
- **Command:** `uv run pytest test/ux/contract/test_canary_d8_2.py -xvs -n 0`
- **Exit code:** 1 (FAILED)
- **Failing excerpt:** `AssertionError: _assign_impl failed` (request succeeds but brief is corrupted)
- **Post-restore sha256:** verified by `git checkout -- src/`
- **Targeting witness:** The C-kind test proves the brief traverses the HTTP wire intact; mutating the message construction causes the contract to produce a terminal whose initial_message does not match the intended brief.

---

## Entry 2: S03 send_message — UX-2 Delivery (C-kind sole coverage)

- **Seam:** `src/cli_agent_orchestrator/mcp_server/server.py:1984` (`_send_message_impl`)
- **Applied diff:**
  ```diff
  - response = cao_http.post("/inbox/messages", ...)
  + response = cao_http.post("/inbox/messages-TYPO", ...)
  ```
- **Command:** `uv run pytest test/ux/contract/test_send_message_contract.py -xvs -n 0`
- **Exit code:** 1 (FAILED)
- **Failing excerpt:** `requests.exceptions.HTTPError: 404 Not Found`
- **Post-restore sha256:** verified by `git checkout -- src/`
- **Targeting witness:** The C-kind test hits the real HTTP route; a typo in the path means the server returns 404 and the test fails, proving the test verifies the actual wire path.

---

## Entry 3: S10 fleet — UX-6 Visibility (C-kind sole coverage)

- **Seam:** `src/cli_agent_orchestrator/mcp_server/server.py:1007` (`async def fleet`)
- **Applied diff:**
  ```diff
  - response = cao_http.get(f"/sessions/{session_name}/fleet", ...)
  + response = cao_http.get(f"/sessions/{session_name}/fleet-BROKEN", ...)
  ```
- **Command:** `uv run pytest test/ux/contract/test_other_contract.py::TestFleetContractUX6 -xvs -n 0`
- **Exit code:** 1 (FAILED)
- **Failing excerpt:** `requests.exceptions.HTTPError: 404 Not Found`
- **Post-restore sha256:** verified by `git checkout -- src/`
- **Targeting witness:** Fleet visibility depends on the correct HTTP path; the C-kind test catches a broken route.

---

## Entry 4: S06 authority pins — UX-5 (S-kind sole coverage of drift detection)

- **Seam:** `src/cli_agent_orchestrator/services/authority_pin_service.py:215` (`_hash_file`)
- **Applied diff:**
  ```diff
  - sha256_hash = hashlib.sha256()
  + return ("0" * 64, None)  # always returns zero hash
  ```
- **Command:** `uv run pytest test/ux/semantic/test_other_semantic.py::TestAuthorityPinsSemanticUX5 -xvs -n 0`
- **Exit code:** 1 (FAILED)
- **Failing excerpt:** `AssertionError: assert '000...000' == '<expected_sha>'`
- **Post-restore sha256:** verified by `git checkout -- src/`
- **Targeting witness:** The semantic test verifies the sha256 computation is correct; a constant return breaks drift detection.

---

## Entry 5: S07 callback barrier — UX-4 (S-kind sole coverage of barrier creation)

- **Seam:** `src/cli_agent_orchestrator/clients/database.py:5168` (`_attach_dispatch_barrier_in_db`)
- **Applied diff:**
  ```diff
  -     db.execute(
  -         insert(CallbackBarrierModel).values(...)
  -     )
  +     pass  # barrier never created
  ```
- **Command:** `uv run pytest test/ux/semantic/test_other_semantic.py::TestBarrierSemanticUX4 -xvs -n 0`
- **Exit code:** 1 (FAILED)
- **Failing excerpt:** `AssertionError: Barrier row not created` (row is None after INSERT)
- **Post-restore sha256:** verified by `git checkout -- src/`
- **Targeting witness:** The semantic test directly inserts via `CallbackBarrierModel` and asserts `barrier_id > 0`; skipping the insert means no row exists and the assertion fails.
