"""Single socket-aware argv builder for every product tmux execution."""

from __future__ import annotations

import os


class TmuxSocketConfigurationError(RuntimeError):
    """The tmux socket binding is absent or malformed."""


def tmux_socket_name() -> str | None:
    value = os.environ.get("CAO_TMUX_SOCKET", "").strip()
    if not value:
        if os.environ.get("CAO_INSTANCE_ID"):
            raise TmuxSocketConfigurationError("CAO_TMUX_SOCKET is required in sandbox")
        return None
    if not value.startswith("cao-sbx-") or not value.replace("-", "").isalnum():
        raise TmuxSocketConfigurationError("invalid CAO_TMUX_SOCKET")
    return value


def tmux_argv(*args: str) -> list[str]:
    socket_name = tmux_socket_name()
    prefix = ["tmux"] if socket_name is None else ["tmux", "-L", socket_name]
    return [*prefix, *args]
