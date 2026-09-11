"""F970 (#819) — read-only workspace access with fail-closed containment.

Ported in shape from XiaoDuoYa/codex-with-chatgpt (`src/mcp/server.ts`,
`src/workspace/manager.ts`, `src/workspace/ignore.ts`, MIT), which is the one
genuinely good idea in that project: instead of packing a snapshot bundle into
the prompt and uploading it, expose a small set of READ-ONLY tools and let the
model pull what it needs. For a review lane that is strictly better — the
reviewer reads the real file at the real path instead of a snapshot someone
else chose — and it deletes the most fragile subsystem this lane owns (upload
readiness calibration, chip identity, stall DOM dumps).

Two properties are the whole point, and both are theirs:

1. **Read-only by construction.** There is no write, shell, patch or commit
   function in this module. Not "disabled" — absent. Prompt injection cannot
   reach a capability that does not exist, which is a much stronger claim than
   a permission check.
2. **Containment is canonical, not textual.** Every path is resolved by
   realpath-ing its deepest existing ancestor before the inside-the-root check,
   so a symlink out of the tree, a ``..`` walk, an absolute path and a
   not-yet-existing leaf all fail the same way. On top of that a sensitive-file
   policy (``.env*`` except ``.env.example``, keys, SSH/AWS/GnuPG dirs, netrc,
   credential JSON, cookie stores) is denied outright, and a noise policy hides
   build output and VCS internals from listings and search.

What is NOT ported: their Cloudflare tunnel and connector-repair machinery,
their OAuth 2.1 server, and their Codex-harness execution tools. This module is
the local capability; how it is exposed (a CAO MCP tool group today, a
registered ChatGPT connector once the account's Developer mode is on) is a
separate decision made by the caller.

Everything here is stdlib and pure filesystem/git — no browser, no database, no
tmux — so the MCP boundary's HTTP-only rule is untouched and the tests are
plain tmp_path tests.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Never readable through this surface, whatever the caller asks for. Matched
#: with gitignore-ish semantics against the workspace-RELATIVE path.
SENSITIVE_PATTERNS: Tuple[str, ...] = (
    ".env",
    ".env.*",
    "!.env.example",
    "!.env.sample",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "id_ecdsa",
    "id_ecdsa.*",
    "id_dsa",
    "id_dsa.*",
    ".ssh/",
    ".aws/",
    ".gnupg/",
    ".npmrc",
    ".netrc",
    "_netrc",
    ".git-credentials",
    "*.keychain",
    "*.keychain-db",
    "credentials.json",
    "service-account*.json",
    "secrets.json",
    "cookies.sqlite",
    "Cookies",
    # CAO-specific additions: the two credential stores this fork actually has
    # on disk next to a workspace, plus the ChatGPT-web profile export.
    "providers.toml",
    "session-export.json",
    "*.token",
    "sudo_passwd*",
)

#: Hidden from listing and search (not an error — just noise).
NOISE_PATTERNS: Tuple[str, ...] = (
    ".git/",
    "node_modules/",
    "dist/",
    "build/",
    "out/",
    ".next/",
    "coverage/",
    ".cache/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "target/",
    ".gradle/",
    ".idea/",
    ".DS_Store",
    "*.lock",
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
)

#: Per-workspace extra rules, in .gitignore syntax (their ``.c2cignore``).
IGNORE_FILE = ".caoignore"

#: Read caps. A pull tool that can return a gigabyte is a context bomb.
MAX_READ_BYTES = 256_000
DEFAULT_READ_LINES = 400
MAX_SEARCH_RESULTS = 200
MAX_LIST_ENTRIES = 1000
MAX_DIFF_BYTES = 262_144
_BINARY_SNIFF_BYTES = 8192


class WorkspaceError(Exception):
    """A typed, enumerable refusal. ``code`` is the wire value."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


CODE_INVALID_PATH = "INVALID_PATH"
CODE_OUTSIDE = "PATH_OUTSIDE_WORKSPACE"
CODE_SENSITIVE = "ACCESS_DENIED_SENSITIVE_FILE"
CODE_NOT_FOUND = "FILE_NOT_FOUND"
CODE_NOT_A_DIRECTORY = "NOT_A_DIRECTORY"
CODE_NOT_A_FILE = "NOT_A_FILE"
CODE_TOO_LARGE = "FILE_TOO_LARGE"
CODE_BINARY = "BINARY_FILE"


def _match_one(pattern: str, rel: str) -> bool:
    """gitignore-ish match of one pattern against a relative POSIX path.

    Supported: plain names, globs, ``dir/`` (the directory and everything under
    it), and a leading ``/`` to anchor at the root. Patterns without a slash
    match any path SEGMENT, which is what makes ``.env`` deny ``config/.env``.
    """
    rel = rel.strip("/")
    if not pattern:
        return False
    anchored = pattern.startswith("/")
    pattern = pattern.lstrip("/")
    dir_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    if dir_only:
        # Match the directory itself and anything beneath it.
        if anchored:
            return rel == pattern or rel.startswith(pattern + "/")
        parts = rel.split("/")
        return any(fnmatch.fnmatch(p, pattern) for p in parts[:-1]) or fnmatch.fnmatch(
            parts[-1], pattern
        )
    if "/" in pattern or anchored:
        return fnmatch.fnmatch(rel, pattern) or rel.startswith(pattern.rstrip("*") + "/")
    return any(fnmatch.fnmatch(part, pattern) for part in rel.split("/"))


class IgnoreRules:
    """Sensitive / noise / custom rule sets with negation support."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.sensitive = list(SENSITIVE_PATTERNS)
        self.noise = list(NOISE_PATTERNS)
        self.custom: List[str] = []
        if root is not None:
            try:
                text = (Path(root) / IGNORE_FILE).read_text(encoding="utf-8")
            except OSError:
                text = ""
            self.custom = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]

    @staticmethod
    def _matches(patterns: Sequence[str], rel: str) -> bool:
        """Last matching rule wins; a ``!`` rule un-matches (gitignore order)."""
        decision = False
        for pattern in patterns:
            if pattern.startswith("!"):
                if _match_one(pattern[1:], rel):
                    decision = False
            elif _match_one(pattern, rel):
                decision = True
        return decision

    def is_sensitive(self, rel: str) -> bool:
        if not rel or rel == ".":
            return False
        return self._matches(self.sensitive, rel) or self._matches(self.custom, rel)

    def is_noise(self, rel: str) -> bool:
        if not rel or rel == ".":
            return False
        return self._matches(self.noise, rel)

    def is_hidden(self, rel: str) -> bool:
        return self.is_sensitive(rel) or self.is_noise(rel)


@dataclass(frozen=True)
class ResolvedPath:
    abs_path: Path
    rel: str


@dataclass
class Workspace:
    """One contained, read-only view of a directory tree."""

    root: Path
    rules: IgnoreRules = field(init=False)

    def __init__(self, root: Any) -> None:
        resolved = Path(root).expanduser()
        try:
            real = resolved.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(CODE_NOT_FOUND, f"workspace root does not exist: {root}") from exc
        if not real.is_dir():
            raise WorkspaceError(CODE_NOT_A_DIRECTORY, f"workspace root is not a directory: {root}")
        self.root = real
        self.rules = IgnoreRules(real)

    # ── containment ──────────────────────────────────────────────────────

    def _canonicalize(self, candidate: Path) -> Path:
        """Realpath the deepest EXISTING ancestor, then re-append the rest.

        ``Path.resolve()`` on a missing leaf already does this on POSIX, but
        doing it explicitly documents the defence and keeps the behaviour if a
        future caller passes a path whose parent is a symlink out of the tree.
        """
        suffix: List[str] = []
        current = candidate
        while True:
            try:
                real = current.resolve(strict=True)
                return real.joinpath(*reversed(suffix)) if suffix else real
            except (OSError, RuntimeError):
                parent = current.parent
                if parent == current:
                    return candidate
                suffix.append(current.name)
                current = parent

    def resolve(self, requested: str, *, allow_sensitive: bool = False) -> ResolvedPath:
        """Resolve an UNTRUSTED path to a canonical path inside the workspace."""
        if not isinstance(requested, str) or "\0" in requested:
            raise WorkspaceError(CODE_INVALID_PATH, "invalid path")
        candidate = requested.strip().replace("\\", "/")
        candidate = re.sub(r"^workspace:/*", "", candidate, flags=re.IGNORECASE)
        if candidate in ("", "/"):
            candidate = "."
        absolute = (
            (self.root / candidate).resolve()
            if not Path(candidate).is_absolute()
            else Path(candidate)
        )
        canonical = self._canonicalize(absolute)
        try:
            rel_path = canonical.relative_to(self.root)
        except ValueError:
            raise WorkspaceError(
                CODE_OUTSIDE, f"path resolves outside the workspace: {requested}"
            ) from None
        rel = rel_path.as_posix()
        rel = "" if rel == "." else rel
        if not allow_sensitive and rel and self.rules.is_sensitive(rel):
            raise WorkspaceError(
                CODE_SENSITIVE,
                f"{rel!r} matches the sensitive-file policy and cannot be read",
            )
        return ResolvedPath(abs_path=canonical, rel=rel)

    # ── reads ────────────────────────────────────────────────────────────

    def info(self) -> Dict[str, Any]:
        """Identity and shape of the workspace — the "call this first" tool."""
        entries = self.list_directory(".", depth=1, limit=200)
        return {
            "root_name": self.root.name,
            "entry_count": len(entries["entries"]),
            "is_git": (self.root / ".git").exists(),
            "has_ignore_file": (self.root / IGNORE_FILE).exists(),
            "read_only": True,
            "sensitive_policy": "deny",
            "top_level": [e["path"] for e in entries["entries"][:60]],
        }

    def list_directory(
        self, path: str = ".", *, depth: int = 1, limit: int = 200, offset: int = 0
    ) -> Dict[str, Any]:
        depth = max(1, min(int(depth), 4))
        limit = max(1, min(int(limit), MAX_LIST_ENTRIES))
        target = self.resolve(path)
        if not target.abs_path.exists():
            raise WorkspaceError(CODE_NOT_FOUND, f"no such path: {path}")
        if not target.abs_path.is_dir():
            raise WorkspaceError(CODE_NOT_A_DIRECTORY, f"not a directory: {path}")
        collected: List[Dict[str, Any]] = []
        for entry in self._walk(target.abs_path, depth):
            rel = entry.relative_to(self.root).as_posix()
            if self.rules.is_hidden(rel):
                continue
            try:
                is_dir = entry.is_dir()
                size = 0 if is_dir else entry.stat().st_size
            except OSError:
                continue
            collected.append({"path": rel, "type": "dir" if is_dir else "file", "bytes": size})
        collected.sort(key=lambda e: e["path"])
        window = collected[offset : offset + limit]
        return {
            "path": target.rel or ".",
            "entries": window,
            "total": len(collected),
            "has_more": offset + len(window) < len(collected),
            "next_offset": offset + len(window),
        }

    def _walk(self, start: Path, depth: int) -> Iterable[Path]:
        stack: List[Tuple[Path, int]] = [(start, 0)]
        while stack:
            current, level = stack.pop()
            try:
                children = sorted(current.iterdir())
            except OSError:
                continue
            for child in children:
                rel = child.relative_to(self.root).as_posix()
                if self.rules.is_noise(rel):
                    continue
                yield child
                if child.is_dir() and level + 1 < depth:
                    stack.append((child, level + 1))

    def read_file(
        self,
        path: str,
        *,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        max_bytes: int = MAX_READ_BYTES,
    ) -> Dict[str, Any]:
        target = self.resolve(path)
        if not target.abs_path.exists():
            raise WorkspaceError(CODE_NOT_FOUND, f"no such file: {path}")
        if target.abs_path.is_dir():
            raise WorkspaceError(CODE_NOT_A_FILE, f"not a file: {path}")
        size = target.abs_path.stat().st_size
        head = target.abs_path.open("rb").read(_BINARY_SNIFF_BYTES)
        if b"\0" in head:
            raise WorkspaceError(CODE_BINARY, f"binary file: {path}")
        raw = target.abs_path.read_bytes()[: max(1, min(int(max_bytes), MAX_READ_BYTES))]
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        first = max(1, int(start_line or 1))
        last = int(end_line) if end_line else first + DEFAULT_READ_LINES - 1
        window = lines[first - 1 : last]
        return {
            "path": target.rel,
            "start_line": first,
            "end_line": first + len(window) - 1 if window else first,
            "total_lines": len(lines),
            "bytes": size,
            "truncated": size > len(raw) or last < len(lines),
            "content": "\n".join(window),
        }

    def search(
        self,
        query: str,
        *,
        path: str = ".",
        glob: Optional[str] = None,
        limit: int = 50,
        regex: bool = False,
    ) -> Dict[str, Any]:
        if not isinstance(query, str) or len(query.strip()) < 2:
            raise WorkspaceError(CODE_INVALID_PATH, "query must be at least 2 characters")
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        target = self.resolve(path)
        pattern = re.compile(query if regex else re.escape(query))
        matches: List[Dict[str, Any]] = []
        for file_path in self._walk(target.abs_path if target.abs_path.is_dir() else self.root, 4):
            if len(matches) >= limit:
                break
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(self.root).as_posix()
            if self.rules.is_hidden(rel):
                continue
            if glob and not fnmatch.fnmatch(file_path.name, glob):
                continue
            try:
                if b"\0" in file_path.open("rb").read(_BINARY_SNIFF_BYTES):
                    continue
                with file_path.open("r", encoding="utf-8", errors="replace") as fh:
                    for number, line in enumerate(fh, start=1):
                        if pattern.search(line):
                            matches.append(
                                {"path": rel, "line": number, "text": line.rstrip()[:300]}
                            )
                            if len(matches) >= limit:
                                break
            except OSError:
                continue
        return {"query": query, "matches": matches, "truncated": len(matches) >= limit}

    # ── git (read-only plumbing) ─────────────────────────────────────────

    def _git(self, args: Sequence[str], *, max_bytes: int = MAX_DIFF_BYTES) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkspaceError(CODE_NOT_FOUND, f"git unavailable: {type(exc).__name__}") from exc
        return proc.stdout[:max_bytes]

    def git_status(self) -> Dict[str, Any]:
        branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"], max_bytes=200).strip()
        porcelain = self._git(["status", "--porcelain=v1", "--untracked-files=normal"])
        staged, unstaged, untracked = [], [], []
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            index_state, worktree_state, name = line[0], line[1], line[3:]
            if self.rules.is_sensitive(name):
                # A sensitive path's NAME is itself a hint; keep it out.
                continue
            if index_state == "?" and worktree_state == "?":
                untracked.append(name)
                continue
            if index_state not in (" ", "?"):
                staged.append(name)
            if worktree_state not in (" ", "?"):
                unstaged.append(name)
        return {
            "branch": branch,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
        }

    def git_diff(
        self,
        *,
        mode: str = "unstaged",
        path: Optional[str] = None,
        offset: int = 0,
        max_bytes: int = 65_536,
    ) -> Dict[str, Any]:
        if mode not in ("unstaged", "staged", "head"):
            raise WorkspaceError(CODE_INVALID_PATH, f"unknown diff mode: {mode}")
        args = ["diff"]
        if mode == "staged":
            args.append("--cached")
        elif mode == "head":
            args.append("HEAD")
        if path:
            target = self.resolve(path)
            args.extend(["--", target.rel or "."])
        text = self._git(args)
        max_bytes = max(1024, min(int(max_bytes), MAX_DIFF_BYTES))
        window = text[offset : offset + max_bytes]
        return {
            "mode": mode,
            "offset": offset,
            "bytes": len(window),
            "has_more": offset + len(window) < len(text),
            "next_offset": offset + len(window),
            "diff": window,
        }


def assert_exposable(path: Any, *, root: Optional[Any] = None) -> Path:
    """Refuse to expose a sensitive file to the model — the runner's hook.

    The bundle the ChatGPT-web lane uploads is chosen by a caller, so the
    sensitive-file policy has to bite on THAT path too, not only on the pull
    tools: "attach this file" must not be a way around "you may not read
    ``.env``". Raises :class:`WorkspaceError` with the same codes.
    """
    candidate = Path(path).expanduser()
    rules = IgnoreRules(Path(root) if root else None)
    rel = candidate.name
    if root is not None:
        try:
            rel = candidate.resolve().relative_to(Path(root).resolve()).as_posix()
        except (OSError, ValueError):
            rel = candidate.name
    if rules.is_sensitive(rel) or rules.is_sensitive(candidate.name):
        raise WorkspaceError(
            CODE_SENSITIVE, f"{rel!r} matches the sensitive-file policy and cannot be exposed"
        )
    return candidate
