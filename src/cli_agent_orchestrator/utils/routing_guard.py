"""Routing-violation rule for dispatches (F754, issue #611 scope add).

Canonical copy. ``orchestrator/routing.toml`` is the SOLE authority on which
lane fills which position, and it carries the user's word. A dispatch naming a
legacy provider-named profile (``codex_dev``, ``kiro_dev``, ``kiro_oracle``,
``grok_dev``, ...) implies that profile's provider — so when routing.toml binds
the position that profile fills to a DIFFERENT provider, or to an in-harness
lane with no CAO cell at all, the dispatch contradicts the store.

Mechanizes the M34-class mistake recorded in routing.toml's own status log
(2026-09-04): three ``kiro_dev`` lanes plus ``kiro_oracle`` were dispatched
against a ``dev = in_harness`` binding, and all four had to be reaped and
re-dispatched.

Stdlib-only, for the same reason as :mod:`terminal_id_scan`: the root repo's
PreToolUse hook carries a byte-identical twin and must not import the fork.
"""

# --- BEGIN SHARED ROUTING GUARD (F754) ---
# This block is BYTE-IDENTICAL in both copies of the routing rule set:
#   fork: src/cli_agent_orchestrator/utils/routing_guard.py
#   root: .claude/hooks/lib/routing_guard.py
# Same arrangement, and same reason, as terminal_id_scan.py's shared block: the
# PreToolUse hook must not import the fork, so the rule lives once and is
# generated into both places. Drift is caught from both sides by
# test_shared_blocks_are_identical.
import re
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence

ROUTING_ERROR_CODE = "E-ROUTING-VIOLATION"

# ---------------------------------------------------------------------------
# profile -> position
# ---------------------------------------------------------------------------
# Two sources, in this order:
#
# 1. The profile's own ``extends:`` frontmatter key. A composed alias stub names
#    the position it fills (``kiro_oracle`` extends ``oracle``,
#    ``codex_empirical_reviewer`` extends ``empirical_reviewer``, ...). This is
#    real metadata and is preferred wherever it exists.
#
# 2. This explicit table, for profiles that carry NO ``extends``. ``role:`` is
#    NOT usable for this — every worker profile in the repo declares
#    ``role: developer``, including the oracle and the gates, so it says nothing
#    about position. The headline case (``codex_dev`` -> ``dev``) has no
#    ``extends``, so the table is required, not a nicety.
#
# Anything this pair cannot map with confidence is NOT guessed: an unmapped
# profile is allowed through. A guard that blocks a dispatch it does not
# understand is worse than the bug it prevents.
PROFILE_POSITION_TABLE: Dict[str, str] = {
    "cline_dev": "dev",
    "codex_dev": "dev",
    "grok_dev": "dev",
    "kiro_dev": "dev",
    "grok_tester": "tester",
    "claude_design_reviewer": "design_reviewer",
}

# Deliberately UNMAPPED, with the reason, so the omission reads as a decision
# rather than an oversight:
#   grok_reviewer          - "reviewer" does not say WHICH gate. kiro_reviewer
#                            settles it with `extends: empirical_reviewer`;
#                            grok_reviewer carries no extends and the name is
#                            ambiguous between the DESIGN and EMPIRICAL gates.
#   claude_blueprint_maker,
#   claude_blueprint_maker_tester
#                          - the maker lane fills no routed position.
#   chao_supervisor        - the seat itself; it is not dispatched into a
#                            position.
#   developer-opus,
#   developer-sonnet       - harness lanes, not CAO cells.
# (These three are listed explicitly rather than relying on "has no provider":
#  the repo's profiles/ copies carry no `provider:`, but the INSTALLED
#  agent-store copies do — verified 2026-09-04, where all three read
#  `provider: kiro_cli`. The guard reads the installed store, so the exclusion
#  has to hold on the artifact it actually parses.)
UNMAPPED_BY_DESIGN = frozenset(
    {
        "grok_reviewer",
        "claude_blueprint_maker",
        "claude_blueprint_maker_tester",
        "chao_supervisor",
        "developer-opus",
        "developer-sonnet",
    }
)

_FRONTMATTER_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):[ \t]*(?P<value>.*)$")


class ProfileMeta(NamedTuple):
    """The three frontmatter facts the routing rule needs."""

    name: str
    provider: Optional[str]
    extends: Optional[str]


def parse_profile_frontmatter(text: str, fallback_name: str) -> ProfileMeta:
    """Read ``name`` / ``provider`` / ``extends`` from a profile's frontmatter.

    Deliberately narrow: top-level scalar keys only, from the leading ``---``
    block. Profiles nest under ``mcpServers`` and ``contextPolicy``, so a nested
    ``provider:`` or ``command:`` must never be read as a top-level key.

    The ONE thing that rejects those is the ``^`` anchor in
    :data:`_FRONTMATTER_LINE_RE`: an indented line cannot match it, so it falls
    through the ``continue`` below. Allowing leading whitespace there would make
    every nested key a top-level one. (An explicit indent test used to sit in
    this loop as well; mutation testing showed no test could tell it from its
    absence, because the anchor already did the work, so it was removed rather
    than left as decorative defence.)

    Quotes are stripped; anything absent is ``None``.
    """
    name = fallback_name
    provider: Optional[str] = None
    extends: Optional[str] = None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ProfileMeta(name, provider, extends)
    for raw in lines[1:]:
        if raw.strip() == "---":
            break
        match = _FRONTMATTER_LINE_RE.match(raw)
        if not match:
            continue
        key = match.group("key")
        value = match.group("value").strip().strip("\"'").strip()
        if not value:
            continue
        if key == "name":
            name = value
        elif key == "provider":
            provider = value
        elif key == "extends":
            extends = value
    return ProfileMeta(name, provider, extends)


def position_for_profile(
    meta: ProfileMeta, known_positions: Optional[Sequence[str]] = None
) -> Optional[str]:
    """The position a profile fills, or ``None`` when it cannot be known.

    ``known_positions`` (when given) gates the ``extends`` route: a profile that
    extends another PROFILE rather than a position contributes nothing here.
    """
    if meta.name in UNMAPPED_BY_DESIGN:
        return None
    if meta.extends:
        if known_positions is None or meta.extends in known_positions:
            return meta.extends
    return PROFILE_POSITION_TABLE.get(meta.name)


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------


def _render_bound_targets(rows: Sequence[Mapping[str, object]]) -> str:
    targets: List[str] = []
    for row in rows:
        if row.get("kind") == "in_harness":
            model = row.get("model")
            targets.append(f"in_harness (model={model})" if model else "in_harness")
        elif row.get("provider"):
            targets.append(str(row.get("provider")))
    seen: List[str] = []
    for target in targets:
        if target not in seen:
            seen.append(target)
    return ", ".join(seen) if seen else "nothing"


def routing_violation(
    profile: str,
    implied_provider: Optional[str],
    position: Optional[str],
    bindings: Sequence[Mapping[str, object]],
    action: str = "dispatch",
) -> Optional[str]:
    """Refusal text when a legacy profile contradicts routing.toml, else ``None``.

    The rule: a legacy provider-named profile implies its own provider. If the
    position it fills is bound to a DIFFERENT provider, or to an in-harness lane
    with no CAO cell at all, the dispatch contradicts the routing store and is
    refused. routing.toml is the sole authority on which lane fills which
    position, and it is the user's word — a dispatch that walks around it is the
    M34-class mistake this guard mechanizes (the 2026-09-04 incident: three
    kiro_dev lanes plus kiro_oracle dispatched against ``dev = in_harness``).

    Never refuses when it cannot be sure: no position, no implied provider, or a
    position routing.toml does not bind at all.
    """
    if not position or not implied_provider:
        return None
    rows = [row for row in bindings if row.get("position") == position]
    if not rows:
        return None  # position not bound — nothing to contradict
    for row in rows:
        if row.get("kind") == "cao" and row.get("provider") == implied_provider:
            return None  # this exact cell is bound; legitimate
    bound = _render_bound_targets(rows)
    return "\n".join(
        [
            f"{ROUTING_ERROR_CODE}: profile {profile} implies provider "
            f"{implied_provider} for position {position}; routing.toml binds "
            f"{position} -> {bound}",
            f"No {action} was sent. routing.toml is the sole authority on which lane fills",
            "which position, and it carries the user's word. Dispatch the POSITION name",
            f'("{position}") and let routing resolve the lane, or pick the profile whose',
            f"provider matches the binding — do not route around the store.",
        ]
    )


# --- END SHARED ROUTING GUARD (F754) ---
