"""Agent profile utilities."""

import logging
import re
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional, Set

import frontmatter

from cli_agent_orchestrator.constants import LOCAL_AGENT_STORE_DIR, PROVIDERS
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.env import resolve_env_vars
from cli_agent_orchestrator.utils.paths import normalized_path

logger = logging.getLogger(__name__)


def _validate_agent_name(agent_name: str) -> None:
    """Reject agent names that could cause path traversal."""
    if "/" in agent_name or "\\" in agent_name or ".." in agent_name:
        raise ValueError(f"Invalid agent name '{agent_name}': must not contain '/', '\\', or '..'")


def _safe_join(root: Path, *parts: str) -> Path | None:
    """Join ``parts`` under ``root`` and return the path only if it stays inside ``root``.

    Normalises the result with ``resolve()`` and confirms containment via
    ``relative_to(root.resolve())``. Returns ``None`` when the joined path
    would escape the root (e.g., due to an absolute component, traversal
    segments, or a symlink that points outside). Callers should treat a
    ``None`` result as "not found" rather than raising, so lookups across
    multiple configured roots can fall through cleanly.

    This is defence-in-depth alongside ``_validate_agent_name``: the name
    check rejects traversal-style inputs up front, and this helper refuses
    to touch the filesystem if anything slipped through.
    """
    resolved_root = root.resolve()
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return None
    return candidate


# Read-time bounds for discovery metadata, mirroring agent_profile.schema.json.
# The schema is only enforced at install/validate time; profiles can reach the
# stores without passing through it (manual copy, git checkout), so the read
# path re-enforces the limits before the values feed search corpora and the
# find_profiles MCP surface. (The full file is read to parse frontmatter, but
# the prompt body is never indexed or returned by discovery.)
_DISCOVERY_MAX_ITEMS = 32
_CAPABILITY_MAX_LEN = 128
_DESCRIPTION_MAX_LEN = 1024
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _discovery_fields(metadata: dict) -> Dict:
    """Extract discovery metadata (description/capabilities/tags/role) from frontmatter.

    Non-string descriptions and non-list tag/capability values are coerced to
    empty, and schema limits (item counts, lengths, tag charset) are enforced
    here so downstream search code can rely on bounded, well-shaped values
    even for profiles that never went through ``cao install`` validation.
    """
    desc_raw = metadata.get("description")
    caps_raw = metadata.get("capabilities")
    tags_raw = metadata.get("tags")
    role = metadata.get("role")

    description = desc_raw[:_DESCRIPTION_MAX_LEN] if isinstance(desc_raw, str) else ""
    capabilities = (
        [str(c)[:_CAPABILITY_MAX_LEN] for c in caps_raw[:_DISCOVERY_MAX_ITEMS]]
        if isinstance(caps_raw, list)
        else []
    )
    tags = (
        [str(t) for t in tags_raw[:_DISCOVERY_MAX_ITEMS] if _TAG_PATTERN.fullmatch(str(t))]
        if isinstance(tags_raw, list)
        else []
    )
    return {
        "description": description,
        "capabilities": capabilities,
        "tags": tags,
        "role": str(role) if isinstance(role, str) else "",
    }


def _scan_profile_source(source, profile_name: str) -> "tuple[Dict, bool]":
    """Extract discovery fields and loadability for a scanned profile source.

    ``loadable`` mirrors what ``load_agent_profile()`` will accept: the text
    must read, the frontmatter must parse, and the metadata must validate
    against the ``AgentProfile`` model (with the same name/description
    defaults ``parse_agent_profile_text`` applies). Environment-variable
    resolution is intentionally not applied here: it is runtime-dependent
    and does not affect structural validity.

    ``source`` is anything with ``read_text()`` (a ``Path`` or an
    ``importlib.resources`` traversable).
    """
    try:
        data = frontmatter.loads(source.read_text())
    except Exception:
        return _discovery_fields({}), False
    discovery = _discovery_fields(data.metadata)
    try:
        meta = dict(data.metadata)
        meta["system_prompt"] = data.content.strip()
        meta.setdefault("name", profile_name)
        meta.setdefault("description", "")
        AgentProfile(**meta)
    except Exception:
        return discovery, False
    return discovery, True


def _scan_directory(
    directory: Path,
    source_label: str,
    profiles: Dict[str, Dict],
    name_sources: Dict[str, List[str]] | None = None,
    dir_profiles_loadable: bool = True,
) -> None:
    """Scan a directory for agent profiles (.md files, .json files, or subdirectories).

    ``profiles`` keeps the first-found profile per name (scan order decides the
    winner). ``name_sources``, when given, records each directory a name was
    found in (winner first, once per directory — a dir holding both
    ``<name>.md`` and ``<name>/`` counts once), so callers can surface
    same-named profiles defined in more than one enabled directory (GH #280).

    ``dir_profiles_loadable`` mirrors ``_read_agent_profile_source``'s source
    rules: directory-style profiles (``<name>/agent.md``) are only resolvable
    from provider and extra directories, not from the local store, so the
    local-store scan passes ``False`` to keep such entries listable but never
    recommendable.

    Only two things are profiles: a top-level ``<name>.md`` regular file, or a
    subdirectory that CONTAINS ``<name>/agent.md``. A subdirectory WITHOUT an
    ``agent.md`` (e.g. the F497 ``positions/`` / ``overlays/`` composition
    stores that live inside the agent store) is not a profile and is skipped
    entirely — a structural rule, not a name blacklist (F558 #413).
    """
    if not directory.exists():
        return
    seen_here: Set[str] = set()

    def _record(profile_name: str) -> None:
        if name_sources is not None and profile_name not in seen_here:
            seen_here.add(profile_name)
            name_sources.setdefault(profile_name, []).append(source_label)

    for item in directory.iterdir():
        if item.is_dir():
            profile_name = item.name
            agent_md = item / "agent.md"
            if not agent_md.exists():
                # A directory with no ``agent.md`` is NOT a profile — it must
                # never surface, not even as a listable-but-unloadable entry.
                # The F497 composition stores (``positions/``, ``overlays/``)
                # live as siblings INSIDE the agent store (constants.py) and
                # are read directly by the resolver, never scanned as flat
                # profiles; recording them here produced fake "positions" /
                # "overlays" profiles whose _charter_projection() then raised
                # FileNotFoundError and crashed the session brief on a fresh
                # install (F558 #413). Any other stray directory is skipped by
                # the same structural rule — this is not a name blacklist.
                continue
            if dir_profiles_loadable:
                discovery, loadable = _scan_profile_source(agent_md, profile_name)
            else:
                # Content may be fine, but _read_agent_profile_source() does
                # not resolve directory-style profiles from this store, so
                # load_agent_profile() would raise FileNotFoundError.
                discovery, _ = _scan_profile_source(agent_md, profile_name)
                loadable = False
            _record(profile_name)
            if profile_name not in profiles:
                profiles[profile_name] = {
                    "name": profile_name,
                    "source": source_label,
                    "loadable": loadable,
                    **discovery,
                }
        elif item.suffix == ".md" and item.is_file():
            profile_name = item.stem
            discovery, loadable = _scan_profile_source(item, profile_name)
            _record(profile_name)
            if profile_name not in profiles:
                profiles[profile_name] = {
                    "name": profile_name,
                    "source": source_label,
                    "loadable": loadable,
                    **discovery,
                }


def list_agent_profiles() -> List[Dict]:
    """Discover all available agent profiles from all configured directories.

    Scans built-in store, local store, and all provider agent directories
    (from settings or defaults). Returns deduplicated list sorted by name.
    """
    from cli_agent_orchestrator.services.settings_service import (
        get_agent_dirs,
        get_disabled_agent_dirs,
        get_extra_agent_dirs,
    )

    profiles: Dict[str, Dict] = {}
    # name -> every enabled directory the name was found in (winner first), used
    # to flag same-named profiles defined in more than one dir (GH #280).
    name_sources: Dict[str, List[str]] = {}
    disabled = {normalized_path(d) for d in get_disabled_agent_dirs()}
    scanned_paths: Set[str] = set()

    # 1. Local agent store (derives from CAO_HOME_DIR, default
    # ~/.aws/cli-agent-orchestrator/agent-store/).
    # It shares a path with the claude_code/codex default, so honour the
    # disable toggle here too — otherwise disabling that default wouldn't hide
    # its profiles.
    local_norm = normalized_path(LOCAL_AGENT_STORE_DIR)
    if local_norm not in disabled:
        _scan_directory(
            LOCAL_AGENT_STORE_DIR,
            "local",
            profiles,
            name_sources,
            dir_profiles_loadable=False,
        )
        scanned_paths.add(local_norm)

    # 2. Provider-specific directories (from settings)
    agent_dirs = get_agent_dirs()
    provider_source_labels = {
        "kiro_cli": "kiro",
        "claude_code": "claude_code",
        "codex": "codex",
        "cao_installed": "installed",
    }
    for provider, dir_path in agent_dirs.items():
        norm = normalized_path(dir_path)
        if norm in disabled or norm in scanned_paths:
            continue
        label = provider_source_labels.get(provider, provider)
        _scan_directory(Path(dir_path), label, profiles, name_sources)
        scanned_paths.add(norm)

    # 3. Extra user-added directories
    for extra_dir in get_extra_agent_dirs():
        norm = normalized_path(extra_dir)
        if norm in disabled or norm in scanned_paths:
            continue
        _scan_directory(Path(extra_dir), "custom", profiles, name_sources)
        scanned_paths.add(norm)

    # 4. Built-in agent store — scanned LAST so on-disk copies win (matches
    # _read_agent_profile_source's lookup order).
    try:
        agent_store = resources.files("cli_agent_orchestrator.agent_store")
        for item in agent_store.iterdir():
            name = item.name
            if name.endswith(".md"):
                profile_name = name[:-3]
                name_sources.setdefault(profile_name, []).append("built-in")
                if profile_name in profiles:
                    continue
                discovery, loadable = _scan_profile_source(item, profile_name)
                profiles[profile_name] = {
                    "name": profile_name,
                    "source": "built-in",
                    "loadable": loadable,
                    **discovery,
                }
    except Exception as e:
        logger.debug(f"Could not scan built-in agent store: {e}")

    # Flag conflicts: a name found in more than one enabled directory. The
    # winner (first scanned) is what loads; ``duplicated_in`` lists the shadowed
    # sources so the UI can show "also defined in …" (GH #280 nice-to-have).
    for profile_name, profile in profiles.items():
        srcs = name_sources.get(profile_name, [])
        profile["duplicated_in"] = srcs[1:] if len(srcs) > 1 else []

    return sorted(profiles.values(), key=lambda p: p["name"])


def parse_agent_profile_text(resolved_text: str, profile_name: str) -> AgentProfile:
    """Parse an AgentProfile from already-resolved markdown text."""
    profile_data = frontmatter.loads(resolved_text)
    meta = profile_data.metadata
    meta["system_prompt"] = profile_data.content.strip()
    # Fill in required fields if missing (Kiro profiles don't have frontmatter)
    if "name" not in meta:
        meta["name"] = profile_name
    if "description" not in meta:
        meta["description"] = ""
    return AgentProfile(**meta)


# --- F497 position/provider decoupling resolver (D2/D5) --------------------
#
# The resolver sits ABOVE the provider layer (D2): it feeds the existing
# ``load_agent_profile`` seam so ``profile.name`` composition is computed once,
# not fanned into the four provider modules. A legacy profile that declares no
# composition keys resolves BYTE-IDENTICALLY to today's direct parse (AC1); a
# profile declaring ``position:``/``extends:`` is composed from the
# ``positions/`` + ``overlays/`` stores via the D5 merge engine
# (``utils/profile_composition.py``).

# Frontmatter keys whose PRESENCE marks a profile as composition-bearing. A
# profile carrying either one is refused by ``cao install`` on a resolver-less
# server (AC2); on a resolver-capable server it is composed here (D5).
PROFILE_COMPOSITION_KEYS = ("extends", "position")

# Resolver-owned meta-keys stripped from the merged dict before ``AgentProfile``
# is constructed (D5). ``AgentProfile`` has no ``model_config`` forbidding extra
# keys, so unknown keys are silently dropped — stripping is defence-in-depth so
# a resolver input never masquerades as (or collides with) a model field. Note
# ``position`` IS a resolver-internal model field (D6): the raw ``position:``
# frontmatter DIRECTIVE is consumed here, and the resolver sets the field
# programmatically from the resolved persona — the two are not the same thing.
_RESOLVER_META_KEYS = (
    "extends",
    "_replace",
    "position",
    "providers",
    "requires",
    "certification",
)


def profile_declares_composition(metadata: dict) -> bool:
    """True when frontmatter carries any F497 composition key (``extends``/``position``).

    Shared by the resolver and the ``cao install`` fail-closed guard (AC2) so
    both agree on exactly which profiles are "new-style" and must not reach a
    resolver-less server. A key present but empty/false still counts as
    declared: an ``extends:`` with no value is a malformed composition profile,
    not a legacy one, and must not slip through as byte-identical.
    """
    return any(key in metadata for key in PROFILE_COMPOSITION_KEYS)


def _read_composition_store(
    store_dir: Path, stem: str, *, resolve_env: bool = True
) -> "tuple[dict, str] | None":
    """Read one composition-store fragment (``positions/`` or ``overlays/``).

    Returns ``(metadata, body)`` for ``<store_dir>/<stem>.md`` when present and
    NOT a frozen fragment (``# FROZEN:`` first line — D3), else ``None``. Uses
    ``_safe_join`` so a crafted stem cannot escape the store root.

    ``resolve_env`` controls ``${VAR}`` expansion: True (default) for the
    load/spawn path (an ``AgentProfile`` wants concrete values); False for the
    install-time SOURCE composition (the context file stores UNRESOLVED source
    so ``${VAR}`` defers to runtime, F497 D2 addendum / Ruling 1).
    """
    path = _safe_join(store_dir, f"{stem}.md")
    if path is None or not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    # D3: the ``# FROZEN:`` first-line convention applies to positions/overlays
    # too, else a frozen fragment silently resurrects.
    if text.lstrip().startswith("# FROZEN:"):
        return None
    if resolve_env:
        text = resolve_env_vars(text)
    parsed = frontmatter.loads(text)
    return dict(parsed.metadata), parsed.content


def _resolve_composition_layers(
    position_name: str,
    provider: str,
    *,
    resolve_env: bool = True,
) -> "list":
    """Load the ordered composition layers for (position, provider) (D4 2→4).

    Order: ``positions/<pos>.md`` → ``overlays/<provider>.md`` →
    ``overlays/<provider>.<pos>.md``. The position fragment is REQUIRED; either
    overlay is optional. Enforces the position ``providers:`` allowlist (D7):
    a provider outside the allowlist raises so the assign/load is rejected
    rather than silently running the wrong instructions.

    ``resolve_env`` is threaded to ``_read_composition_store``: True for the
    load/spawn path, False for install-time SOURCE composition (Ruling 1).
    """
    from cli_agent_orchestrator.constants import overlays_store_dir, positions_store_dir
    from cli_agent_orchestrator.utils.profile_composition import CompositionError, Layer

    positions_dir = positions_store_dir()
    overlays_dir = overlays_store_dir()

    pos = _read_composition_store(positions_dir, position_name, resolve_env=resolve_env)
    if pos is None:
        raise CompositionError(
            f"position '{position_name}' not found in positions store "
            f"{positions_dir} (or it is frozen)"
        )
    pos_meta, pos_body = pos

    # D7: position-level providers allowlist. Absent allowlist = unconstrained.
    allow = pos_meta.get("providers")
    if isinstance(allow, list) and allow and provider not in allow:
        raise CompositionError(
            f"provider '{provider}' is not in position '{position_name}' "
            f"allowlist {allow}; refusing composition (D7)"
        )

    layers = [Layer(kind=f"position:{position_name}", metadata=pos_meta, body=pos_body)]

    overlay = _read_composition_store(overlays_dir, provider, resolve_env=resolve_env)
    if overlay is not None:
        o_meta, o_body = overlay
        layers.append(
            Layer(
                kind=f"overlay:{provider}",
                metadata=o_meta,
                body=o_body,
                provider=provider,
                replaces=list(o_meta.get("replaces") or []),
            )
        )

    overlay_pos = _read_composition_store(
        overlays_dir, f"{provider}.{position_name}", resolve_env=resolve_env
    )
    if overlay_pos is not None:
        op_meta, op_body = overlay_pos
        layers.append(
            Layer(
                kind=f"overlay:{provider}.{position_name}",
                metadata=op_meta,
                body=op_body,
                provider=provider,
                replaces=list(op_meta.get("replaces") or []),
            )
        )
    return layers


def _stub_composition_inputs(metadata: dict, profile_name: str) -> "tuple[str, str]":
    """Resolve (position_name, provider) from a composition-bearing profile's frontmatter.

    Two spellings are accepted:
      * alias stub: ``extends: <position>`` + ``provider: <provider>`` (D3/D4).
      * direct: ``position: <position>`` + ``provider: <provider>``.
    ``extends`` wins when both are present. The provider MUST resolve — a
    composition profile with no provider cannot be composed (D7).
    """
    position_name = metadata.get("extends") or metadata.get("position")
    if not position_name or not isinstance(position_name, str):
        raise ValueError(
            f"Agent profile '{profile_name}' declares composition but names no "
            f"position (expected 'extends:' or 'position:')"
        )
    provider = metadata.get("provider")
    if not provider or not isinstance(provider, str):
        raise ValueError(
            f"Agent profile '{profile_name}' declares composition (position "
            f"'{position_name}') but names no provider; cannot compose (D7)"
        )
    return position_name, provider


def resolve_agent_profile(resolved_text: str, profile_name: str) -> AgentProfile:
    """Compose an ``AgentProfile`` from already-env-resolved markdown text.

    This is the F497 resolver seam (D2) — the single point above the provider
    layer where persona/overlay composition happens, so ``.name`` composition
    is computed once, not fanned into the four provider modules.

    Contract:
      * A profile declaring NO composition key resolves exactly as
        ``parse_agent_profile_text`` does today — byte-identical (AC1). This is
        the whole legacy corpus.
      * A profile declaring ``extends:``/``position:`` is a COMPOSITION profile
        (an alias stub or a direct position profile). The resolver loads the
        ``positions/<pos>.md`` persona and the ``overlays/<provider>[.<pos>].md``
        overlays, merges them via the D5 engine, stamps the LEGACY concrete name
        (D6), and enforces the D3 ``role`` mirror agreement + owns
        ``description`` from the stub layer.
    """
    parsed = frontmatter.loads(resolved_text)
    metadata = dict(parsed.metadata)
    if not profile_declares_composition(metadata):
        return parse_agent_profile_text(resolved_text, profile_name)

    from cli_agent_orchestrator.utils.profile_composition import compose_profile

    position_name, provider = _stub_composition_inputs(metadata, profile_name)
    layers = _resolve_composition_layers(position_name, provider)

    composed = compose_profile(
        profile_name,
        layers,
        position_name=position_name,
        provider=provider,
    )

    # D3 field split: ``description`` is STUB-OWNED (per-legacy-name identity
    # text) and contributes to the composed profile; ``role`` is a SCANNER-ONLY
    # MIRROR excluded from the merge, with a STRICT agreement check.
    stub_description = metadata.get("description")
    if isinstance(stub_description, str) and stub_description:
        composed.description = stub_description

    stub_role = metadata.get("role")
    if isinstance(stub_role, str) and stub_role and composed.role and stub_role != composed.role:
        raise ValueError(
            f"Agent profile '{profile_name}': mirrored role '{stub_role}' in the "
            f"alias stub disagrees with the composed role '{composed.role}' "
            f"(D3 role-mirror agreement check, AC12)"
        )
    # The stub's role mirror is authoritative for the composed .role only when
    # the position layer left it unset; otherwise the agreement check above has
    # already confirmed they match.
    if isinstance(stub_role, str) and stub_role and not composed.role:
        composed.role = stub_role

    return composed


def compose_agent_profile_source(raw_text: str, profile_name: str) -> str:
    """Compose the UNRESOLVED markdown SOURCE for a composition stub (Ruling 1).

    F497 D2 addendum: kiro delivers its persona via the install-time CONTEXT
    FILE (``agent-context/<name>.md``), which stores the UNRESOLVED profile
    source so ``${VAR}`` defers to runtime. A composition stub has an EMPTY
    body, so the context file must instead receive the COMPOSED body — but
    still unresolved. This mirrors ``resolve_agent_profile`` EXCEPT it never
    env-resolves and it re-serialises to markdown (frontmatter + composed body)
    instead of constructing an ``AgentProfile``.

    For a NON-composition profile this returns ``raw_text`` UNCHANGED (byte
    identical), so the legacy install path is untouched.
    """
    parsed = frontmatter.loads(raw_text)
    metadata = dict(parsed.metadata)
    if not profile_declares_composition(metadata):
        return raw_text

    from cli_agent_orchestrator.utils.profile_composition import (
        compose_source_body,
    )

    position_name, provider = _stub_composition_inputs(metadata, profile_name)
    layers = _resolve_composition_layers(position_name, provider, resolve_env=False)

    # Composed BODY (unresolved) from the raw fragments.
    composed_body = compose_source_body(layers)

    # Composed FRONTMATTER: merge the layer frontmatters (same dict-layer merge
    # the AgentProfile path uses is overkill here — the context file is prose +
    # frontmatter for kiro's resource, and only the BODY carries the persona).
    # Preserve the stub's identity keys (name/description/provider/role) and
    # drop resolver meta-keys so the context file frontmatter is clean.
    out_meta = dict(metadata)
    for meta_key in ("extends", "_replace", "replaces", "providers", "requires", "certification"):
        out_meta.pop(meta_key, None)
    out_meta["position"] = position_name

    post = frontmatter.Post(composed_body, **out_meta)
    return str(frontmatter.dumps(post)) + "\n"


def read_agent_profile_source(agent_name: str) -> str:
    """Locate an agent profile across configured stores and return the raw text.

    Search order:
    1. Local store: <CAO_HOME_DIR>/agent-store/{name}.md (default
       ~/.aws/cli-agent-orchestrator/agent-store/)
    2. Provider-specific directories (flat {name}.md or {name}/agent.md)
    3. Extra user-added directories (flat {name}.md or {name}/agent.md)
    4. Built-in store (packaged with CAO)

    Shared by ``load_agent_profile`` (which parses the text into an
    ``AgentProfile``) and the install service (which writes the raw text to
    the context file). Centralising the lookup keeps the two callers in sync.
    """
    _validate_agent_name(agent_name)

    from cli_agent_orchestrator.services.settings_service import (
        get_agent_dirs,
        get_disabled_agent_dirs,
        get_extra_agent_dirs,
    )

    # Honour the disable toggle on the load path too, so disabling a directory
    # actually swaps which same-named profile wins (GH #280), not just what the
    # Settings list shows.
    disabled = {normalized_path(d) for d in get_disabled_agent_dirs()}

    # Every filesystem read below goes through _safe_join so the path is
    # normalised and verified to stay inside its configured root. This is
    # belt-and-braces on top of _validate_agent_name above — the name check
    # rejects obvious traversal inputs, and _safe_join additionally blocks
    # anything that sneaks past (e.g. symlinks resolving outside the root).
    if normalized_path(LOCAL_AGENT_STORE_DIR) not in disabled:
        local_profile = _safe_join(LOCAL_AGENT_STORE_DIR, f"{agent_name}.md")
        if local_profile is not None and local_profile.exists():
            return local_profile.read_text(encoding="utf-8")

    def _lookup_in_directory(directory: Path) -> str | None:
        if not directory.exists():
            return None
        flat = _safe_join(directory, f"{agent_name}.md")
        if flat is not None and flat.exists():
            return flat.read_text(encoding="utf-8")
        nested = _safe_join(directory, agent_name, "agent.md")
        if nested is not None and nested.exists():
            return nested.read_text(encoding="utf-8")
        return None

    for dir_path in get_agent_dirs().values():
        if normalized_path(dir_path) in disabled:
            continue
        found = _lookup_in_directory(Path(dir_path))
        if found is not None:
            return found

    for extra_dir in get_extra_agent_dirs():
        if normalized_path(extra_dir) in disabled:
            continue
        found = _lookup_in_directory(Path(extra_dir))
        if found is not None:
            return found

    # Built-in store is inside the installed package — the traversable API
    # still concatenates agent_name as a single segment, so validate the
    # result's name before reading.
    agent_store = resources.files("cli_agent_orchestrator.agent_store")
    built_in = agent_store / f"{agent_name}.md"
    if built_in.name == f"{agent_name}.md" and built_in.is_file():
        return built_in.read_text(encoding="utf-8")

    raise FileNotFoundError(f"Agent profile not found: {agent_name}")


# Backward-compatible private alias; new manifest consumers use the public helper.
_read_agent_profile_source = read_agent_profile_source


def load_agent_profile(agent_name: str) -> AgentProfile:
    """Load an agent profile from the configured stores.

    Routes through the F497 resolver seam (``resolve_agent_profile``) so a
    single point above the provider layer owns profile composition (D2). For
    the whole legacy corpus this is byte-identical to the previous direct
    ``parse_agent_profile_text`` call (AC1); a composition-bearing profile
    (``position:``/``extends:``) is composed from the positions/overlays
    stores via the D5 merge engine.
    """
    try:
        raw_text = read_agent_profile_source(agent_name)
        return resolve_agent_profile(resolve_env_vars(raw_text), agent_name)
    except (FileNotFoundError, ValueError):
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to load agent profile '{agent_name}': {e}")


def resolve_provider(agent_profile_name: str, fallback_provider: str) -> str:
    """Resolve the provider to use for an agent profile.

    Loads the agent profile from the CAO agent store and checks for a
    ``provider`` key.  If present and valid, returns the profile's provider.
    Otherwise returns the fallback provider (typically inherited from the
    calling terminal).

    Args:
        agent_profile_name: Name of the agent profile to look up.
        fallback_provider: Provider to use when the profile does not specify
            one or specifies an invalid value.

    Returns:
        Resolved provider type string.
    """
    try:
        profile = load_agent_profile(agent_profile_name)
    except (FileNotFoundError, RuntimeError):
        # Profile not found or failed to load — provider.initialize()
        # will surface a clear error later.  Fall back for now.
        return fallback_provider

    if profile.provider:
        if profile.provider in PROVIDERS:
            return profile.provider
        else:
            logger.warning(
                "Agent profile '%s' has invalid provider '%s'. "
                "Valid providers: %s. Falling back to '%s'.",
                agent_profile_name,
                profile.provider,
                PROVIDERS,
                fallback_provider,
            )

    return fallback_provider


# --- F497 D7 — assign(provider=) position-name resolution ------------------
#
# ``agent_profile`` on an assign becomes resolvable as EITHER a legacy concrete
# name (a real file in the agent store — unchanged behaviour) OR a POSITION name
# (a file in the positions store, composed with a provider). A position-name
# assign resolves its provider from the ``provider=`` arg; the routing binding
# (D9) that would otherwise supply it is P4, so until then a position name with
# no ``provider=`` is a HARD FAIL. The position's ``providers: [...]`` allowlist
# (D7) rejects a disallowed provider so a mismatched cell never spawns (a warn
# would burn a gate round running the wrong instructions).
#
# NAMED ERRORS (stable codes, asserted by tests and surfaced to the operator):
E_POSITION_NEEDS_PROVIDER = "E-POSITION-NEEDS-PROVIDER"
E_PROVIDER_NOT_ALLOWED = "E-PROVIDER-NOT-ALLOWED"
E_UNKNOWN_POSITION = "E-UNKNOWN-POSITION"


class AssignmentResolutionError(ValueError):
    """A position-name assign could not be resolved (carries a stable ``.code``)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _position_exists(position_name: str) -> bool:
    """True when ``positions/<position_name>.md`` exists (and is not frozen)."""
    from cli_agent_orchestrator.constants import positions_store_dir

    try:
        _validate_agent_name(position_name)
    except ValueError:
        return False
    return (
        _read_composition_store(positions_store_dir(), position_name, resolve_env=False) is not None
    )


def resolve_assignment_target(
    agent_profile: str, provider: Optional[str]
) -> "tuple[str, Optional[str]]":
    """Resolve an assign ``agent_profile`` (+ optional ``provider=``) to a spawn target (D7).

    The resolver ENGAGES (position mode) only when the caller passes ``provider=``
    OR ``agent_profile`` is a bare position file (``positions/<name>.md``). Every
    OTHER name passes through UNTOUCHED as a legacy concrete name — no store
    lookup, no ``<provider>_<position>`` shape inference. This is
    the r2 (option b) fix: a legacy name (installed OR NOT — e.g.
    ``kiro_dev`` / ``codex_dev`` on a clean store) spawns exactly as pre-D7, and
    a ``<provider>_<position>``-shaped legacy name is never mistaken for a
    position miss.

    Returns ``(effective_profile_name, resolved_provider)``:

      * ENGAGED + ``agent_profile`` is a position file: the provider comes from
        ``provider=`` or, when absent, the routing.toml binding (D9); a position
        with neither is ``E-POSITION-NEEDS-PROVIDER``. The provider must be in the
        position's ``providers:`` allowlist (else ``E-PROVIDER-NOT-ALLOWED``). On
        success the effective name is the legacy ALIAS for that cell when one
        exists, else the D6 synthesis ``<provider>_<position>``.
      * ENGAGED via ``provider=`` but ``agent_profile`` is NOT a position file:
        ``E-UNKNOWN-POSITION`` (position mode was requested on a non-position).
      * NOT ENGAGED (no ``provider=`` and not a position file): passthrough
        unchanged (legacy).

    Raises ``AssignmentResolutionError`` (with ``.code``) on a position-mode
    failure only.
    """
    is_position = _position_exists(agent_profile)

    # NOT ENGAGED: no provider= and not a bare position file → legacy passthrough
    # (no store lookup, no shape inference). Pre-D7 behaviour preserved exactly.
    if provider is None and not is_position:
        return agent_profile, provider

    # ENGAGED via provider= but the name is not a position file → position mode
    # was requested on a non-position.
    if not is_position:
        raise AssignmentResolutionError(
            E_UNKNOWN_POSITION,
            f"{E_UNKNOWN_POSITION}: '{agent_profile}' is not a known position "
            f"(provider= requests position mode; <provider>_<position> synthesis "
            f"is P4)",
        )

    # ENGAGED + a bare position file. In P4 (D9) a bare position with no
    # explicit provider= consults the routing binding for its provider; only
    # when neither is available is it a hard fail.
    if not provider:
        provider = _routing_provider_for_position(agent_profile)
    if not provider:
        raise AssignmentResolutionError(
            E_POSITION_NEEDS_PROVIDER,
            f"{E_POSITION_NEEDS_PROVIDER}: position '{agent_profile}' needs an "
            f"explicit provider= or a routing.toml binding (D9)",
        )

    # Enforce the position ``providers:`` allowlist (D7). Absent/empty = open; a
    # non-empty allowlist that omits the provider rejects.
    from cli_agent_orchestrator.constants import positions_store_dir

    pos = _read_composition_store(positions_store_dir(), agent_profile, resolve_env=False)
    assert pos is not None  # _position_exists confirmed it
    allow = pos[0].get("providers")
    if isinstance(allow, list) and allow and provider not in allow:
        raise AssignmentResolutionError(
            E_PROVIDER_NOT_ALLOWED,
            f"{E_PROVIDER_NOT_ALLOWED}: provider '{provider}' is not in position "
            f"'{agent_profile}' allowlist {allow}",
        )

    # D6 — synthesise the spawn profile name for a position-name target. When a
    # legacy alias stub exists for this (provider, position) cell, prefer it (its
    # ``description`` identity + ``[p.profiles.<name>]`` overrides stay keyed on
    # the legacy name, D6); otherwise synthesise ``<provider>_<position>``
    # deterministically. Legacy dev names (``codex_dev``/``grok_dev``) are never
    # reached here — they are not position files, so this branch is position-only.
    effective = _synthesise_position_profile_name(agent_profile, provider)
    return effective, provider


def _routing_provider_for_position(position_name: str) -> Optional[str]:
    """The provider bound to ``position_name`` by routing.toml (D9), or None.

    A missing/malformed routing store is treated as "no binding" here (None) so
    the caller falls through to the ``E-POSITION-NEEDS-PROVIDER`` hard fail with
    its explicit message — the routing store's own structural validation surfaces
    via the D9 validator on the resolution path, not this convenience lookup.
    """
    from cli_agent_orchestrator.constants import routing_toml_path

    try:
        from cli_agent_orchestrator.utils.routing import load_routing_table

        table = load_routing_table(routing_toml_path())
    except Exception:
        return None
    binding = table.binding_for(position_name, None)
    if binding is not None and binding.kind == "cao" and binding.provider:
        return binding.provider
    return None


def _synthesise_position_profile_name(position_name: str, provider: str) -> str:
    """D6 — resolve a position-name target to its concrete spawn profile name.

    Prefers an existing LEGACY ALIAS stub for the (provider, position) cell so
    name-keyed config (``[p.profiles.<name>]``, ``default_fork_base``,
    ``find_profiles``) stays keyed on the legacy name (D6). Falls back to the
    deterministic ``<provider>_<position>`` synthesis when no alias resolves it.
    The alias search reads the flat agent store and matches a stub whose
    ``extends``/``position`` == this position AND ``provider`` == this provider.
    """
    synthesized = f"{provider}_{position_name}"
    try:
        alias = _find_alias_for_cell(position_name, provider)
    except Exception:
        alias = None
    return alias or synthesized


def _find_alias_for_cell(position_name: str, provider: str) -> Optional[str]:
    """Find a legacy alias stub bound to (position_name, provider), or None.

    Scans the flat agent-store ``*.md`` for a composition stub whose resolved
    (position, provider) matches. Returns the stub's file stem (the legacy
    concrete name) so D6 keeps name-keyed consumers working. A synthesised name
    that IS itself an installed stub is returned as-is by the caller's default.
    """
    import frontmatter

    from cli_agent_orchestrator.constants import local_agent_store_dir

    store = local_agent_store_dir()
    if not store.exists():
        return None
    for path in sorted(store.glob("*.md")):
        try:
            meta = dict(frontmatter.loads(path.read_text(encoding="utf-8")).metadata)
        except Exception:
            continue
        if not profile_declares_composition(meta):
            continue
        pos = meta.get("extends") or meta.get("position")
        if pos == position_name and meta.get("provider") == provider:
            return path.stem
    return None
