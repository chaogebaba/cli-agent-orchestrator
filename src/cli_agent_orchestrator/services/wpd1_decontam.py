"""WPD1 provider glue for transcript decontamination and incident audit."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, cast

from cli_agent_orchestrator.constants import LOG_DIR
from cli_agent_orchestrator.providers.codex import (
    CONTENT_POLICY_ARTIFACT_RULE_ID,
    CONTENT_POLICY_REFUSAL_PATTERN,
    CONTENT_POLICY_SCREEN_RULE_ID,
)
from cli_agent_orchestrator.transcript_scrub import (
    NEUTRAL_REPLACEMENT_POLICY,
    ScrubRejected,
    ScrubResult,
    Span,
    rewrite_jsonl,
)
from cli_agent_orchestrator.utils.provider_plane import provider_home

SCHEMA_VERSION_PREFIX = "0.144."
CLASSIFIED_REASON = "content_policy_refusal"
RECOVERY_NUDGE_MESSAGE = (
    "Continue from the recovered conversation state and complete the interrupted task."
)
SCREEN_TO_ARTIFACT_RULE = {
    CONTENT_POLICY_SCREEN_RULE_ID: CONTENT_POLICY_ARTIFACT_RULE_ID,
}
FINAL_STAGES = frozenset(
    {"backup", "scrub", "validate", "install", "resume", "settle", "complete"}
)
NUDGE_STATUSES = frozenset({"sent", "skipped", "failed", "not_attempted"})
NUDGE_SKIP_REASONS = frozenset({"no-nudge-flag", "caller-unresolvable"})
_RULE_ID_RE = re.compile(r"^[a-z0-9.\-]+$")

FieldSpec = Mapping[str, frozenset[str]]


def _spec(
    required: Mapping[str, Sequence[str]],
    optional: Mapping[str, Sequence[str]] | None = None,
):
    return {
        "required": {key: frozenset(types) for key, types in required.items()},
        "optional": {key: frozenset(types) for key, types in (optional or {}).items()},
    }


# Frozen transcription of blueprints/wpd1-annex-schema-0144.md.  Required and
# observed-optional fields are data; validators must not infer new shapes.
SCHEMA_0144: dict[str, dict[str | None, dict[str, dict[str, frozenset[str]]]]] = {
    "compacted": {
        None: _spec(
            {
                "first_window_id": ["string"],
                "message": ["string"],
                "previous_window_id": ["string"],
                "replacement_history": ["array"],
                "window_id": ["string"],
                "window_number": ["integer"],
            }
        )
    },
    "event_msg": {
        "agent_message": _spec(
            {
                "memory_citation": ["null"],
                "message": ["string"],
                "phase": ["null", "string"],
                "type": ["string"],
            }
        ),
        "context_compacted": _spec({"type": ["string"]}),
        "entered_review_mode": _spec(
            {
                "item_id": ["string"],
                "target": ["object"],
                "turn_id": ["string"],
                "type": ["string"],
                "user_facing_hint": ["string"],
            }
        ),
        "exited_review_mode": _spec(
            {
                "item_id": ["string"],
                "review_output": ["object"],
                "turn_id": ["string"],
                "type": ["string"],
            }
        ),
        "mcp_tool_call_end": _spec(
            {
                "call_id": ["string"],
                "duration": ["object"],
                "invocation": ["object"],
                "result": ["object"],
                "type": ["string"],
            }
        ),
        "patch_apply_end": _spec(
            {
                "call_id": ["string"],
                "changes": ["object"],
                "status": ["string"],
                "stderr": ["string"],
                "stdout": ["string"],
                "success": ["bool"],
                "turn_id": ["string"],
                "type": ["string"],
            }
        ),
        "sub_agent_activity": _spec(
            {
                "agent_path": ["string"],
                "agent_thread_id": ["string"],
                "event_id": ["string"],
                "kind": ["string"],
                "occurred_at_ms": ["integer"],
                "type": ["string"],
            }
        ),
        "task_complete": _spec(
            {
                "completed_at": ["integer"],
                "duration_ms": ["integer"],
                "last_agent_message": ["null", "string"],
                "turn_id": ["string"],
                "type": ["string"],
            },
            {"time_to_first_token_ms": ["integer"]},
        ),
        "task_started": _spec(
            {
                "collaboration_mode_kind": ["string"],
                "model_context_window": ["integer"],
                "started_at": ["integer"],
                "turn_id": ["string"],
                "type": ["string"],
            }
        ),
        "thread_rolled_back": _spec({"num_turns": ["integer"], "type": ["string"]}),
        "thread_settings_applied": _spec(
            {"thread_settings": ["object"], "type": ["string"]}
        ),
        "token_count": _spec(
            {
                "info": ["null", "object"],
                "rate_limits": ["null", "object"],
                "type": ["string"],
            }
        ),
        "turn_aborted": _spec(
            {"reason": ["string"], "turn_id": ["string"], "type": ["string"]},
            {"completed_at": ["integer"], "duration_ms": ["integer"]},
        ),
        "user_message": _spec(
            {
                "images": ["array"],
                "local_images": ["array"],
                "message": ["string"],
                "text_elements": ["array"],
                "type": ["string"],
            }
        ),
        "web_search_end": _spec(
            {
                "action": ["object"],
                "call_id": ["string"],
                "query": ["string"],
                "type": ["string"],
            }
        ),
    },
    "inter_agent_communication_metadata": {
        None: _spec({"trigger_turn": ["bool"]})
    },
    "response_item": {
        "agent_message": _spec(
            {
                "author": ["string"],
                "content": ["array"],
                "internal_chat_message_metadata_passthrough": ["object"],
                "recipient": ["string"],
                "type": ["string"],
            }
        ),
        "custom_tool_call": _spec(
            {
                "call_id": ["string"],
                "id": ["string"],
                "input": ["string"],
                "internal_chat_message_metadata_passthrough": ["object"],
                "name": ["string"],
                "status": ["string"],
                "type": ["string"],
            },
            {"namespace": ["string"]},
        ),
        "custom_tool_call_output": _spec(
            {
                "call_id": ["string"],
                "internal_chat_message_metadata_passthrough": ["object"],
                "output": ["array", "string"],
                "type": ["string"],
            }
        ),
        "function_call": _spec(
            {
                "arguments": ["string"],
                "call_id": ["string"],
                "id": ["string"],
                "internal_chat_message_metadata_passthrough": ["object"],
                "name": ["string"],
                "type": ["string"],
            },
            {"namespace": ["string"]},
        ),
        "function_call_output": _spec(
            {
                "call_id": ["string"],
                "internal_chat_message_metadata_passthrough": ["object"],
                "output": ["array", "string"],
                "type": ["string"],
            }
        ),
        "message": _spec(
            {"content": ["array"], "role": ["string"], "type": ["string"]},
            {
                "id": ["string"],
                "internal_chat_message_metadata_passthrough": ["object"],
                "phase": ["string"],
            },
        ),
        "reasoning": _spec(
            {
                "encrypted_content": ["null", "string"],
                "id": ["string"],
                "internal_chat_message_metadata_passthrough": ["object"],
                "summary": ["array"],
                "type": ["string"],
            },
            {"content": ["null"]},
        ),
    },
    "session_meta": {
        None: _spec(
            {
                "base_instructions": ["object"],
                "cli_version": ["string"],
                "context_window": ["object"],
                "cwd": ["string"],
                "history_mode": ["string"],
                "id": ["string"],
                "model_provider": ["string"],
                "originator": ["string"],
                "session_id": ["string"],
                "source": ["object", "string"],
                "thread_source": ["string"],
                "timestamp": ["string"],
            },
            {
                "agent_nickname": ["string"],
                "agent_path": ["string"],
                "forked_from_id": ["string"],
                "git": ["object"],
                "multi_agent_version": ["string"],
                "parent_thread_id": ["string"],
            },
        )
    },
    "turn_context": {
        None: _spec(
            {
                "approval_policy": ["string"],
                "approvals_reviewer": ["string"],
                "collaboration_mode": ["object"],
                "current_date": ["string"],
                "cwd": ["string"],
                "model": ["string"],
                "multi_agent_version": ["string"],
                "permission_profile": ["object"],
                "personality": ["string"],
                "realtime_active": ["bool"],
                "sandbox_policy": ["object"],
                "summary": ["string"],
                "timezone": ["string"],
                "turn_id": ["string"],
                "workspace_roots": ["array"],
            },
            {
                "comp_hash": ["string"],
                "effort": ["string"],
                "file_system_sandbox_policy": ["object"],
                "multi_agent_mode": ["string"],
            },
        )
    },
    "world_state": {None: _spec({"full": ["bool"], "state": ["object"]})},
}


class DecontaminationError(RuntimeError):
    """Stable, stage-labelled content recovery failure."""

    def __init__(self, code: str, stage: str) -> None:
        self.code = code
        self.stage = stage
        super().__init__(f"{stage}:{code}")


class RepeatedIncident(DecontaminationError):
    def __init__(self, incident_path: Path) -> None:
        self.incident_path = incident_path
        super().__init__(f"prior_successful_incident:{incident_path}", "backup")


@dataclass(frozen=True)
class ArtifactIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class ArtifactLease:
    session_uuid: str
    generation: int


@dataclass
class PreparedRecovery:
    terminal_id: str
    lifecycle_generation: int
    session_uuid: str
    artifact_path: Path
    backup_path: Path
    incident_path: Path
    attempt_index: int
    installed_identity: ArtifactIdentity
    scrub_result: ScrubResult
    lease: ArtifactLease
    span_detail: list[dict[str, Any]]


@dataclass(frozen=True)
class CpaDiscoveryResult:
    spans: tuple[Span, ...]
    dropped: int
    human_gate_required: bool
    unavailable: bool


_artifact_lease_lock = threading.Lock()
_artifact_leases: dict[str, int] = {}
_artifact_lease_generation = 0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def screen_rule_id_for_reason(reason: str | None) -> str | None:
    return CONTENT_POLICY_SCREEN_RULE_ID if reason == CLASSIFIED_REASON else None


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def validate_record(record: Any) -> None:
    if not isinstance(record, dict):
        raise DecontaminationError("record_not_object", "validate")
    if set(("timestamp", "type", "payload")) - record.keys():
        raise DecontaminationError("envelope_required_field_missing", "validate")
    if not isinstance(record["timestamp"], str) or not isinstance(record["type"], str):
        raise DecontaminationError("envelope_field_type_invalid", "validate")
    payload = record["payload"]
    if not isinstance(payload, dict):
        raise DecontaminationError("payload_not_object", "validate")
    family = record["type"]
    family_table = SCHEMA_0144.get(family)
    if family_table is None:
        raise DecontaminationError("unknown_top_level_family", "validate")
    variant: str | None = None
    if family in {"event_msg", "response_item"}:
        raw_variant = payload.get("type")
        if not isinstance(raw_variant, str):
            raise DecontaminationError("nested_discriminator_missing", "validate")
        variant = raw_variant
    shape = family_table.get(variant)
    if shape is None:
        raise DecontaminationError("unknown_nested_discriminator", "validate")
    required = shape["required"]
    optional = shape["optional"]
    missing = set(required) - payload.keys()
    if missing:
        raise DecontaminationError("required_payload_field_missing", "validate")
    extra = set(payload) - set(required) - set(optional)
    if extra:
        raise DecontaminationError("unknown_payload_field", "validate")
    for field, allowed_types in {**required, **optional}.items():
        if field in payload and _json_type(payload[field]) not in allowed_types:
            raise DecontaminationError("payload_field_type_invalid", "validate")


def validate_artifact_bytes(content: bytes, session_uuid: str, filename: str) -> list[dict]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecontaminationError("artifact_not_utf8", "validate") from exc
    records: list[dict] = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DecontaminationError("artifact_json_invalid", "validate") from exc
        validate_record(record)
        records.append(record)
    if not records:
        raise DecontaminationError("artifact_empty", "validate")
    first = records[0]
    payload = first.get("payload", {})
    if (
        first.get("type") != "session_meta"
        or payload.get("id") != session_uuid
        or session_uuid not in filename
    ):
        raise DecontaminationError("artifact_identity_invalid", "validate")
    version = payload.get("cli_version")
    if not isinstance(version, str) or not version.startswith(SCHEMA_VERSION_PREFIX):
        raise DecontaminationError("artifact_version_unsupported", "validate")
    return records


def validate_artifact(path: Path, session_uuid: str) -> list[dict]:
    return validate_artifact_bytes(_read_regular(path), session_uuid, path.name)


def find_artifact(session_uuid: str) -> Path:
    matches = list(provider_home("codex").sessions.glob(f"**/rollout-*{session_uuid}*.jsonl"))
    if not matches:
        raise DecontaminationError("session_artifact_missing", "backup")
    if len(matches) != 1:
        raise DecontaminationError("session_artifact_ambiguous", "backup")
    _require_regular(matches[0])
    return matches[0]


def acquire_artifact_lease(session_uuid: str) -> ArtifactLease | None:
    global _artifact_lease_generation
    with _artifact_lease_lock:
        if session_uuid in _artifact_leases:
            return None
        _artifact_lease_generation += 1
        _artifact_leases[session_uuid] = _artifact_lease_generation
        return ArtifactLease(session_uuid, _artifact_lease_generation)


def release_artifact_lease(lease: ArtifactLease | None) -> None:
    if lease is None:
        return
    with _artifact_lease_lock:
        if _artifact_leases.get(lease.session_uuid) != lease.generation:
            raise RuntimeError("artifact_lease_generation_mismatch")
        del _artifact_leases[lease.session_uuid]


def _validate_artifact_lease(lease: ArtifactLease) -> None:
    with _artifact_lease_lock:
        if _artifact_leases.get(lease.session_uuid) != lease.generation:
            raise DecontaminationError("artifact_lease_lost", "install")


def _require_regular(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DecontaminationError("artifact_unavailable", "backup") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DecontaminationError("regular_file_required", "backup")
    return info


def _read_regular(path: Path) -> bytes:
    _require_regular(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            return stream.read()
    except OSError as exc:
        raise DecontaminationError("artifact_read_failed", "backup") from exc


def artifact_identity(path: Path) -> ArtifactIdentity:
    info = _require_regular(path)
    content = _read_regular(path)
    return ArtifactIdentity(
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        hashlib.sha256(content).hexdigest(),
    )


def _artifact_pattern() -> re.Pattern[str]:
    prefix = "(?i)^"
    if not CONTENT_POLICY_REFUSAL_PATTERN.startswith(prefix):
        raise RuntimeError("content_screen_rule_shape_changed")
    if CONTENT_POLICY_REFUSAL_PATTERN[len("(?i)") :].count("^") != 1:
        raise RuntimeError("content_screen_rule_anchor_changed")
    return re.compile("(?i)" + CONTENT_POLICY_REFUSAL_PATTERN[len(prefix) :])


def _decoded_candidates(records: Sequence[dict]) -> list[tuple[int, tuple[Any, ...], str]]:
    last_compacted = max(
        (index for index, record in enumerate(records) if record.get("type") == "compacted"),
        default=-1,
    )
    candidates: list[tuple[int, tuple[Any, ...], str]] = []
    for index, record in enumerate(records):
        family = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if family == "compacted":
            if index != last_compacted:
                continue
            message = payload.get("message")
            if isinstance(message, str):
                candidates.append((index + 1, ("payload", "message"), message))
            history = payload.get("replacement_history")
            if isinstance(history, list):
                for history_index, item in enumerate(history):
                    if not isinstance(item, dict) or not isinstance(item.get("content"), list):
                        continue
                    for content_index, content in enumerate(item["content"]):
                        if isinstance(content, dict) and isinstance(content.get("text"), str):
                            candidates.append(
                                (
                                    index + 1,
                                    (
                                        "payload",
                                        "replacement_history",
                                        history_index,
                                        "content",
                                        content_index,
                                        "text",
                                    ),
                                    content["text"],
                                )
                            )
            continue
        if family != "response_item" or index <= last_compacted:
            continue
        variant = payload.get("type")
        if variant == "message" and isinstance(payload.get("content"), list):
            for content_index, content in enumerate(payload["content"]):
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    candidates.append(
                        (
                            index + 1,
                            ("payload", "content", content_index, "text"),
                            content["text"],
                        )
                    )
        elif variant == "custom_tool_call" and isinstance(payload.get("input"), str):
            candidates.append((index + 1, ("payload", "input"), payload["input"]))
        elif variant == "custom_tool_call_output" and isinstance(payload.get("output"), list):
            for output_index, output in enumerate(payload["output"]):
                if isinstance(output, dict) and isinstance(output.get("text"), str):
                    candidates.append(
                        (
                            index + 1,
                            ("payload", "output", output_index, "text"),
                            output["text"],
                        )
                    )
    return candidates


def discover_artifact_spans(records: Sequence[dict]) -> tuple[Span, ...]:
    pattern = _artifact_pattern()
    spans: list[Span] = []
    for line_number, key_path, value in _decoded_candidates(records):
        for match in pattern.finditer(value):
            preimage = value[match.start() : match.end()]
            spans.append(
                Span(
                    line_number=line_number,
                    key_path=key_path,
                    start=match.start(),
                    end=match.end(),
                    replacement_policy=NEUTRAL_REPLACEMENT_POLICY,
                    expected_preimage_sha256=hashlib.sha256(
                        preimage.encode("utf-8")
                    ).hexdigest(),
                    rule_id=CONTENT_POLICY_ARTIFACT_RULE_ID,
                )
            )
            if len(spans) > 64:
                raise DecontaminationError("artifact_match_limit_exceeded", "scrub")
    return tuple(spans)


def _candidate_payload(
    candidates: Sequence[tuple[int, tuple[Any, ...], str]],
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    truncated = len(candidates) > 512
    for line_number, key_path, value in candidates[:512]:
        selected = value[:8192]
        truncated = truncated or len(selected) != len(value)
        rows.append(
            {
                "line_number": line_number,
                "key_path": list(key_path),
                "text": selected,
                "sha256": hashlib.sha256(selected.encode("utf-8")).hexdigest(),
            }
        )
    while rows:
        encoded = json.dumps(
            {"model": "grok-4.5", "candidates": rows},
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) <= 256 * 1024:
            return rows, truncated
        rows.pop()
        truncated = True
    return rows, truncated


def discover_cpa_spans(
    records: Sequence[dict],
    *,
    config_path: Path | None = None,
    timeout: float = 60.0,
) -> CpaDiscoveryResult:
    config_path = config_path or (Path.home() / ".graphify" / "providers.json")
    api_key = os.environ.get("CPA_API_KEY")
    if not api_key or not config_path.is_file():
        return CpaDiscoveryResult((), 0, False, True)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        provider = config.get("cpa") or config.get("providers", {}).get("cpa")
        if not isinstance(provider, dict):
            return CpaDiscoveryResult((), 0, False, True)
        endpoint = provider.get("url") or provider.get("base_url")
        if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
            return CpaDiscoveryResult((), 0, False, True)
        rows, truncated = _candidate_payload(_decoded_candidates(records))
        request_object = {
            "model": provider.get("model", "grok-4.5"),
            "candidates": rows,
            "instruction": (
                "Return exact candidate indexes and decoded code-point spans only; "
                "do not propose replacement text."
            ),
        }
        body = json.dumps(request_object, separators=(",", ":")).encode("utf-8")
        while len(body) > 256 * 1024 and rows:
            rows.pop()
            truncated = True
            body = json.dumps(request_object, separators=(",", ":")).encode("utf-8")
        if len(body) > 256 * 1024:
            return CpaDiscoveryResult((), 0, True, False)
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read(128 * 1024 + 1)
        if len(response_body) > 128 * 1024:
            return CpaDiscoveryResult((), 0, True, False)
        decoded = json.loads(response_body)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return CpaDiscoveryResult((), 0, False, True)

    proposals = decoded.get("proposals") if isinstance(decoded, dict) else None
    if not isinstance(proposals, list):
        return CpaDiscoveryResult((), 0, truncated, False)
    dropped = max(0, len(proposals) - 64)
    proposals = proposals[:64]
    source_candidates = _decoded_candidates(records)
    spans: list[Span] = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            dropped += 1
            continue
        candidate_index = proposal.get("candidate_index")
        start = proposal.get("start")
        end = proposal.get("end")
        digest = proposal.get("preimage_sha256")
        if (
            type(candidate_index) is not int
            or type(start) is not int
            or type(end) is not int
            or not isinstance(digest, str)
            or candidate_index < 0
            or candidate_index >= min(len(source_candidates), 512)
        ):
            dropped += 1
            continue
        line_number, key_path, value = source_candidates[candidate_index]
        if start < 0 or end <= start or end > min(len(value), 8192):
            dropped += 1
            continue
        if hashlib.sha256(value[start:end].encode("utf-8")).hexdigest() != digest:
            dropped += 1
            continue
        spans.append(
            Span(
                line_number,
                key_path,
                start,
                end,
                NEUTRAL_REPLACEMENT_POLICY,
                digest,
                "cpa.proposal.v1",
            )
        )
    return CpaDiscoveryResult(tuple(spans), dropped, truncated, False)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DecontaminationError("incident_directory_invalid", "backup")
    os.chmod(path, 0o700)


def incident_directory(
    terminal_id: str,
    lifecycle_generation: int,
    rule_id: str = CONTENT_POLICY_SCREEN_RULE_ID,
    *,
    log_dir: Path = LOG_DIR,
) -> Path:
    if not terminal_id or "/" in terminal_id or "\\" in terminal_id:
        raise DecontaminationError("terminal_id_not_filesystem_safe", "backup")
    if not _RULE_ID_RE.fullmatch(rule_id):
        raise DecontaminationError("rule_id_not_filesystem_safe", "backup")
    root = log_dir / "decontam"
    _ensure_private_dir(root)
    path = root / f"{terminal_id}-{lifecycle_generation}-{rule_id}"
    _ensure_private_dir(path)
    return path


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_private_write(path: Path, content: bytes) -> None:
    _ensure_private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _read_incident(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    _require_regular(path)
    try:
        value = json.loads(_read_regular(path))
    except json.JSONDecodeError as exc:
        raise DecontaminationError("incident_json_invalid", "backup") from exc
    validate_incident_record(value)
    return cast(dict[str, Any], value)


def validate_incident_record(record: Any) -> None:
    required = {
        "record_version",
        "terminal_id",
        "lifecycle_generation",
        "provider",
        "provider_session_uuid",
        "rule_id",
        "classified_reason",
        "invoker",
        "caller_snapshot",
        "gating_basis",
        "force",
        "prior_incident",
        "attempts",
        "nudge_status",
        "nudge_skip_reason",
        "nudge_message_id",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise DecontaminationError("incident_fields_invalid", "validate")
    scalar_types = {
        "record_version": int,
        "terminal_id": str,
        "lifecycle_generation": int,
        "provider": str,
        "provider_session_uuid": str,
        "rule_id": str,
        "classified_reason": str,
        "invoker": str,
        "gating_basis": str,
        "force": bool,
        "attempts": list,
        "nudge_status": str,
    }
    for field, expected in scalar_types.items():
        if type(record[field]) is not expected:
            raise DecontaminationError("incident_field_type_invalid", "validate")
    if record["record_version"] != 1 or record["provider"] != "codex":
        raise DecontaminationError("incident_version_or_provider_invalid", "validate")
    if record["classified_reason"] != CLASSIFIED_REASON:
        raise DecontaminationError("incident_reason_invalid", "validate")
    if not _RULE_ID_RE.fullmatch(record["rule_id"]):
        raise DecontaminationError("incident_rule_invalid", "validate")
    if record["prior_incident"] is not None and not isinstance(record["prior_incident"], str):
        raise DecontaminationError("incident_prior_invalid", "validate")
    caller = record["caller_snapshot"]
    if not isinstance(caller, dict) or set(caller) != {
        "mailbox_id",
        "incarnation_terminal_id",
    }:
        raise DecontaminationError("incident_caller_snapshot_invalid", "validate")
    if caller["mailbox_id"] is not None and not isinstance(caller["mailbox_id"], str):
        raise DecontaminationError("incident_caller_snapshot_invalid", "validate")
    if caller["incarnation_terminal_id"] is not None and not isinstance(
        caller["incarnation_terminal_id"], str
    ):
        raise DecontaminationError("incident_caller_snapshot_invalid", "validate")
    for attempt in record["attempts"]:
        _validate_attempt(attempt)
    status = record["nudge_status"]
    skip_reason = record["nudge_skip_reason"]
    message_id = record["nudge_message_id"]
    if status not in NUDGE_STATUSES:
        raise DecontaminationError("incident_nudge_status_invalid", "validate")
    if status == "skipped":
        if skip_reason not in NUDGE_SKIP_REASONS:
            raise DecontaminationError("incident_nudge_skip_reason_missing", "validate")
    elif skip_reason is not None:
        raise DecontaminationError("incident_nudge_skip_reason_forbidden", "validate")
    if status == "sent":
        if type(message_id) is not int:
            raise DecontaminationError("incident_nudge_message_id_missing", "validate")
    elif message_id is not None:
        raise DecontaminationError("incident_nudge_message_id_forbidden", "validate")


def validate_nudge_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        raise ValueError("invalid nudge result")
    status = value["status"]
    expected = {"status"}
    if status == "sent":
        expected.add("nudge_message_id")
        if type(value.get("nudge_message_id")) is not int:
            raise ValueError("sent nudge requires nudge_message_id")
    elif status == "skipped":
        expected.add("skip_reason")
        if value.get("skip_reason") not in NUDGE_SKIP_REASONS:
            raise ValueError("skipped nudge requires a known skip_reason")
    elif status not in {"failed", "not_attempted"}:
        raise ValueError("unknown nudge status")
    if set(value) != expected:
        raise ValueError("nudge field presence invalid")
    return value


def _validate_attempt(attempt: Any) -> None:
    fields = {"started_at", "finished_at", "final_stage", "result", "scrub_summary"}
    if not isinstance(attempt, dict) or set(attempt) != fields:
        raise DecontaminationError("incident_attempt_fields_invalid", "validate")
    if not isinstance(attempt["started_at"], str):
        raise DecontaminationError("incident_attempt_started_invalid", "validate")
    if attempt["finished_at"] is not None and not isinstance(attempt["finished_at"], str):
        raise DecontaminationError("incident_attempt_finished_invalid", "validate")
    if attempt["final_stage"] not in FINAL_STAGES:
        raise DecontaminationError("incident_attempt_stage_invalid", "validate")
    if attempt["result"] not in {"installed-and-validated", "failed", "aborted"}:
        raise DecontaminationError("incident_attempt_result_invalid", "validate")
    summary = attempt["scrub_summary"]
    if summary is None:
        return
    expected = {
        "span_count",
        "rule_ids",
        "artifact_sha256_before",
        "artifact_sha256_after",
    }
    if not isinstance(summary, dict) or set(summary) != expected:
        raise DecontaminationError("incident_scrub_summary_invalid", "validate")
    if type(summary["span_count"]) is not int or not isinstance(summary["rule_ids"], list):
        raise DecontaminationError("incident_scrub_summary_invalid", "validate")
    if not all(isinstance(rule, str) for rule in summary["rule_ids"]):
        raise DecontaminationError("incident_scrub_summary_invalid", "validate")
    if not isinstance(summary["artifact_sha256_before"], str):
        raise DecontaminationError("incident_scrub_summary_invalid", "validate")
    if summary["artifact_sha256_after"] is not None and not isinstance(
        summary["artifact_sha256_after"], str
    ):
        raise DecontaminationError("incident_scrub_summary_invalid", "validate")


def mutate_incident(
    incident_dir: Path,
    update: Callable[[dict[str, Any] | None], dict[str, Any]],
) -> dict[str, Any]:
    _ensure_private_dir(incident_dir)
    lock_path = incident_dir / "incident.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        path = incident_dir / "incident.json"
        current = _read_incident(path)
        updated = update(current)
        validate_incident_record(updated)
        _atomic_private_write(
            path,
            (json.dumps(updated, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        return updated
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _initial_incident(
    *,
    terminal_id: str,
    lifecycle_generation: int,
    session_uuid: str,
    invoker: str,
    caller_mailbox_id: str | None,
    caller_terminal_id: str | None,
    gating_basis: str,
    force: bool,
    prior_incident: str | None,
) -> dict[str, Any]:
    return {
        "record_version": 1,
        "terminal_id": terminal_id,
        "lifecycle_generation": lifecycle_generation,
        "provider": "codex",
        "provider_session_uuid": session_uuid,
        "rule_id": CONTENT_POLICY_SCREEN_RULE_ID,
        "classified_reason": CLASSIFIED_REASON,
        "invoker": invoker,
        "caller_snapshot": {
            "mailbox_id": caller_mailbox_id,
            "incarnation_terminal_id": caller_terminal_id,
        },
        "gating_basis": gating_basis,
        "force": force,
        "prior_incident": prior_incident,
        "attempts": [],
        "nudge_status": "not_attempted",
        "nudge_skip_reason": None,
        "nudge_message_id": None,
    }


def _append_attempt(
    incident_dir: Path,
    *,
    initial: dict[str, Any],
    force: bool,
) -> tuple[int, dict[str, Any]]:
    def update(current: dict[str, Any] | None) -> dict[str, Any]:
        record = dict(initial if current is None else current)
        attempts = [dict(item) for item in record["attempts"]]
        if any(item["result"] == "installed-and-validated" for item in attempts):
            if not force:
                raise RepeatedIncident(incident_dir / "incident.json")
            record["force"] = True
            record["prior_incident"] = str(incident_dir / "incident.json")
        attempts.append(
            {
                "started_at": _utcnow(),
                "finished_at": None,
                "final_stage": "backup",
                "result": "aborted",
                "scrub_summary": None,
            }
        )
        record["attempts"] = attempts
        return record

    record = mutate_incident(incident_dir, update)
    return len(record["attempts"]) - 1, record


def _update_attempt(
    prepared: PreparedRecovery,
    *,
    stage: str,
    result: str,
    finished: bool,
) -> None:
    if stage not in FINAL_STAGES:
        raise ValueError("unknown WPD1 stage")

    def update(current: dict[str, Any] | None) -> dict[str, Any]:
        if current is None:
            raise DecontaminationError("incident_missing", stage)
        record = dict(current)
        attempts = [dict(item) for item in current["attempts"]]
        attempt = attempts[prepared.attempt_index]
        attempt["final_stage"] = stage
        attempt["result"] = result
        attempt["finished_at"] = _utcnow() if finished else None
        attempt["scrub_summary"] = {
            "span_count": prepared.scrub_result.span_count,
            "rule_ids": list(prepared.scrub_result.rule_ids),
            "artifact_sha256_before": prepared.scrub_result.artifact_sha256_before,
            "artifact_sha256_after": prepared.scrub_result.artifact_sha256_after,
        }
        attempts[prepared.attempt_index] = attempt
        record["attempts"] = attempts
        return record

    mutate_incident(prepared.incident_path.parent, update)


def update_incident_nudge(
    incident_path: Path,
    *,
    status: str,
    skip_reason: str | None = None,
    message_id: int | None = None,
) -> None:
    def update(current: dict[str, Any] | None) -> dict[str, Any]:
        if current is None:
            raise DecontaminationError("incident_missing", "complete")
        record = dict(current)
        record["nudge_status"] = status
        record["nudge_skip_reason"] = skip_reason
        record["nudge_message_id"] = message_id
        return record

    mutate_incident(incident_path.parent, update)


def _create_private_file(path: Path, content: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise DecontaminationError("protected_file_create_failed", "backup") from exc


def _replace_target_from_bytes(path: Path, content: bytes, session_uuid: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.wpd1-", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        validate_artifact(temp, session_uuid)
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def restore_backup(prepared: PreparedRecovery) -> None:
    backup = _read_regular(prepared.backup_path)
    _replace_target_from_bytes(prepared.artifact_path, backup, prepared.session_uuid)


def post_initialize_failure(prepared: PreparedRecovery, stage: str) -> bool:
    """Restore only when the installed artifact is proven not to have advanced."""
    try:
        current = artifact_identity(prepared.artifact_path)
    except DecontaminationError:
        _update_attempt(prepared, stage=stage, result="installed-and-validated", finished=True)
        return False
    if current == prepared.installed_identity:
        restore_backup(prepared)
        _update_attempt(prepared, stage=stage, result="failed", finished=True)
        return True
    _update_attempt(prepared, stage=stage, result="installed-and-validated", finished=True)
    return False


def mark_recovery_complete(prepared: PreparedRecovery) -> None:
    _update_attempt(
        prepared,
        stage="complete",
        result="installed-and-validated",
        finished=True,
    )


def mark_recovery_failure(prepared: PreparedRecovery, stage: str, *, restored: bool) -> None:
    _update_attempt(
        prepared,
        stage=stage,
        result="failed" if restored else "installed-and-validated",
        finished=True,
    )


def _span_detail(spans: Iterable[Span]) -> list[dict[str, Any]]:
    return [
        {
            "line_number": span.line_number,
            "key_path": list(span.key_path),
            "start": span.start,
            "end": span.end,
            "replacement_policy": span.replacement_policy,
            "expected_preimage_sha256": span.expected_preimage_sha256,
            "rule_id": span.rule_id,
        }
        for span in spans
    ]


def prepare_content_recovery(
    *,
    terminal_id: str,
    lifecycle_generation: int,
    session_uuid: str,
    invoker: str,
    caller_mailbox_id: str | None,
    caller_terminal_id: str | None,
    gating_basis: str,
    force: bool,
    show: bool,
    ad_hoc_spans: Iterable[Span] = (),
    use_cpa: bool = True,
    log_dir: Path = LOG_DIR,
) -> PreparedRecovery:
    lease = acquire_artifact_lease(session_uuid)
    if lease is None:
        raise DecontaminationError("artifact_lease_busy", "backup")
    prepared: PreparedRecovery | None = None
    attempt_index: int | None = None
    artifact_path: Path | None = None
    original: bytes | None = None
    scrub_result: ScrubResult | None = None
    installed = False
    incident_dir = incident_directory(
        terminal_id, lifecycle_generation, log_dir=log_dir
    )
    try:
        initial = _initial_incident(
            terminal_id=terminal_id,
            lifecycle_generation=lifecycle_generation,
            session_uuid=session_uuid,
            invoker=invoker,
            caller_mailbox_id=caller_mailbox_id,
            caller_terminal_id=caller_terminal_id,
            gating_basis=gating_basis,
            force=force,
            prior_incident=None,
        )
        attempt_index, _record = _append_attempt(incident_dir, initial=initial, force=force)
        artifact_path = find_artifact(session_uuid)
        original_identity = artifact_identity(artifact_path)
        original = _read_regular(artifact_path)
        backup_path = incident_dir / f"backup-{attempt_index + 1:04d}.jsonl"
        _create_private_file(backup_path, original)
        records = validate_artifact_bytes(original, session_uuid, artifact_path.name)
        spans = list(discover_artifact_spans(records))
        spans.extend(ad_hoc_spans)
        cpa_result = discover_cpa_spans(records) if use_cpa else CpaDiscoveryResult((), 0, False, True)
        if cpa_result.human_gate_required:
            raise DecontaminationError("cpa_truncated_human_gate_required", "scrub")
        spans.extend(cpa_result.spans)
        if not spans:
            raise DecontaminationError("no_authorized_spans", "scrub")
        try:
            scrub_result = rewrite_jsonl(original, spans)
        except ScrubRejected as exc:
            raise DecontaminationError(str(exc), "scrub") from exc
        validate_artifact_bytes(scrub_result.content, session_uuid, artifact_path.name)
        detail = _span_detail(scrub_result.spans)
        detail_path = incident_dir / f"span-detail-{attempt_index + 1:04d}.json"
        _create_private_file(
            detail_path,
            (json.dumps(detail, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        _validate_artifact_lease(lease)
        if artifact_identity(artifact_path) != original_identity:
            raise DecontaminationError("artifact_drift_before_install", "install")
        _replace_target_from_bytes(artifact_path, scrub_result.content, session_uuid)
        installed = True
        installed_identity = artifact_identity(artifact_path)
        if installed_identity.sha256 != scrub_result.artifact_sha256_after:
            raise DecontaminationError("installed_artifact_hash_mismatch", "install")
        prepared = PreparedRecovery(
            terminal_id=terminal_id,
            lifecycle_generation=lifecycle_generation,
            session_uuid=session_uuid,
            artifact_path=artifact_path,
            backup_path=backup_path,
            incident_path=incident_dir / "incident.json",
            attempt_index=attempt_index,
            installed_identity=installed_identity,
            scrub_result=scrub_result,
            lease=lease,
            span_detail=detail if show else [],
        )
        _update_attempt(
            prepared,
            stage="install",
            result="installed-and-validated",
            finished=False,
        )
        return prepared
    except Exception as exc:
        if prepared is None:
            try:
                restored = False
                if installed and artifact_path is not None and original is not None:
                    _replace_target_from_bytes(artifact_path, original, session_uuid)
                    restored = True
                if attempt_index is not None:
                    stage = getattr(exc, "stage", "backup")

                    def fail(current: dict[str, Any] | None) -> dict[str, Any]:
                        if current is None:
                            raise DecontaminationError("incident_missing", stage)
                        record = dict(current)
                        attempts = [dict(item) for item in current["attempts"]]
                        attempt = attempts[attempt_index]
                        attempt["final_stage"] = stage if stage in FINAL_STAGES else "backup"
                        attempt["finished_at"] = _utcnow()
                        attempt["result"] = (
                            "failed" if restored or not installed else "aborted"
                        )
                        if scrub_result is not None:
                            attempt["scrub_summary"] = {
                                "span_count": scrub_result.span_count,
                                "rule_ids": list(scrub_result.rule_ids),
                                "artifact_sha256_before": scrub_result.artifact_sha256_before,
                                "artifact_sha256_after": scrub_result.artifact_sha256_after,
                            }
                        attempts[attempt_index] = attempt
                        record["attempts"] = attempts
                        return record

                    mutate_incident(incident_dir, fail)
            finally:
                release_artifact_lease(lease)
        raise


def release_prepared_recovery(prepared: PreparedRecovery | None) -> None:
    if prepared is not None:
        release_artifact_lease(prepared.lease)


def public_scrub_summary(prepared: PreparedRecovery, *, show: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "incident_path": str(prepared.incident_path),
        "backup_path": str(prepared.backup_path),
        "span_count": prepared.scrub_result.span_count,
        "rule_ids": list(prepared.scrub_result.rule_ids),
        "artifact_sha256_before": prepared.scrub_result.artifact_sha256_before,
        "artifact_sha256_after": prepared.scrub_result.artifact_sha256_after,
    }
    if show:
        result["spans"] = prepared.span_detail
    return result
