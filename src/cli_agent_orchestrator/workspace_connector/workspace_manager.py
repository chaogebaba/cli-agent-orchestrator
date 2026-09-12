"""Read-only workspace containment — port of ``src/workspace/manager.ts``.

Upstream: XiaoDuoYa/codex-with-chatgpt at commit
8fdd97c188c7678d0d9c43b3769b426940de568a (MIT).  Behavioural port: realpath
containment against the deepest existing ancestor, sensitive-file denial,
pagination and byte caps.  CAO additions (D5): ``.c2c.json`` project config and
project detection are replaced by the manifest-scoped ``workspace_info``
summary (the N4 r4 ruling: identity, reviewed commit and manifest totals only),
a content digest on every result, and the ``PATH_NOT_IN_MANIFEST`` refusal when
an attempt-scoped allowlist manifest is bound.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
from pathlib import Path

from cli_agent_orchestrator.workspace_connector.ignore_rules import IgnoreRules

DEFAULT_MAX_LINES = 400
HARD_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 256 * 1024


class WorkspaceError(Exception):
    """Structured workspace refusal (upstream parity: code + message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Upstream error codes, verbatim (D5 rename map does not touch these).
INVALID_PATH = "INVALID_PATH"
PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
ACCESS_DENIED_SENSITIVE_FILE = "ACCESS_DENIED_SENSITIVE_FILE"
PATH_NOT_IN_MANIFEST = "PATH_NOT_IN_MANIFEST"
FILE_NOT_FOUND = "FILE_NOT_FOUND"
NOT_A_FILE = "NOT_A_FILE"
NOT_A_DIRECTORY = "NOT_A_DIRECTORY"
BINARY_FILE = "BINARY_FILE"
FILE_TOO_LARGE = "FILE_TOO_LARGE"


def content_digest(data: str | bytes) -> str:
    """CAO addition (D5): SHA-256 content digest attached to every result."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_result_bytes(data: object) -> bytes:
    """Canonical result encoding used for both audit digests and byte budgets."""
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class Workspace:
    """A frozen read-only workspace root bound to one attempt (D5).

    Upstream constructs this from a live workspace directory; the CAO port
    points it at the attempt's frozen read-only worktree checked out at the
    reviewed source commit.  Containment behaviour is the upstream's: an
    untrusted path is canonicalized by realpath-ing its deepest existing
    ancestor, which defends against symlink escapes even for not-yet-existing
    leaf segments.
    """

    def __init__(self, root_input: str | Path, manifest: list[str] | None = None) -> None:
        resolved = Path(root_input).resolve()
        try:
            real = Path(os.path.realpath(resolved))
        except OSError:
            raise WorkspaceError(FILE_NOT_FOUND, f"Workspace root does not exist: {root_input}")
        if not real.is_dir():
            raise WorkspaceError(
                NOT_A_DIRECTORY, f"Workspace root is not a directory: {root_input}"
            )
        self.root = real
        self.id = hashlib.sha256(os.fsencode(str(real))).hexdigest()[:12]
        self.ignore_rules = IgnoreRules(real)
        self.name = real.name or str(real)
        # CAO addition (D5, r3 N4): the allowlisted source manifest.  When
        # present, every path-bearing operation is scoped to it and the
        # attempt's access token is bound to it.
        self.manifest = self._normalize_manifest(manifest)
        manifest_rows = sorted(self.manifest) if self.manifest is not None else []
        self.manifest_digest = content_digest(canonical_result_bytes(manifest_rows))

    def _normalize_manifest(self, manifest: list[str] | None) -> frozenset[str] | None:
        if manifest is None:
            return None
        normalized: set[str] = set()
        for requested in manifest:
            _, rel = self.resolve(requested)
            if rel:
                normalized.add(rel)
        return frozenset(normalized)

    # ---- containment -------------------------------------------------------

    def _contains(self, candidate: Path) -> bool:
        try:
            candidate.relative_to(self.root)
            return True
        except ValueError:
            return candidate == self.root

    def _canonicalize(self, abs_path: Path) -> Path:
        """Realpath the deepest existing ancestor (upstream parity)."""
        current = abs_path
        suffix: list[str] = []
        while True:
            try:
                real = Path(os.path.realpath(current))
                return real.joinpath(*suffix) if suffix else real
            except OSError:
                parent = current.parent
                if parent == current:
                    return abs_path
                suffix.insert(0, current.name)
                current = parent

    def _in_manifest(self, rel: str) -> bool:
        if self.manifest is None:
            return True
        if rel in self.manifest:
            return True
        # A manifest directory entry scopes every file beneath it.
        parts = Path(rel).parts
        for i in range(1, len(parts)):
            if str(Path(*parts[:i])) in self.manifest:
                return True
        return False

    def _visible_in_manifest(self, rel: str) -> bool:
        """Return true for an allowed row or a directory leading to one."""
        if self.manifest is None or rel == "":
            return True
        prefix = rel.rstrip("/") + "/"
        return self._in_manifest(rel) or any(item.startswith(prefix) for item in self.manifest)

    def resolve(
        self, requested: str, *, allow_sensitive: bool = False, require_manifest: bool = False
    ) -> tuple[Path, str]:
        """Resolve an untrusted path to a canonical absolute path inside the workspace.

        Raises ``WorkspaceError`` with ``PATH_OUTSIDE_WORKSPACE`` or
        ``ACCESS_DENIED_SENSITIVE_FILE`` (upstream) or ``PATH_NOT_IN_MANIFEST``
        (CAO addition, D5).
        """
        if not isinstance(requested, str) or "\x00" in requested:
            raise WorkspaceError(INVALID_PATH, "Invalid path")
        p = requested.strip()
        if p in ("", "/"):
            p = "."
        # Normalize separators so Windows-style input behaves identically.
        p = p.replace("\\", "/")
        # Strip a "workspace:/" alias prefix if the model echoes it back.
        if p.lower().startswith("workspace:/"):
            p = p[len("workspace:/") :]
            if p.startswith("/"):
                p = p[1:]
        if p == "":
            p = "."

        abs_path = self.root / p if p != "." else self.root
        abs_path = Path(os.path.normpath(abs_path))
        canonical = self._canonicalize(abs_path)
        if not self._contains(canonical):
            raise WorkspaceError(
                PATH_OUTSIDE_WORKSPACE,
                f"Path resolves outside the connected workspace: {requested}",
            )
        try:
            rel = str(canonical.relative_to(self.root))
        except ValueError:
            raise WorkspaceError(
                PATH_OUTSIDE_WORKSPACE,
                f"Path resolves outside the connected workspace: {requested}",
            )
        rel = "" if rel == "." else rel
        if rel.startswith(".."):
            raise WorkspaceError(
                PATH_OUTSIDE_WORKSPACE,
                f"Path resolves outside the connected workspace: {requested}",
            )
        if not allow_sensitive and rel != "" and self.ignore_rules.is_sensitive(rel):
            raise WorkspaceError(
                ACCESS_DENIED_SENSITIVE_FILE,
                f"ACCESS_DENIED_SENSITIVE_FILE: '{rel}' matches the sensitive-file policy"
                " and cannot be read.",
            )
        if require_manifest and rel != "" and not self._in_manifest(rel):
            raise WorkspaceError(
                PATH_NOT_IN_MANIFEST,
                f"PATH_NOT_IN_MANIFEST: '{rel}' is not in this attempt's allowlisted"
                " source manifest.",
            )
        return canonical, rel

    # ---- reads -------------------------------------------------------------

    @staticmethod
    def _is_binary(abs_path: Path) -> bool:
        try:
            with open(abs_path, "rb") as fh:
                buf = fh.read(8192)
        except OSError:
            return True
        return b"\x00" in buf

    def read_file(
        self,
        requested: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        max_lines: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, object]:
        """Line-range paginated read (upstream parity + CAO digest)."""
        abs_path, rel = self.resolve(requested, require_manifest=True)
        try:
            stat = abs_path.stat()
        except OSError:
            raise WorkspaceError(FILE_NOT_FOUND, f"File not found: {rel}")
        if not stat_module.S_ISREG(stat.st_mode):
            raise WorkspaceError(NOT_A_FILE, f"Not a regular file: {rel}")
        if self._is_binary(abs_path):
            raise WorkspaceError(
                BINARY_FILE, f"Binary file ({stat.st_size} bytes): {rel}. Content is not returned."
            )

        start = max(1, int(start_line or 1))
        max_ln = min(HARD_MAX_LINES, max(1, int(max_lines or DEFAULT_MAX_LINES)))
        if end_line is not None:
            end_limit = min(int(end_line), start + HARD_MAX_LINES - 1)
        else:
            end_limit = start + max_ln - 1
        max_b = min(1024 * 1024, max(1024, int(max_bytes or DEFAULT_MAX_BYTES)))

        lines: list[str] = []
        total_lines = 0
        collected_bytes = 0
        byte_truncated = False
        actual_end = start - 1

        try:
            with open(abs_path, encoding="utf-8", errors="strict") as fh:
                for line in fh:
                    line = line.rstrip("\n").rstrip("\r")
                    total_lines += 1
                    if total_lines >= start and total_lines <= end_limit and not byte_truncated:
                        cost = len(line.encode("utf-8")) + 1
                        if collected_bytes + cost > max_b:
                            if not lines:
                                raw = line.encode("utf-8")[:max_b]
                                lines.append(raw.decode("utf-8", errors="ignore"))
                                collected_bytes = len(raw)
                                actual_end = total_lines
                            byte_truncated = True
                        else:
                            lines.append(line)
                            collected_bytes += cost
                            actual_end = total_lines
        except UnicodeDecodeError:
            raise WorkspaceError(BINARY_FILE, f"Binary file ({stat.st_size} bytes): {rel}.")
        except OSError as exc:
            raise WorkspaceError(FILE_NOT_FOUND, f"File not found: {rel}: {exc}")

        remaining = max(0, total_lines - actual_end)
        content = "\n".join(lines)
        result: dict[str, object] = {
            "path": rel,
            "sizeBytes": stat.st_size,
            "totalLines": total_lines,
            "startLine": min(start, max(total_lines, 1)),
            "endLine": actual_end,
            "truncated": remaining > 0,
            "remainingLines": remaining,
            "nextStartLine": actual_end + 1 if remaining > 0 else None,
            "content": content,
        }
        # CAO addition (D5): digest on every result.
        result["contentDigest"] = content_digest(content)
        return result

    def list_directory(
        self,
        requested: str,
        *,
        depth: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, object]:
        """Paginated directory listing with hidden/noise entries omitted (upstream parity).

        CAO addition (D5, r4 N4): when a manifest is bound, entries outside it
        are omitted entirely.
        """
        abs_path, rel = self.resolve(requested)
        try:
            stat = abs_path.stat()
        except OSError:
            raise WorkspaceError(FILE_NOT_FOUND, f"Directory not found: {rel or '.'}")
        if not stat_module.S_ISDIR(stat.st_mode):
            raise WorkspaceError(NOT_A_DIRECTORY, f"Not a directory: {rel}")
        d = min(4, max(1, int(depth or 1)))
        lim = min(1000, max(1, int(limit or 200)))
        off = max(0, int(offset or 0))

        all_entries: list[dict[str, object]] = []

        def walk(dir_abs: Path, dir_rel: str, level: int) -> None:
            try:
                entries = sorted(os.scandir(dir_abs), key=lambda e: (not e.is_dir(), e.name))
            except OSError:
                return
            for entry in entries:
                child_rel = f"{dir_rel}/{entry.name}" if dir_rel else entry.name
                if self.ignore_rules.is_hidden(child_rel) or self.ignore_rules.is_hidden(
                    child_rel + "/"
                ):
                    continue
                if self.manifest is not None and not self._visible_in_manifest(child_rel):
                    # Manifest scoping (r4 N4): never reveal anything outside it.
                    continue
                if entry.is_dir(follow_symlinks=False):
                    all_entries.append({"path": child_rel + "/", "type": "dir"})
                    if level < d:
                        walk(dir_abs / entry.name, child_rel, level + 1)
                elif entry.is_file(follow_symlinks=False):
                    try:
                        size = (dir_abs / entry.name).stat().st_size
                    except OSError:
                        size = 0
                    all_entries.append({"path": child_rel, "type": "file", "sizeBytes": size})
                if len(all_entries) >= off + lim + 2000:  # hard cap for huge trees
                    return

        walk(abs_path, rel, 1)
        page = all_entries[off : off + lim]
        result: dict[str, object] = {
            "path": rel or ".",
            "entries": page,
            "total": len(all_entries),
            "offset": off,
            "limit": lim,
            "hasMore": off + len(page) < len(all_entries),
        }
        result["contentDigest"] = content_digest(canonical_result_bytes(page))
        return result
