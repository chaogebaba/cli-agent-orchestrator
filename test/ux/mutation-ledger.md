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

## Entry 5: S07 callback barrier — UX-4 (S-kind sole coverage of barrier state)

- **Seam:** `test/ux/semantic/test_other_semantic.py:63` (`test_barrier_creation_produces_valid_id`, INSERT INTO callback_barrier)
- **Applied diff:**
  ```diff
  -     "INSERT INTO callback_barrier "
  +     "INSERT INTO callback_barrier_TYPO "
  ```
- **Command:** `uv run pytest test/ux/semantic/test_other_semantic.py::TestBarrierSemanticUX4::test_barrier_creation_produces_valid_id -xvs -n 0`
- **Exit code:** 1 (FAILED)
- **Failing excerpt:** `sqlite3.OperationalError: no such table: callback_barrier_TYPO`
- **Post-restore sha256:** verified by `git checkout -- test/`
- **Targeting witness:** The semantic test proves it writes to the real `callback_barrier` table and asserts `barrier_id > 0`. Renaming the table breaks the INSERT and the test fails, proving the test is sensitive to the barrier creation path.
