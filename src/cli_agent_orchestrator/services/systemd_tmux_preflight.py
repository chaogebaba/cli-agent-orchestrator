"""F137 — systemd tmux-scope activation preflight service.

Pure, testable service that validates the host's systemd configuration
is safe for CAO activation. Read-only, fail-closed, never mutates.

Checks:
- systemd version classification (259.* = known_unsafe)
- Prefix drop-in prohibition (tmux-spawn-.scope.d/*.conf)
- Aggregate app.slice property contract (10G/12G/6G + ManagedOOMSwap=auto)
- Manager responsiveness (bounded timeouts)
- Job-list diagnostic (warning-only)
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, Sequence


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivationPreflightCheck:
    """Single preflight check result."""

    code: str
    ok: bool
    observed: str | int | bool | tuple[str, ...] | None = None
    expected: str | int | bool | tuple[str, ...] | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ActivationPreflightResult:
    """Aggregate preflight outcome."""

    ok: bool
    mode: Literal["aggregate_only"]
    version: str | None
    version_policy: Literal["known_unsafe", "unqualified"]
    enabled_prefix_dropins: tuple[str, ...]
    checks: tuple[ActivationPreflightCheck, ...]


# ---------------------------------------------------------------------------
# Command runner protocol + default implementation
# ---------------------------------------------------------------------------

# Mutation commands that must NEVER appear in any subprocess call.
_DENIED_COMMANDS: frozenset[str] = frozenset(
    {
        "systemd-run",
        "busctl",
    }
)

_DENIED_SYSTEMCTL_VERBS: frozenset[str] = frozenset(
    {
        "set-property",
        "daemon-reload",
        "daemon-reexec",
        "start",
        "stop",
        "restart",
        "kill",
        "reset-failed",
        "cancel",
    }
)


class CommandRunner(Protocol):
    """Injectable command execution interface."""

    def run(
        self, args: Sequence[str], *, timeout: float = 5.0
    ) -> subprocess.CompletedProcess[str]: ...


class DefaultCommandRunner:
    """Production runner — subprocess with timeout, never shell=True."""

    def run(
        self, args: Sequence[str], *, timeout: float = 5.0
    ) -> subprocess.CompletedProcess[str]:
        _enforce_allowlist(args)
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


def _enforce_allowlist(args: Sequence[str]) -> None:
    """Raise if args would invoke a denied mutation command."""
    if not args:
        raise ValueError("empty command")
    cmd = Path(args[0]).name
    if cmd in _DENIED_COMMANDS:
        raise PermissionError(f"F137: denied command: {cmd}")
    if cmd == "systemctl":
        for arg in args[1:]:
            if arg.startswith("-"):
                continue
            if arg in _DENIED_SYSTEMCTL_VERBS:
                raise PermissionError(f"F137: denied systemctl verb: {arg}")
            break


# ---------------------------------------------------------------------------
# Aggregate contract constants
# ---------------------------------------------------------------------------

_EXPECTED_MEMORY_HIGH: int = 10 * 1024**3  # 10737418240
_EXPECTED_MEMORY_MAX: int = 12 * 1024**3  # 12884901888
_EXPECTED_MEMORY_SWAP_MAX: int = 6 * 1024**3  # 6442450944
_EXPECTED_MANAGED_OOM_SWAP: str = "auto"

_DROPIN_DIR_NAME: str = "tmux-spawn-.scope.d"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"systemd\s+(\d+(?:\.\d+)*)")


def _parse_version(output: str) -> tuple[str | None, Literal["known_unsafe", "unqualified"], bool]:
    """Extract version string and classify policy.

    Returns (version_string, policy, parse_ok).
    """
    first_line = output.strip().split("\n", 1)[0] if output.strip() else ""
    m = _VERSION_RE.search(first_line)
    if not m:
        return None, "unqualified", False
    ver = m.group(1)
    major = ver.split(".")[0]
    policy: Literal["known_unsafe", "unqualified"] = (
        "known_unsafe" if major == "259" else "unqualified"
    )
    return ver, policy, True


def _parse_unit_paths(output: str) -> list[str] | None:
    """Parse systemd-analyze --user unit-paths output into a list of paths.

    Returns None if output is empty/malformed.
    """
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    return lines if lines else None


def _scan_prefix_dropins(unit_paths: list[str]) -> tuple[str, ...]:
    """Scan for active .conf files in tmux-spawn-.scope.d/ under each unit path.

    Returns sorted tuple of absolute paths to offending files.
    """
    found: list[str] = []
    for root in unit_paths:
        dropin_dir = Path(root) / _DROPIN_DIR_NAME
        if not dropin_dir.is_dir():
            continue
        try:
            for entry in sorted(dropin_dir.iterdir()):
                if entry.name.endswith(".conf"):
                    found.append(str(entry))
        except PermissionError:
            # Unreadable directory — fail closed by reporting as if a conf exists
            found.append(str(dropin_dir / "<unreadable>"))
    return tuple(sorted(found))


def _parse_property_value(output: str, prop_name: str) -> str | None:
    """Parse 'PropertyName=value' from systemctl show output.

    If the property appears more than once (duplicate scalar), fail closed by
    returning None — AC3 requires explicit failure on duplicated properties.
    """
    prefix = f"{prop_name}="
    found: str | None = None
    for line in output.strip().splitlines():
        if line.startswith(prefix):
            if found is not None:
                # Duplicate scalar property — fail closed
                return None
            found = line[len(prefix) :]
    return found


def _normalize_bytes(value: str) -> int | None:
    """Convert a systemd byte value to int. Returns None for infinity/unparsable."""
    if value.strip().lower() == "infinity":
        return None
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_activation_preflight(
    runner: CommandRunner | None = None,
    deadline: float = 15.0,
) -> ActivationPreflightResult:
    """Execute the full activation preflight.

    Returns a structured result. Never mutates the system.
    All failures are fail-closed (preflight fails, activation blocked).
    """
    if runner is None:
        runner = DefaultCommandRunner()

    checks: list[ActivationPreflightCheck] = []
    start = time.monotonic()
    version: str | None = None
    version_policy: Literal["known_unsafe", "unqualified"] = "unqualified"
    enabled_dropins: tuple[str, ...] = ()

    def _remaining() -> float:
        return max(0.1, deadline - (time.monotonic() - start))

    # --- Check 1: version parse ---
    try:
        result = runner.run(["systemctl", "--version"], timeout=min(5.0, _remaining()))
        ver, policy, parsed = _parse_version(result.stdout)
        version = ver
        version_policy = policy
        if parsed:
            checks.append(
                ActivationPreflightCheck(
                    code="version_parse",
                    ok=True,
                    observed=ver,
                    detail=f"policy={policy}",
                )
            )
        else:
            checks.append(
                ActivationPreflightCheck(
                    code="version_parse",
                    ok=False,
                    observed=result.stdout[:200] if result.stdout else None,
                    detail="failed to parse systemd version from output",
                )
            )
    except subprocess.TimeoutExpired:
        checks.append(
            ActivationPreflightCheck(
                code="version_parse",
                ok=False,
                detail="systemctl --version timed out",
            )
        )
        checks.append(
            ActivationPreflightCheck(
                code="manager_unresponsive",
                ok=False,
                detail="systemctl --version timed out",
            )
        )
    except (FileNotFoundError, PermissionError) as exc:
        checks.append(
            ActivationPreflightCheck(
                code="version_parse",
                ok=False,
                detail=f"command unavailable: {exc}",
            )
        )

    # --- Check 2: prefix drop-in scan ---
    try:
        result = runner.run(
            ["systemd-analyze", "--user", "unit-paths"],
            timeout=min(5.0, _remaining()),
        )
        unit_paths = _parse_unit_paths(result.stdout)
        if unit_paths is None:
            checks.append(
                ActivationPreflightCheck(
                    code="prefix_dropin_scan",
                    ok=False,
                    detail="systemd-analyze --user unit-paths returned empty/malformed output",
                )
            )
        else:
            enabled_dropins = _scan_prefix_dropins(unit_paths)
            if enabled_dropins:
                checks.append(
                    ActivationPreflightCheck(
                        code="prefix_dropin_scan",
                        ok=False,
                        observed=enabled_dropins,
                        expected=(),
                        detail=f"active prefix drop-ins block activation: {', '.join(enabled_dropins)}",
                    )
                )
            else:
                checks.append(
                    ActivationPreflightCheck(
                        code="prefix_dropin_scan",
                        ok=True,
                        observed=(),
                        detail=f"scanned {len(unit_paths)} unit path roots",
                    )
                )
    except subprocess.TimeoutExpired:
        checks.append(
            ActivationPreflightCheck(
                code="prefix_dropin_scan",
                ok=False,
                detail="systemd-analyze --user unit-paths timed out",
            )
        )
        checks.append(
            ActivationPreflightCheck(
                code="manager_unresponsive",
                ok=False,
                detail="systemd-analyze timed out",
            )
        )
    except (FileNotFoundError, PermissionError) as exc:
        checks.append(
            ActivationPreflightCheck(
                code="prefix_dropin_scan",
                ok=False,
                detail=f"command unavailable: {exc}",
            )
        )

    # --- Checks 3-6: aggregate slice properties ---
    aggregate_props = [
        ("MemoryHigh", _EXPECTED_MEMORY_HIGH, "aggregate_memory_high"),
        ("MemoryMax", _EXPECTED_MEMORY_MAX, "aggregate_memory_max"),
        ("MemorySwapMax", _EXPECTED_MEMORY_SWAP_MAX, "aggregate_memory_swap_max"),
    ]

    try:
        props_to_query = "MemoryHigh,MemoryMax,MemorySwapMax,ManagedOOMSwap"
        result = runner.run(
            ["systemctl", "--user", "show", "app.slice", f"-p{props_to_query}"],
            timeout=min(5.0, _remaining()),
        )

        for prop_name, expected_val, check_code in aggregate_props:
            raw = _parse_property_value(result.stdout, prop_name)
            if raw is None:
                checks.append(
                    ActivationPreflightCheck(
                        code=check_code,
                        ok=False,
                        observed=None,
                        expected=expected_val,
                        detail=f"{prop_name} not found in systemctl output",
                    )
                )
            else:
                actual = _normalize_bytes(raw)
                if actual is None:
                    checks.append(
                        ActivationPreflightCheck(
                            code=check_code,
                            ok=False,
                            observed=raw,
                            expected=expected_val,
                            detail=f"{prop_name}={raw} is infinity or unparsable",
                        )
                    )
                elif actual != expected_val:
                    checks.append(
                        ActivationPreflightCheck(
                            code=check_code,
                            ok=False,
                            observed=actual,
                            expected=expected_val,
                            detail=f"{prop_name} mismatch",
                        )
                    )
                else:
                    checks.append(
                        ActivationPreflightCheck(
                            code=check_code,
                            ok=True,
                            observed=actual,
                            expected=expected_val,
                        )
                    )

        # ManagedOOMSwap check
        oom_raw = _parse_property_value(result.stdout, "ManagedOOMSwap")
        if oom_raw is None:
            checks.append(
                ActivationPreflightCheck(
                    code="aggregate_managed_oom_swap",
                    ok=False,
                    observed=None,
                    expected=_EXPECTED_MANAGED_OOM_SWAP,
                    detail="ManagedOOMSwap not found in systemctl output",
                )
            )
        elif oom_raw.strip() != _EXPECTED_MANAGED_OOM_SWAP:
            checks.append(
                ActivationPreflightCheck(
                    code="aggregate_managed_oom_swap",
                    ok=False,
                    observed=oom_raw.strip(),
                    expected=_EXPECTED_MANAGED_OOM_SWAP,
                    detail=f"ManagedOOMSwap={oom_raw.strip()} != {_EXPECTED_MANAGED_OOM_SWAP}",
                )
            )
        else:
            checks.append(
                ActivationPreflightCheck(
                    code="aggregate_managed_oom_swap",
                    ok=True,
                    observed=oom_raw.strip(),
                    expected=_EXPECTED_MANAGED_OOM_SWAP,
                )
            )

    except subprocess.TimeoutExpired:
        for _, _, check_code in aggregate_props:
            checks.append(
                ActivationPreflightCheck(
                    code=check_code,
                    ok=False,
                    detail="systemctl --user show app.slice timed out",
                )
            )
        checks.append(
            ActivationPreflightCheck(
                code="aggregate_managed_oom_swap",
                ok=False,
                detail="systemctl --user show app.slice timed out",
            )
        )
        checks.append(
            ActivationPreflightCheck(
                code="manager_unresponsive",
                ok=False,
                detail="systemctl --user show app.slice timed out",
            )
        )
    except (FileNotFoundError, PermissionError) as exc:
        for _, _, check_code in aggregate_props:
            checks.append(
                ActivationPreflightCheck(
                    code=check_code,
                    ok=False,
                    detail=f"systemctl unavailable: {exc}",
                )
            )
        checks.append(
            ActivationPreflightCheck(
                code="aggregate_managed_oom_swap",
                ok=False,
                detail=f"systemctl unavailable: {exc}",
            )
        )

    # --- Check 7: jobs diagnostic (warning-only) ---
    try:
        result = runner.run(
            ["systemctl", "--user", "list-jobs", "--no-pager"],
            timeout=min(5.0, _remaining()),
        )
        jobs_output = result.stdout.strip()
        # systemctl list-jobs with no pending jobs prints "No jobs pending."
        # or an empty string depending on version
        has_jobs = bool(jobs_output) and "no jobs" not in jobs_output.lower()
        checks.append(
            ActivationPreflightCheck(
                code="jobs_diagnostic",
                ok=True,  # warning-only, never fails
                observed=has_jobs,
                detail=jobs_output[:500] if has_jobs else "no pending jobs",
            )
        )
    except subprocess.TimeoutExpired:
        checks.append(
            ActivationPreflightCheck(
                code="jobs_diagnostic",
                ok=False,
                detail="systemctl --user list-jobs timed out",
            )
        )
        # Also mark manager as unresponsive if not already
        if not any(c.code == "manager_unresponsive" for c in checks):
            checks.append(
                ActivationPreflightCheck(
                    code="manager_unresponsive",
                    ok=False,
                    detail="systemctl --user list-jobs timed out",
                )
            )
    except (FileNotFoundError, PermissionError) as exc:
        checks.append(
            ActivationPreflightCheck(
                code="jobs_diagnostic",
                ok=False,
                detail=f"systemctl unavailable: {exc}",
            )
        )

    # --- Assemble result ---
    all_ok = all(c.ok for c in checks)

    return ActivationPreflightResult(
        ok=all_ok,
        mode="aggregate_only",
        version=version,
        version_policy=version_policy,
        enabled_prefix_dropins=enabled_dropins,
        checks=tuple(checks),
    )
