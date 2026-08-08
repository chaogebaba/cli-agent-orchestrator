"""Resolve provider binaries to absolute paths with fallback directories.

Under systemd PATH or minimal container environments, bare binary names
like ``"codex"`` may not be in PATH. This module provides a resolution
helper that checks ``shutil.which``, common user-local bin directories,
and an explicit environment-variable override before falling back to the
bare name (which will produce a clear ``FileNotFoundError`` from the OS).
"""

import os
import shutil


def resolve_provider_binary(name: str) -> str:
    """Resolve provider binary to absolute path with fallback dirs.

    Resolution order:
    1. ``shutil.which(name)`` — standard PATH lookup.
    2. Common user-local bin directories (expanded from ``~``).
    3. Environment variable ``CAO_<NAME>_PATH`` (explicit override).
    4. Bare ``name`` as last resort — lets the OS produce a clear error.
    """
    resolved = shutil.which(name)
    if resolved:
        return resolved
    for fallback in (
        "~/.bun/bin",
        "~/.local/bin",
        "~/.cargo/bin",
        "~/.nix-profile/bin",
    ):
        candidate = os.path.expanduser(os.path.join(fallback, name))
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    env_key = f"CAO_{name.upper().replace('-', '_')}_PATH"
    env_val = os.environ.get(env_key)
    if env_val and os.path.isfile(env_val):
        return env_val
    return name  # last resort — let it fail loudly with clear error
