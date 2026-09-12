"""Sensitive/noise ignore rules — port of ``src/workspace/ignore.ts``.

Upstream: XiaoDuoYa/codex-with-chatgpt at commit
8fdd97c188c7678d0d9c43b3769b426940de568a (MIT).  Behavioural port only: the
upstream uses the npm ``ignore`` package's gitignore semantics; this port uses
``pathspec``'s GitWildCardPattern matching, which implements the same
gitignore-style negation semantics.  The user-config file is renamed
``.c2cignore`` -> ``.caoignore`` (D5).
"""

from __future__ import annotations

from pathlib import Path

import pathspec

# Files that must never be readable through MCP, regardless of user config.
# Matched with gitignore semantics against workspace-relative paths.
SENSITIVE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "!.env.example",
    "!**/.env.example",
    "!.env.sample",
    "!**/.env.sample",
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
    ".ssh/**",
    ".aws/",
    ".aws/**",
    ".gnupg/",
    ".gnupg/**",
    ".npmrc",
    ".netrc",
    "_netrc",
    ".git-credentials",
    ".git/config",
    "*.keychain",
    "*.keychain-db",
    ".cloudflared/",
    "credentials.json",
    "service-account*.json",
    "secrets.json",
    "cookies.sqlite",
    "Cookies",
    # CAO addition (D5): the upstream's own secret file name is replaced by the
    # CAO equivalents; provider/session exports and password files are denied
    # per the blueprint's sensitive-file policy.
    ".c2c-secrets*",
    "*.session-export*",
    "*provider-export*",
    "*credential*.json",
    "*cookie*.json",
    "*passwd*.txt",
)

# High-noise directories excluded from listing/search by default.
NOISE_PATTERNS: tuple[str, ...] = (
    ".git/",
    "node_modules/",
    "dist/",
    "build/",
    "out/",
    ".next/",
    ".nuxt/",
    ".svelte-kit/",
    "coverage/",
    ".cache/",
    ".turbo/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    "target/",
    ".gradle/",
    ".idea/",
    ".tooling/",
    ".pnpm-store/",
    ".DS_Store",
    "*.lock",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
)


class IgnoreRules:
    """Sensitive / noise / custom (``.caoignore``) rule sets for one workspace."""

    def __init__(self, workspace_root: str | Path) -> None:
        root = Path(workspace_root)
        self._sensitive = pathspec.PathSpec.from_lines("gitwildmatch", SENSITIVE_PATTERNS)
        self._noise = pathspec.PathSpec.from_lines("gitwildmatch", NOISE_PATTERNS)
        custom_lines: list[str] = []
        try:
            caoignore = root / ".caoignore"
            if caoignore.is_file():
                custom_lines = caoignore.read_text(encoding="utf-8").splitlines()
        except OSError:
            # unreadable .caoignore: fall back to defaults only (upstream parity)
            custom_lines = []
        self._custom = pathspec.PathSpec.from_lines("gitwildmatch", custom_lines)

    def is_sensitive(self, rel_path: str) -> bool:
        """True when the path must be denied with ``ACCESS_DENIED_SENSITIVE_FILE``."""
        if not rel_path or rel_path == ".":
            return False
        return self._sensitive.match_file(rel_path) or self._custom.match_file(rel_path)

    def is_noise(self, rel_path: str) -> bool:
        """True when the path should be hidden from listing/search (not an error)."""
        if not rel_path or rel_path == ".":
            return False
        return self._noise.match_file(rel_path)

    def is_hidden(self, rel_path: str) -> bool:
        return self.is_sensitive(rel_path) or self.is_noise(rel_path)
