"""Digest artifact validation and publication for warm-base refreshes."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from cli_agent_orchestrator.services.fork_context_service import (
    SnapshotDelta,
    SnapshotEntry,
)

MAX_DIGEST_BYTES = 12_000
_FILENAME = re.compile(r"^(?P<base>.+)-(?P<date>\d{4}-\d{2}-\d{2})-r(?P<round>\d+)\.md$")
_MANIFEST_START = b"<!-- digest-manifest\n"
_MANIFEST_END = b"-->\n"


def encode_path(path: str) -> str:
    """Injectively encode a UTF-8 path for a one-line manifest entry."""
    return path.replace("\\", "\\\\").replace("\n", "\\n")


def decode_path(value: str) -> str:
    chars: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\":
            chars.append(char)
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"\\", "n"}:
            raise ValueError("malformed-path-escape")
        chars.append("\\" if value[index + 1] == "\\" else "\n")
        index += 2
    return "".join(chars)


def canonical_entries(entries: tuple[SnapshotEntry, ...] | list[SnapshotEntry]) -> str:
    ordered = sorted(entries, key=lambda entry: encode_path(entry.path).encode("utf-8"))
    return "\n".join(
        f"{entry.state} {entry.value or '-'} {encode_path(entry.path)}"
        for entry in ordered
    )


def state_key(delta: SnapshotDelta) -> str:
    return hashlib.sha256(canonical_entries(delta.entries).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BaseDigestArtifact:
    path: Path
    base: str
    parent_artifact_sha: str
    artifact_sha: str
    entries: tuple[SnapshotEntry, ...]
    body: str


@dataclass(frozen=True)
class DigestCovered:
    artifact: BaseDigestArtifact
    kind: Literal["covered"] = "covered"


@dataclass(frozen=True)
class DigestPending:
    delta: SnapshotDelta
    kind: Literal["pending"] = "pending"


@dataclass(frozen=True)
class DigestInvalid:
    reason: str
    kind: Literal["invalid"] = "invalid"


DigestDecision = DigestCovered | DigestPending | DigestInvalid


def _artifact_hash(data: bytes) -> str:
    marker = re.compile(rb"^artifact_sha: [0-9a-f]{64}\n", re.MULTILINE)
    canonical, count = marker.subn(b"", data, count=1)
    if count != 1:
        raise ValueError("malformed-artifact-sha")
    return hashlib.sha256(canonical).hexdigest()


def _parse_manifest(path: Path) -> BaseDigestArtifact:
    data = path.read_bytes()
    if not data.startswith(_MANIFEST_START):
        raise ValueError("malformed-manifest")
    end = data.find(_MANIFEST_END, len(_MANIFEST_START))
    if end < 0:
        raise ValueError("malformed-manifest")
    manifest = data[len(_MANIFEST_START) : end].decode("utf-8", "strict")
    body = data[end + len(_MANIFEST_END) :].decode("utf-8", "strict")
    fields: dict[str, str] = {}
    entries: list[SnapshotEntry] = []
    for line in manifest.splitlines():
        if not line:
            continue
        if line.startswith(("base:", "parent_artifact_sha:", "artifact_sha:")):
            key, value = line.split(":", 1)
            if key in fields:
                raise ValueError("duplicate-manifest-field")
            fields[key] = value.strip()
            continue
        pieces = line.split(" ", 2)
        if len(pieces) != 3:
            raise ValueError("malformed-entry")
        state, value, encoded_path = pieces
        if state not in {"sha256", "absent", "unhashable"}:
            raise ValueError("malformed-entry-state")
        if state == "sha256" and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("malformed-entry-hash")
        if state != "sha256" and value != "-":
            raise ValueError("malformed-entry-value")
        entries.append(
            SnapshotEntry(
                decode_path(encoded_path),
                state,  # type: ignore[arg-type]
                value if state == "sha256" else None,
            )
        )
    if set(fields) != {"base", "parent_artifact_sha", "artifact_sha"}:
        raise ValueError("malformed-manifest-fields")
    if not fields["base"] or not re.fullmatch(
        r"(?:[0-9a-f]{64}|genesis)", fields["parent_artifact_sha"]
    ):
        raise ValueError("malformed-lineage")
    if not re.fullmatch(r"[0-9a-f]{64}", fields["artifact_sha"]):
        raise ValueError("malformed-artifact-sha")
    paths = [entry.path for entry in entries]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate-entry-path")
    actual_sha = _artifact_hash(data)
    if actual_sha != fields["artifact_sha"]:
        raise ValueError("artifact-sha-mismatch")
    return BaseDigestArtifact(
        path=path,
        base=fields["base"],
        parent_artifact_sha=fields["parent_artifact_sha"],
        artifact_sha=fields["artifact_sha"],
        entries=tuple(entries),
        body=body,
    )


def _digest_dir(row: dict) -> Path:
    return Path(row["cwd"]) / "tmp" / "orch" / "digests"


def _newest_candidate(base: str, directory: Path) -> Path | None:
    candidates: list[tuple[date, int, Path]] = []
    try:
        paths = directory.iterdir()
    except OSError:
        return None
    for path in paths:
        match = _FILENAME.match(path.name)
        if not match or match.group("base") != base:
            continue
        try:
            candidates.append(
                (date.fromisoformat(match.group("date")), int(match.group("round")), path)
            )
        except ValueError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _same_entries(left: tuple[SnapshotEntry, ...], right: tuple[SnapshotEntry, ...]) -> bool:
    key = lambda entry: encode_path(entry.path).encode("utf-8")
    return sorted(left, key=key) == sorted(right, key=key)


def evaluate(row: dict, delta: SnapshotDelta) -> DigestDecision:
    """Return the closed refresh decision for one base and acquired delta."""
    if delta.acquisition_error:
        return DigestInvalid(f"acquisition:{delta.acquisition_error}")
    candidate = _newest_candidate(row["name"], _digest_dir(row))
    if candidate is None:
        return DigestPending(delta)
    try:
        if candidate.stat().st_size > MAX_DIGEST_BYTES:
            return DigestInvalid("over-budget")
        artifact = _parse_manifest(candidate)
    except (OSError, UnicodeError, ValueError) as exc:
        return DigestInvalid(str(exc))
    if artifact.base != row["name"]:
        return DigestInvalid("base-mismatch")
    expected_parent = row.get("digest_head") or "genesis"
    if artifact.parent_artifact_sha != expected_parent:
        return DigestInvalid("lineage")
    if any(entry.state == "unhashable" for entry in delta.entries):
        return DigestPending(delta)
    if not _same_entries(artifact.entries, delta.entries):
        return DigestPending(delta)
    return DigestCovered(artifact)


def publish(
    *,
    base: str,
    cwd: str,
    parent_artifact_sha: str | None,
    delta: SnapshotDelta,
    body: str,
    round_number: int,
) -> BaseDigestArtifact:
    if delta.acquisition_error:
        raise ValueError(f"cannot-publish:{delta.acquisition_error}")
    directory = Path(cwd) / "tmp" / "orch" / "digests"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{base}-{datetime.now(timezone.utc).date().isoformat()}-r{round_number}.md"
    parent = parent_artifact_sha or "genesis"
    entries = canonical_entries(delta.entries)
    manifest = (
        "<!-- digest-manifest\n"
        f"base: {base}\n"
        f"parent_artifact_sha: {parent}\n"
        "artifact_sha: " + "0" * 64 + "\n"
        f"{entries}\n"
        "-->\n"
    )
    provisional = (manifest + body.rstrip("\n") + "\n").encode("utf-8")
    artifact_sha = _artifact_hash(provisional)
    final = provisional.replace(
        ("artifact_sha: " + "0" * 64 + "\n").encode("ascii"),
        f"artifact_sha: {artifact_sha}\n".encode("ascii"),
        1,
    )
    if len(final) > MAX_DIGEST_BYTES:
        raise ValueError("over-budget")
    with tempfile.NamedTemporaryFile("wb", dir=directory, prefix=".digest-", delete=False) as stream:
        stream.write(final)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.replace(temporary, path)
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return _parse_manifest(path)


def refresh_prompt(artifact: BaseDigestArtifact) -> str:
    return (
        f"[CAO AUTO-REFRESH] Read ONLY digest artifact '{artifact.path}'. "
        "Ingest it as your updated general project context; do no unrelated work "
        "and reply only after the refresh is complete."
    )
