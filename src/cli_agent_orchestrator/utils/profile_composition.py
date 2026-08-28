"""F497 D5 — the profile composition merge engine (position + provider overlay).

The resolver in ``utils/agent_profiles.py`` (D2) calls into this module when a
profile declares ``position:``. Composition merges an ordered list of *layers*
(each a parsed frontmatter dict plus its markdown body) at the DICT level, then
constructs a single ``AgentProfile`` (D5: "merged at the DICT layer,
``AgentProfile`` constructed once"). Precedence is lowest → highest (D4):

  (2) positions/<pos>.md
  (3) overlays/<provider>.md
  (4) overlays/<provider>.<pos>.md

Layer (1) — the CLI built-in — is not a file layer here; it is whatever
``AgentProfile`` defaults to for unset fields. Layers (5)-(7) — providers.toml
``[provider]`` / ``[provider.profiles.<name>]`` / ``assign`` args — are applied
by ``settings_service`` / the provider modules on top of the composed profile,
unchanged (D4: "Layers 5-7 are today's chain unchanged").

The six merge-key classes (D5):

  1. PERSONA TEXT (the markdown body): append-only fragments. Each overlay body
     is appended under a ``## Provider notes (<provider>)`` heading UNLESS it
     declares ``replaces: ["<heading>"]``, in which case it replaces exactly the
     named section(s) in the composed body. A ``replaces:`` naming a heading
     ABSENT from the composed body is a HARD error (AC10) — never a silent
     no-op (a heading typo would leave the lane running the clause the overlay
     was written to override while the D8 hash still validates).
  2. SCALARS: last-write-wins; an explicit empty string CLEARS (mirrors
     ``settings_service`` empty-string clearing).
  3. LISTS (skills, tools, …): UNION preserving order, with a ``_replace:``
     escape hatch ({"_replace": true} sibling directive) for a hard override.
     Absent key ≠ explicit ``null`` ≠ ``[]`` is preserved (the skills tri-state).
  4. DICTS (mcpServers, hooks, …): shallow key-wise merge, never deep.
  5. contextPolicy: ATOMIC replacement EXCEPT ``extraLeaves`` which UNIONS.
  6. CATCH-ALL: any ``AgentProfile`` field not in a named class merges as a
     scalar (last-write-wins, empty-string clears). A test enumerates
     ``AgentProfile.model_fields`` and asserts every field has an assigned
     class (AC13), so an upstream field addition fails the suite rather than
     silently inheriting a default.

Resolver meta-keys (``extends``, ``_replace``, ``position``, ``providers``,
``replaces``) are stripped from the merged dict before construction (D5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.models.agent_profile import AgentProfile

# --------------------------------------------------------------------------
# Merge-key classification (D5). Every AgentProfile field must appear in
# exactly one class OR fall to the catch-all; AC13 enforces total coverage.
# --------------------------------------------------------------------------

# 1. Persona text — the markdown body, handled specially (not via the dict merge).
PERSONA_TEXT_FIELD = "system_prompt"

# 3. Lists merged by UNION (order-preserving), with a ``_replace`` escape.
LIST_UNION_FIELDS = frozenset(
    {
        "skills",
        "tools",
        "allowedTools",
        "resources",
        "capabilities",
        "tags",
    }
)

# 4. Dicts merged SHALLOW key-wise (never deep).
DICT_SHALLOW_FIELDS = frozenset(
    {
        "mcpServers",
        "toolAliases",
        "toolsSettings",
        "hooks",
        "permissions",
        "codexConfig",
        "claudeConfig",
    }
)

# 5. contextPolicy — atomic replace, extraLeaves unions.
CONTEXT_POLICY_FIELD = "contextPolicy"

# 2/6. Everything else is a scalar (last-write-wins, empty-string clears). We do
# NOT enumerate scalars explicitly: the catch-all IS the scalar rule, so an
# upstream field addition lands here safely. AC13 asserts the four named classes
# above plus the catch-all cover every model field.

# Resolver-owned directive keys stripped from the merged dict before
# construction. ``position`` is a model field but the raw ``position:`` DIRECTIVE
# is a resolver input (D6): the resolver sets ``.position`` programmatically, so
# the directive key itself must not pass through as a passthrough dict value.
_MERGE_META_KEYS = (
    "extends",
    "_replace",
    "position",
    "providers",
    "replaces",
    "requires",
    "certification",
)

# Provider-notes heading template for an overlay body appended (not replacing).
_PROVIDER_NOTES_HEADING = "## Provider notes ({provider})"

# A markdown ATX heading line, any level.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)


class CompositionError(ValueError):
    """A composition could not be completed (bad overlay, missing replaces target)."""


@dataclass
class Layer:
    """One composition layer: parsed frontmatter metadata + markdown body.

    ``kind`` names the layer for error messages and provider-notes headings.
    ``provider`` is the overlay's provider (used for the notes heading); None
    for the position layer.
    """

    kind: str
    metadata: Dict[str, Any]
    body: str = ""
    provider: Optional[str] = None
    replaces: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# List union with tri-state + _replace escape (D5 class 3, AC3)
# --------------------------------------------------------------------------


def _merge_list_field(
    current: Any,
    incoming: Any,
    incoming_replace: bool,
) -> Any:
    """Merge one list-class field, preserving the absent/null/[] tri-state.

    * incoming absent (sentinel _ABSENT) → keep ``current`` unchanged.
    * incoming ``None`` (explicit null) → last-write-wins to None (the
      "full catalog" tri-state value for skills). Union with a prior list is
      NOT attempted — null is a deliberate override, not an additive entry.
    * incoming ``_replace`` escape → last-write-wins to the incoming list.
    * otherwise → order-preserving union of ``current`` (if a list) and
      ``incoming``.
    """
    if incoming is _ABSENT:
        return current
    if incoming is None:
        return None
    if incoming_replace:
        return list(incoming)
    base = list(current) if isinstance(current, list) else []
    out = list(base)
    for item in incoming:
        if item not in out:
            out.append(item)
    return out


# --------------------------------------------------------------------------
# contextPolicy atomic-replace + extraLeaves union (D5 class 5, AC6)
# --------------------------------------------------------------------------


def _merge_context_policy(current: Any, incoming: Any) -> Any:
    """Atomic replacement of contextPolicy, EXCEPT extraLeaves which unions.

    Both values are the raw dict forms from frontmatter (or None). When both
    are dicts, the incoming policy wins wholesale, but its ``extraLeaves`` is
    the union of both layers' ``extraLeaves`` (so a codex overlay's
    unrestricted leaves survive a position-level contextPolicy — AC6).
    """
    if incoming is _ABSENT:
        return current
    if not isinstance(incoming, dict):
        return incoming
    if not isinstance(current, dict):
        return dict(incoming)
    merged = dict(incoming)  # atomic replace
    cur_leaves = current.get("extraLeaves") or []
    inc_leaves = incoming.get("extraLeaves") or []
    if isinstance(cur_leaves, list) or isinstance(inc_leaves, list):
        union: List[Any] = []
        for leaf in list(cur_leaves) + list(inc_leaves):
            if leaf not in union:
                union.append(leaf)
        if union:
            merged["extraLeaves"] = union
    return merged


# --------------------------------------------------------------------------
# Dict shallow merge (D5 class 4)
# --------------------------------------------------------------------------


def _merge_dict_field(current: Any, incoming: Any) -> Any:
    """Shallow key-wise dict merge (never deep)."""
    if incoming is _ABSENT:
        return current
    if not isinstance(incoming, dict):
        return incoming
    if not isinstance(current, dict):
        return dict(incoming)
    out = dict(current)
    out.update(incoming)  # shallow — incoming keys win wholesale
    return out


# --------------------------------------------------------------------------
# Scalar last-write-wins with empty-string clear (D5 class 2 + catch-all 6)
# --------------------------------------------------------------------------


def _merge_scalar_field(current: Any, incoming: Any) -> Any:
    """Last-write-wins; an explicit empty string CLEARS to None."""
    if incoming is _ABSENT:
        return current
    if incoming == "":
        return None
    return incoming


class _Absent:
    """Sentinel distinguishing "key absent" from an explicit ``None`` value."""

    _singleton: "Optional[_Absent]" = None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<ABSENT>"


_ABSENT = _Absent()


# --------------------------------------------------------------------------
# Persona-text composition (D5 class 1, AC10)
# --------------------------------------------------------------------------


def _find_section_span(body: str, heading_text: str) -> "tuple[int, int] | None":
    """Return the (start, end) char span of the section named ``heading_text``.

    The span runs from the heading line to just before the NEXT heading of the
    same-or-higher level (or EOF). Returns None when no heading matches. Used
    for byte-exact in-place replacement (never split+rejoin, which would
    normalise separators and break AC1 byte-identity).
    """
    matches = list(_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        if m.group(2).strip() == heading_text.strip():
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            return start, end
    return None


def _overlay_sections(overlay_body: str) -> List[str]:
    """Split an overlay body into its top-level heading sections, in order.

    Each returned string is the exact substring from one heading to just before
    the next (byte-preserving). Leading content before the first heading is
    dropped (an overlay used for ``replaces`` is a sequence of replacement
    sections; prose before the first heading has no target to map onto).
    """
    matches = list(_HEADING_RE.finditer(overlay_body))
    if not matches:
        return [overlay_body]
    out: List[str] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(overlay_body)
        out.append(overlay_body[m.start() : end])
    return out


def _compose_persona_text(position_body: str, overlays: List[Layer]) -> str:
    """Append or replace overlay bodies into the composed persona text (AC10).

    * An overlay with no ``replaces:`` is APPENDED under a
      ``## Provider notes (<provider>)`` heading.
    * An overlay declaring ``replaces: ["<h1>", "<h2>", ...]`` replaces each
      named composed section, IN PLACE and byte-exact, with the CORRESPONDING
      section of the overlay body (positional: the i-th replaces target is
      swapped for the i-th heading-section of the overlay body). This handles a
      section whose heading TEXT itself changes (e.g. an H1 title rename), which
      a heading-name match could not. A named target absent from the composed
      body is a HARD CompositionError (AC10 — never a silent no-op); an overlay
      supplying fewer sections than it names targets is also an error.
    """
    composed = position_body
    for overlay in overlays:
        if overlay.replaces:
            for target in overlay.replaces:
                if _find_section_span(composed, target) is None:
                    raise CompositionError(
                        f"overlay '{overlay.kind}' declares replaces: "
                        f"[{target!r}] but heading {target!r} is absent from the "
                        f"composed persona body; a replaces target must match an "
                        f"existing heading exactly (AC10 — never a silent no-op)"
                    )
            repl_sections = _overlay_sections(overlay.body)
            if len(repl_sections) < len(overlay.replaces):
                raise CompositionError(
                    f"overlay '{overlay.kind}' names {len(overlay.replaces)} "
                    f"replaces targets but its body supplies only "
                    f"{len(repl_sections)} heading-section(s)"
                )
            # Apply right-to-left by span so earlier edits don't shift later
            # spans. Sort target spans by start descending. Each replacement
            # inherits the TRAILING whitespace of the span it replaces, so a
            # frontmatter-round-tripped overlay (whose final section lost its
            # trailing newline to a YAML dumper) still splices byte-exactly and
            # does not glue onto the following heading.
            edits = []
            for i, target in enumerate(overlay.replaces):
                span = _find_section_span(composed, target)
                assert span is not None  # validated above
                edits.append((span[0], span[1], repl_sections[i]))
            for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
                replaced = composed[start:end]
                trail = replaced[len(replaced.rstrip("\n")) :]
                repl_norm = repl.rstrip("\n") + trail
                composed = composed[:start] + repl_norm + composed[end:]
        else:
            heading = _PROVIDER_NOTES_HEADING.format(provider=overlay.provider or overlay.kind)
            body = overlay.body.strip("\n")
            if body:
                composed = f"{composed.rstrip(chr(10))}\n\n{heading}\n\n{body}\n"
    return composed.strip("\n")


# --------------------------------------------------------------------------
# Field classification dispatch
# --------------------------------------------------------------------------


def _merge_one_field(
    field_name: str,
    current: Any,
    incoming: Any,
    incoming_replace: bool,
) -> Any:
    """Dispatch one field to its D5 merge class."""
    if field_name in LIST_UNION_FIELDS:
        return _merge_list_field(current, incoming, incoming_replace)
    if field_name in DICT_SHALLOW_FIELDS:
        return _merge_dict_field(current, incoming)
    if field_name == CONTEXT_POLICY_FIELD:
        return _merge_context_policy(current, incoming)
    # class 2 + catch-all 6: scalar last-write-wins, empty-string clears.
    return _merge_scalar_field(current, incoming)


def _list_replace_requested(metadata: Dict[str, Any], field_name: str) -> bool:
    """True when a layer requested a hard list replace for ``field_name``.

    The ``_replace`` directive is a list of field names to replace rather than
    union: ``_replace: ["skills"]``. (A bare boolean ``_replace: true`` is
    treated as "replace all list fields present in this layer".)
    """
    directive = metadata.get("_replace")
    if directive is True:
        return True
    if isinstance(directive, list):
        return field_name in directive
    return False


# --------------------------------------------------------------------------
# Top-level compose
# --------------------------------------------------------------------------

# The set of model fields, computed once. Used to know which frontmatter keys
# are real fields (everything else that is not a meta-key is dropped by
# AgentProfile construction anyway, but we merge only known fields to keep the
# tri-state semantics honest).
_MODEL_FIELDS = tuple(AgentProfile.model_fields.keys())


def compose_profile(
    profile_name: str,
    layers: List[Layer],
    *,
    position_name: str,
    provider: str,
) -> AgentProfile:
    """Compose an ``AgentProfile`` from ordered layers (lowest → highest, D4).

    ``layers[0]`` is the position layer; the rest are provider overlays in
    precedence order. ``profile_name`` becomes the composed ``.name`` (D6 — the
    legacy concrete name is preserved by the caller). ``position_name`` /
    ``provider`` are stamped into the resolver-internal axis fields.
    """
    if not layers:
        raise CompositionError(f"no layers to compose for '{profile_name}'")

    position_layer = layers[0]
    overlay_layers = layers[1:]

    # --- dict-layer field merge (D5 classes 2-6) --------------------------
    merged: Dict[str, Any] = {}
    for layer in layers:
        for field_name in _MODEL_FIELDS:
            if field_name == PERSONA_TEXT_FIELD:
                continue  # persona text handled separately below
            incoming = layer.metadata.get(field_name, _ABSENT)
            current = merged.get(field_name, _ABSENT)
            incoming_replace = _list_replace_requested(layer.metadata, field_name)
            result = _merge_one_field(field_name, current, incoming, incoming_replace)
            if result is not _ABSENT:
                merged[field_name] = result

    # --- persona text (D5 class 1, AC10) ----------------------------------
    # parse_agent_profile_text stores ``data.content.strip()``; match that so a
    # single-layer (no-overlay) composition is byte-identical to a direct parse
    # (AC1).
    merged[PERSONA_TEXT_FIELD] = _compose_persona_text(position_layer.body, overlay_layers).strip()

    # --- strip meta-keys, stamp identity ----------------------------------
    for meta in _MERGE_META_KEYS:
        merged.pop(meta, None)
    merged["name"] = profile_name
    if not merged.get("description"):
        merged["description"] = ""
    merged["position"] = position_name
    merged["provider"] = provider

    return AgentProfile(**merged)


def compose_source_body(layers: List[Layer]) -> str:
    """Compose ONLY the persona BODY from ordered layers (Ruling 1 source path).

    Used by the install-time SOURCE composition, which needs the merged persona
    text WITHOUT constructing an ``AgentProfile`` and without env resolution
    (the caller passed raw, unresolved fragment bodies). Same persona-merge
    (append / span-exact ``replaces:``) as ``compose_profile`` (D5 class 1), so
    the two composition paths agree on the body (the install↔spawn invariant
    the Ruling-1 test asserts).
    """
    if not layers:
        raise CompositionError("no layers to compose")
    position_layer = layers[0]
    overlay_layers = layers[1:]
    return _compose_persona_text(position_layer.body, overlay_layers).strip()


# --------------------------------------------------------------------------
# D8 — composed-profile hash (compute-only in P2; see F127 #130 note)
# --------------------------------------------------------------------------


def composed_profile_hash(layers: List[Layer]) -> str:
    """Return a stable content hash over the ordered composition layers (D8).

    D8 keys certification by ``(position, provider, position_sha, overlay_sha)``;
    this computes a single digest over ALL layer frontmatters + bodies in order,
    which invalidates on a persona edit (position layer) OR an overlay edit
    (any overlay layer) — the behaviour D8 requires.

    P2 SCOPE NOTE (F127 #130 still open): the blueprint (D8) says this hash rides
    the F127 resolved-model echo channel and is stamped into terminal metadata at
    spawn. F127 (#130) is NOT landed, so we COMPUTE the hash here but DO NOT build
    a second echo channel to stamp it. The spawn-time stamp is a TODO gated on
    F127 #130 — see terminal_service (no stamp wired in P2). Computing it now lets
    ``cao install`` detect an input change (Ruling 1b) without the echo channel.
    """
    import hashlib
    import json

    h = hashlib.sha256()
    for layer in layers:
        # Canonical, order-stable serialisation of each layer.
        h.update(layer.kind.encode("utf-8"))
        h.update(b"\x00")
        h.update(json.dumps(layer.metadata, sort_keys=True, default=str).encode("utf-8"))
        h.update(b"\x00")
        h.update(layer.body.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


# Frontmatter keys excluded from the AC15 position_sha. r9 (a): the
# ``certification:`` block MUST NOT feed position_sha, else recording a PASS row
# would change the sha and invalidate the very row just written (self-
# invalidation). Everything else in the position frontmatter is merge-relevant
# (role, skills, mcpServers, requires, contextPolicy, …) and IS hashed.
_POSITION_SHA_EXCLUDE = ("certification",)


def position_sha(body: str, frontmatter_meta: Dict[str, Any], length: int = 16) -> str:
    """AC15/D8 position_sha over the persona BODY + merge-relevant frontmatter.

    Excludes the ``certification:`` block (r9 a) so a PASS row never invalidates
    itself. Deterministic (sorted-key JSON for the frontmatter). Returns the
    first ``length`` hex chars (the certification block records 16).
    """
    import hashlib
    import json

    merge_meta = {k: v for k, v in frontmatter_meta.items() if k not in _POSITION_SHA_EXCLUDE}
    h = hashlib.sha256()
    h.update(body.encode("utf-8"))
    h.update(b"\x00")
    h.update(json.dumps(merge_meta, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()[:length]


def overlay_sha(fragments: List[str], length: int = 16) -> str:
    """AC15/D8 overlay_sha over a provider's overlay fragment source(s), in order."""
    import hashlib

    h = hashlib.sha256()
    for frag in fragments:
        h.update(frag.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:length]
