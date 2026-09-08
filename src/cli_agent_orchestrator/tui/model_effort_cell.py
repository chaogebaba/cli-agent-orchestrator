"""F826 (#683) D6 — render one MODEL / EFFORT cell as ``value [M]``.

Pure functions, no Textual dependency, so the cell/legend/detail logic is
unit-testable in isolation and importable anywhere. The projector
(``services/fleet_service.py``) hands the TUI two additive dicts per row
(``model_obs`` / ``effort_obs``) of shape::

    {value, marker, kind, event_time, source, validity, configured}

plus the configured DB value on the ``TerminalState`` (``resolved_model`` /
``reasoning_effort``). This module turns that into:

* the cell string ``value [M]`` (D1 markers L / R / S / C / ?), with ``!``
  appended to the marker on an observation-vs-configured CONFLICT (D1);
* a one-line LEGEND for the hints row;
* a details string for the selected row (kind, source, age, configured value).

Precedence L > R > S > C > ? is resolved by the PROJECTOR's marker on the
observation; this module only falls back to the configured ``[C]`` (or ``[?]``)
when there is no usable observation value, and never PROMOTES a marker.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
    "CELL_UNKNOWN",
    "LEGEND",
    "observation_cell",
    "observation_detail",
]

#: What an empty/unknown cell renders as (matches fleet_app.CELL_UNKNOWN).
CELL_UNKNOWN: str = "-"

#: The legend line shown in the hints row (D6). Terse; one line.
LEGEND: str = "model/effort: [L]ive [R]ecorded [S]tale [C]onfigured [?]unknown  !=conflict"


def _obs_value(obs: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not isinstance(obs, Mapping):
        return None
    value = obs.get("value")
    return value if isinstance(value, str) and value else None


def _obs_marker(obs: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not isinstance(obs, Mapping):
        return None
    marker = obs.get("marker")
    return marker if isinstance(marker, str) and marker else None


def _obs_configured(obs: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not isinstance(obs, Mapping):
        return None
    configured = obs.get("configured")
    return configured if isinstance(configured, str) and configured else None


def observation_cell(
    configured: Optional[str],
    obs: Optional[Mapping[str, Any]],
) -> str:
    """The ``value [M]`` cell text for one field.

    * A usable observation renders its own value + marker; when the observed
      value DIFFERS from the configured value a ``!`` is appended to the marker
      (D1 conflict), e.g. ``medium [L!]``. An alias like ``auto`` observed
      against a concrete configured value is NOT a conflict handled here — the
      projector marks genuine disagreements; this only appends ``!`` when both
      strings are present and differ.
    * No usable observation → the configured value ``[C]`` (D6 fallback), or
      ``- [?]`` when neither exists.
    """
    value = _obs_value(obs)
    marker = _obs_marker(obs)
    if value is not None and marker is not None and marker != "?":
        configured_val = _obs_configured(obs) if configured is None else configured
        conflict = marker in ("L", "R") and _values_conflict(value, configured_val)
        suffix = "!" if conflict else ""
        return f"{value} [{marker}{suffix}]"
    # Fallback: configured-only, or unknown.
    if configured:
        return f"{configured} [C]"
    return f"{CELL_UNKNOWN} [?]"


def _values_conflict(observed: Optional[str], configured: Optional[str]) -> bool:
    """Whether an observed value genuinely disagrees with the configured one (D1).

    Conservative on purpose: a status-line ``display_name`` ("Opus 4.6") is not
    the same string as a configured model id ("opus"), and an alias like
    ``auto`` is not a concrete model — neither is a real conflict (r2). So a
    conflict is flagged ONLY when both strings are present, neither is the alias
    ``auto``, and neither is a case-insensitive substring of the other. That
    catches ``sonnet`` vs ``opus`` while leaving id-vs-display-name and
    alias-vs-concrete alone.
    """
    if not observed or not configured:
        return False
    a = observed.strip().lower()
    b = configured.strip().lower()
    if not a or not b or a == b:
        return False
    if "auto" in (a, b):
        return False
    return a not in b and b not in a


def _fmt_age(event_time_ns: Any, now_ns: Optional[int]) -> Optional[str]:
    if not isinstance(event_time_ns, int) or now_ns is None:
        return None
    age_s = max(0.0, (now_ns - event_time_ns) / 1e9)
    if age_s < 90:
        return f"{age_s:.0f}s ago"
    return f"{age_s / 60:.0f}m ago"


def observation_detail(
    field_name: str,
    configured: Optional[str],
    obs: Optional[Mapping[str, Any]],
    *,
    now_ns: Optional[int] = None,
) -> str:
    """A one-line detail for the selected row (D1: kind shown in details).

    Shows the field, the observation kind + source + age when present, the
    configured value, and any validity reason (e.g. ``exited``, ``runtime
    effort unavailable from this provider``). Retains both configured and
    observed values so a conflict is legible (D1).
    """
    parts: list[str] = [f"{field_name}:"]
    value = _obs_value(obs)
    marker = _obs_marker(obs)
    if value is not None and marker is not None:
        seg = f"{value} [{marker}]"
        if isinstance(obs, Mapping):
            kind = obs.get("kind")
            if isinstance(kind, str) and kind:
                seg += f" {kind}"
            source = obs.get("source")
            if isinstance(source, str) and source:
                seg += f" from {source}"
            age = _fmt_age(obs.get("event_time"), now_ns)
            if age:
                seg += f" ({age})"
        parts.append(seg)
    else:
        parts.append(f"{configured or CELL_UNKNOWN} [{'C' if configured else '?'}]")
    if configured and value is not None and configured != value:
        parts.append(f"| configured {configured}")
    if isinstance(obs, Mapping):
        validity = obs.get("validity")
        if isinstance(validity, str) and validity:
            parts.append(f"| {validity}")
    return " ".join(parts)
