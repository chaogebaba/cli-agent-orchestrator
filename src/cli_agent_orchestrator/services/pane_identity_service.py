"""Provider-generic tmux pane identity authority checks."""

from __future__ import annotations

from typing import Any

PANE_IDENTITY_FAILURE_REASONS = frozenset(
    {"mismatch", "missing_env", "read_error", "pane_cardinality", "incarnation_changed"}
)


class PaneIdentityMismatchError(RuntimeError):
    """Raised before paste when the live pane cannot prove terminal ownership."""

    def __init__(self, reason: str) -> None:
        if reason not in PANE_IDENTITY_FAILURE_REASONS:
            raise ValueError(f"invalid pane identity mismatch reason: {reason}")
        self.reason = reason
        super().__init__(f"pane_identity_mismatch:{reason}")


def pane_identity_failure(terminal_id: str, metadata: dict[str, Any], backend: Any) -> str | None:
    """Return a closed failure reason, or None for success/unsupported backends."""
    if getattr(backend, "supports_identity_readback", False) is not True:
        return None
    result = backend.read_pane_identity(metadata["tmux_session"], metadata["tmux_window"])
    reason = getattr(result, "reason", None)
    if isinstance(reason, str):
        return reason if reason in PANE_IDENTITY_FAILURE_REASONS else "read_error"
    identity = getattr(result, "identity", None)
    return None if identity == terminal_id else "mismatch"
