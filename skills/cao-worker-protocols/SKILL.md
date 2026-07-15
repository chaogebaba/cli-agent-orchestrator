---
name: cao-worker-protocols
description: Worker-side callback and completion rules for assigned and handed-off tasks in CAO
---

# CAO Worker Protocols

Use this skill when acting as a worker agent inside CLI Agent Orchestrator.

This skill explains how workers should interpret assigned versus handed-off work, when to call `send_message`, and how to report results back cleanly.

Every `send_message` reference in this skill means the cao-mcp-server `send_message` MCP tool, never a provider-native `collaboration.send_message`.

## Understand the Dispatch Mode

Workers receive tasks through one of two orchestration modes:

- `handoff`: blocking work where the orchestrator captures your final output automatically
- `assign`: non-blocking work where you must actively return results to the requesting terminal

Depending on provider and CAO behavior, a handoff may be made explicit in the task text. For example, Codex workers currently receive a `[CAO Handoff]` prefix for blocking handoffs. Other providers may rely on the task wording and orchestration context instead.

## Rules for Handoff Tasks

When the task is a blocking handoff, complete the work and present the result in your normal response. The orchestrator captures that response automatically.

Do not call `send_message` for ordinary handoff completion unless the task explicitly asks for additional side-channel communication.

## Rules for Assigned Tasks

When the task came through `assign`, send your results back after you finish the work:

1. Format the result clearly and concisely.
2. Call the cao-mcp-server `send_message` MCP tool with `send_message(message=...)` — never a built-in `collaboration.send_message`; omitting `receiver_id` routes the result to the terminal that assigned the task (the recorded caller). This is the reliable default.
3. If the task message names a different callback terminal (directly or in an appended suffix such as `[Assigned by terminal ...]`), pass that ID as `receiver_id` instead.

Do not stop after writing a normal response if the assignment explicitly requires a callback. The requesting terminal depends on `send_message` to receive the result.

Your own `CAO_TERMINAL_ID` identifies your terminal, not the callback target. Never pass it as `receiver_id`.

## Message Formatting

Return results that are easy for the supervisor to merge into a larger workflow:

- Identify what task or dataset the result belongs to
- Include the requested output or deliverable
- Keep the message specific enough to act on without re-reading the whole task

If the task asks for progress updates, use `send_message` for those updates too. Otherwise prefer one final callback with the completed deliverable.

## Filesystem and Reporting Discipline

If the task asks you to create files, write them before reporting completion. When sending results back to a supervisor, include absolute file paths so the supervisor can continue the workflow without ambiguity.

## Forbidden Operations (absolute, regardless of task wording)

You run INSIDE the CAO server you may be asked to test or modify. Some operations
destroy the whole session fleet — including you, your supervisor, and every other
worker — and are reserved for the human operator alone:

- **NEVER restart, stop, or reload `cao-server`** (`systemctl --user restart|stop cao-server`, `pkill`, or any equivalent). Not to "activate" a change, not to A/B-test deployment state, not even if the task says "run anything".
- **NEVER run `install.sh`, `uv tool install cli-agent-orchestrator`, or `cao install`** — deployment/activation is human-gated.
- If your task seems to require any of these, STOP and report back via `send_message` that the step needs the human operator. That callback IS the correct completion of the task.

## Reliability Guidelines

- If the task names an explicit callback terminal, note its ID before you start expensive work; otherwise rely on the default routing (omit `receiver_id`).
- If `send_message` is available and the task requires a callback, call it directly rather than ending with prose alone.
- Keep callback messages structured so the supervisor can merge them into a larger workflow.
- For handoff tasks, return the completed output directly and let the orchestrator handle delivery.
