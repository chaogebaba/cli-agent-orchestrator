"""Digest artifact validation and publication for warm-base refreshes."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, assert_never, cast

from cli_agent_orchestrator.models.observation import (
    CoverageProof,
    CoverageReason,
    CoverageUnobs,
    Covered,
    Deadline,
    Disproven,
    Observation,
    Proven,
    Unobservable,
)
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
        f"{entry.state} {entry.value or '-'} {encode_path(entry.path)}" for entry in ordered
    )


def projected_manifest_bytes(entries: tuple[SnapshotEntry, ...] | list[SnapshotEntry]) -> int:
    """Return the encoded entry-manifest bytes before snapshot state is discarded."""
    return len(canonical_entries(entries).encode("utf-8"))


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
    reason: DigestInvalidReason
    delta: SnapshotDelta
    detail: str | BudgetBreakdown | None = None
    kind: Literal["invalid"] = "invalid"


@dataclass(frozen=True)
class DigestUnobservable:
    reason: CoverageUnobs
    delta: SnapshotDelta
    detail: str | None = None
    kind: Literal["unobservable"] = "unobservable"


DigestInvalidReason = Literal[
    "over_budget_artifact",
    "base_mismatch",
    "lineage",
    "malformed_manifest",
    "unreadable",
]


@dataclass(frozen=True)
class BudgetBreakdown:
    cap: int
    artifact_bytes: int | None = None
    manifest_bytes: int | None = None
    body_bytes: int | None = None
    top_dirty: tuple[tuple[str, int], ...] = ()


class OverBudgetError(ValueError):
    """A rendered digest exceeded the cap before publication."""

    def __init__(self, detail: BudgetBreakdown, *, kind: Literal["rendered"] = "rendered"):
        self.kind = kind
        self.detail = detail
        super().__init__(
            f"{kind} digest exceeds {detail.cap} bytes: "
            f"manifest={detail.manifest_bytes}, body={detail.body_bytes}, "
            f"top_dirty={detail.top_dirty}"
        )


DigestDecision = DigestCovered | DigestPending | DigestInvalid | DigestUnobservable


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


def _digest_dir(row: dict[str, Any]) -> Path:
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


def coverage(
    artifact: BaseDigestArtifact,
    delta: SnapshotDelta,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Observation[Covered, CoverageProof, CoverageReason]:
    """Select the first applicable coverage disposition in the ruled order."""
    if delta.acquisition_error:
        return Unobservable("apparatus_unavailable", retry_after=Deadline(clock()))
    if any(entry.state == "unhashable" for entry in delta.entries):
        return Unobservable("unhashable_entry", retry_after=Deadline(clock()))
    if not _same_entries(artifact.entries, delta.entries):
        return Disproven("entry_mismatch")
    return Proven(Covered(), by=CoverageProof.ENTRIES_MATCH)


def evaluate(row: dict[str, Any], delta: SnapshotDelta) -> DigestDecision:
    """Return the closed refresh decision for one base and acquired delta."""
    if delta.acquisition_error:
        return DigestUnobservable("apparatus_unavailable", delta, detail=delta.acquisition_error)
    candidate = _newest_candidate(row["name"], _digest_dir(row))
    if candidate is None:
        return DigestPending(delta)
    try:
        if candidate.stat().st_size > MAX_DIGEST_BYTES:
            artifact_bytes = candidate.stat().st_size
            manifest_bytes: int | None = None
            body_bytes: int | None = None
            top_dirty: tuple[tuple[str, int], ...] = ()
            try:
                raw = candidate.read_bytes()
                if raw.startswith(_MANIFEST_START):
                    end = raw.find(_MANIFEST_END, len(_MANIFEST_START))
                    if end >= 0:
                        manifest_bytes = end + len(_MANIFEST_END)
                        body_bytes = len(raw) - manifest_bytes
                        # Extract top dirty paths from manifest for diagnostics
                        manifest_text = raw[len(_MANIFEST_START) : end].decode("utf-8", "replace")
                        entry_sizes: list[tuple[str, int]] = []
                        for line in manifest_text.splitlines():
                            if not line or line.startswith(
                                ("base:", "parent_artifact_sha:", "artifact_sha:")
                            ):
                                continue
                            parts = line.split(" ", 2)
                            if len(parts) == 3:
                                entry_sizes.append((parts[2], len(line.encode("utf-8"))))
                        entry_sizes.sort(key=lambda item: (-item[1], item[0]))
                        top_dirty = tuple(entry_sizes[:5])
            except OSError:
                pass
            return DigestInvalid(
                "over_budget_artifact",
                delta,
                detail=BudgetBreakdown(
                    artifact_bytes=artifact_bytes,
                    cap=MAX_DIGEST_BYTES,
                    manifest_bytes=manifest_bytes,
                    body_bytes=body_bytes,
                    top_dirty=top_dirty,
                ),
            )
        artifact = _parse_manifest(candidate)
    except (OSError, UnicodeError) as exc:
        return DigestInvalid("unreadable", delta, detail=str(exc))
    except ValueError as exc:
        return DigestInvalid("malformed_manifest", delta, detail=str(exc))
    if artifact.base != row["name"]:
        return DigestInvalid("base_mismatch", delta)
    expected_parent = row.get("digest_head") or "genesis"
    if artifact.parent_artifact_sha != expected_parent:
        return DigestInvalid("lineage", delta)
    observation = coverage(artifact, delta)
    if isinstance(observation, Disproven):
        return DigestPending(delta)
    if isinstance(observation, Unobservable):
        detail: str | None = None
        if observation.reason == "unhashable_entry":
            unhashable_paths = [e.path for e in delta.entries if e.state == "unhashable"][:10]
            detail = f"unhashable_paths={unhashable_paths}"
        return DigestUnobservable(cast(CoverageUnobs, observation.reason), delta, detail=detail)
    if isinstance(observation, Proven):
        return DigestCovered(artifact)
    assert_never(observation)


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
    manifest_data = manifest.encode("utf-8")
    body_data = (body.rstrip("\n") + "\n").encode("utf-8")
    provisional = manifest_data + body_data
    artifact_sha = _artifact_hash(provisional)
    final = provisional.replace(
        ("artifact_sha: " + "0" * 64 + "\n").encode("ascii"),
        f"artifact_sha: {artifact_sha}\n".encode("ascii"),
        1,
    )
    if len(final) > MAX_DIGEST_BYTES:
        contributors = tuple(
            sorted(
                (
                    (
                        entry.path,
                        len(
                            f"{entry.state} {entry.value or '-'} {encode_path(entry.path)}".encode(
                                "utf-8"
                            )
                        ),
                    )
                    for entry in delta.entries
                ),
                key=lambda item: (-item[1], encode_path(item[0]).encode("utf-8")),
            )[:5]
        )
        raise OverBudgetError(
            BudgetBreakdown(
                manifest_bytes=len(manifest_data),
                body_bytes=len(body_data),
                cap=MAX_DIGEST_BYTES,
                top_dirty=contributors,
            )
        )
    with tempfile.NamedTemporaryFile(
        "wb", dir=directory, prefix=".digest-", delete=False
    ) as stream:
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


def get_digest_head(base_name: str) -> str | None:
    """Return the current digest_head SHA for a base, or None if no digest published."""
    from cli_agent_orchestrator.clients.database import get_ready_provider_session

    row = get_ready_provider_session(base_name)
    if row is None:
        return None
    return row.get("digest_head")


def publish_genesis_digest(base_name: str, cwd: str) -> BaseDigestArtifact:
    """Publish a zero-entry genesis digest for a fresh base on a clean tree.

    The genesis digest has parent_artifact_sha='genesis' and an empty entry set,
    establishing the lineage root for all future digests.
    """
    delta = SnapshotDelta(git_sha=None, entries=())
    artifact = publish(
        base=base_name,
        cwd=cwd,
        parent_artifact_sha=None,
        delta=delta,
        body="Genesis digest: base registered with clean worktree.\n",
        round_number=0,
    )
    # Update the base row's digest_head to the new artifact SHA
    from cli_agent_orchestrator.clients.database import (
        get_ready_provider_session,
        update_provider_session_snapshot,
    )

    row = get_ready_provider_session(base_name)
    if row is not None:
        update_provider_session_snapshot(
            row["id"],
            git_sha=row.get("git_sha"),
            dirty_hashes=row.get("dirty_hashes", "{}"),
            digest_head=artifact.artifact_sha,
        )
    return artifact
