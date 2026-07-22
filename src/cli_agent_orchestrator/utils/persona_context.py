"""Terminal-scoped native provider context composition and rehydration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import frontmatter

from cli_agent_orchestrator.models.agent_profile import ContextPolicy
from cli_agent_orchestrator.utils.provider_plane import provider_home

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
PERSONA_HEADER = "# CAO persona context"
PERSONA_ENV_UNSET = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
)
RESERVED_LEAVES = {
    "CLAUDE.md",
    ".credentials.json",
    "settings.json",
    "persona-manifest.json",
}
_PREFLIGHTED_BWRAP: set[tuple[str, str]] = set()


class PersonaContextError(RuntimeError):
    """Persona composition or manifest validation failed."""


@dataclass(frozen=True)
class PersonaBind:
    src: Path
    dst: Path


@dataclass(frozen=True)
class PersonaPlan:
    manifest_version: int
    terminal_id: str
    provider: str
    profile_name: str
    generation: str
    policy_hash: str
    canonical_cwd: Path
    generation_dir: Path
    persona_bind: PersonaBind | None
    credential_bind: PersonaBind | None
    leaf_binds: tuple[PersonaBind, ...]
    shadow_binds: tuple[PersonaBind, ...]
    created_mount_points: tuple[Path, ...]
    bwrap_executable: Path | None
    env_set: Mapping[str, str]
    env_unset: tuple[str, ...]
    codex_home: Path | None
    memory_instructions: str


@dataclass(frozen=True)
class PersonaRetentionIntent:
    session_uuid: str
    destination: Path
    member_row_ids: tuple[int, ...] = ()


def cwd_key(cwd: str | Path) -> str:
    """Return Claude's empirically pinned project-directory encoding."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _policy_hash(policy: ContextPolicy) -> str:
    payload = json.dumps(
        policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _header(profile_name: str, policy_hash: str) -> str:
    return (
        "\n".join(
            (
                PERSONA_HEADER,
                f"Profile: {profile_name} (policy sha256:{policy_hash[:12]})",
                "Context filtered by contextPolicy — full supervisor context intentionally absent.",
            )
        )
        + "\n"
    )


def _native_memory_metadata(path: Path) -> tuple[str, str] | None:
    try:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    name = post.metadata.get("name")
    metadata = post.metadata.get("metadata")
    memory_type = metadata.get("type") if isinstance(metadata, dict) else None
    if not isinstance(name, str) or not isinstance(memory_type, str):
        return None
    return name, memory_type


def filter_native_memory(corpus_dir: Path, policy: ContextPolicy) -> list[Path]:
    """Select native Claude memory files by frontmatter type/name."""
    if not corpus_dir.is_dir():
        return []
    selected: list[Path] = []
    names = set(policy.memoryNames)
    types = set(policy.memoryTypes)
    for path in sorted(corpus_dir.glob("*.md"), key=lambda candidate: candidate.name):
        if path.name == "MEMORY.md" or path.is_symlink() or not path.is_file():
            continue
        metadata = _native_memory_metadata(path)
        if metadata is None:
            continue
        name, memory_type = metadata
        if name in names or memory_type in types:
            selected.append(path)
    return selected


def _render_memory(files: Sequence[Path]) -> str:
    if not files:
        return ""
    sections = ["# CAO persona memory"]
    for path in files:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        name = post.metadata.get("name", path.stem)
        sections.extend((f"## {name}", post.content.strip()))
    return "\n\n".join(sections).rstrip() + "\n"


def _write_memory_tree(destination: Path, files: Sequence[Path]) -> None:
    destination.mkdir(parents=True, mode=0o700)
    index_lines = ["# Memory Index", ""]
    for source in files:
        target = destination / source.name
        shutil.copyfile(source, target)
        target.chmod(0o600)
        metadata = _native_memory_metadata(source)
        name = metadata[0] if metadata is not None else source.stem
        index_lines.append(f"- [{name}]({source.name})")
    (destination / "MEMORY.md").write_text("\n".join(index_lines).rstrip() + "\n", encoding="utf-8")
    (destination / "MEMORY.md").chmod(0o600)


def _persona_root(*, create: bool = True) -> Path:
    raw = os.environ.get("XDG_RUNTIME_DIR", "")
    if not raw or not os.path.isabs(raw):
        raise PersonaContextError("persona_runtime_dir_invalid")
    runtime = Path(raw)
    try:
        runtime_stat = runtime.stat()
    except OSError as exc:
        raise PersonaContextError("persona_runtime_dir_inaccessible") from exc
    if runtime_stat.st_uid != os.getuid() or stat.S_IMODE(runtime_stat.st_mode) != 0o700:
        raise PersonaContextError("persona_runtime_dir_ownership_or_mode_invalid")
    root = runtime / "cao-personas"
    if not root.exists() and not create:
        return root
    try:
        if create:
            root.mkdir(mode=0o700, exist_ok=True)
        root_stat = root.stat()
    except OSError as exc:
        raise PersonaContextError("persona_root_unavailable") from exc
    if root_stat.st_uid != os.getuid() or stat.S_IMODE(root_stat.st_mode) != 0o700:
        raise PersonaContextError("persona_root_ownership_or_mode_invalid")
    return root


def _next_generation(terminal_root: Path) -> tuple[str, Path]:
    numbers = []
    for candidate in terminal_root.glob("gen-*"):
        match = re.fullmatch(r"gen-(\d+)", candidate.name)
        if match:
            numbers.append(int(match.group(1)))
    name = f"gen-{max(numbers, default=0) + 1}"
    return name, terminal_root / name


def _validated_leaf_sources(provider: str, leaves: Iterable[str]) -> list[tuple[str, Path]]:
    native_home = provider_home(provider).home.resolve()
    result: list[tuple[str, Path]] = []
    for leaf in leaves:
        if (
            not leaf
            or leaf in {".", ".."}
            or leaf in RESERVED_LEAVES
            or "/" in leaf
            or "\\" in leaf
            or "\x00" in leaf
            or os.path.isabs(leaf)
        ):
            raise PersonaContextError(f"persona_leaf_invalid:{leaf!r}")
        source = native_home / leaf
        if source.is_symlink() or not source.is_file():
            raise PersonaContextError(f"persona_leaf_source_invalid:{leaf}")
        resolved = source.resolve()
        if resolved.parent != native_home:
            raise PersonaContextError(f"persona_leaf_escape:{leaf}")
        result.append((leaf, resolved))
    return result


def _ensure_shadow_mount_points(cwd: Path) -> tuple[Path, ...]:
    created: list[Path] = []
    claude_dir = cwd / ".claude"
    root_claude = cwd / "CLAUDE.md"
    if claude_dir.is_symlink() or (claude_dir.exists() and not claude_dir.is_dir()):
        raise PersonaContextError("persona_shadow_destination_invalid:.claude")
    if root_claude.is_symlink() or (root_claude.exists() and not root_claude.is_file()):
        raise PersonaContextError("persona_shadow_destination_invalid:CLAUDE.md")
    if not claude_dir.exists():
        claude_dir.mkdir(mode=0o755, exist_ok=True)
        created.append(claude_dir)
    if not root_claude.exists():
        root_claude.touch(mode=0o644, exist_ok=True)
        created.append(root_claude)
    return tuple(created)


def _write_codex_config(source: Path, destination: Path) -> None:
    content = source.read_text(encoding="utf-8") if source.is_file() else ""
    lines = content.splitlines(keepends=True)
    first_table = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines)
    )
    root_pattern = re.compile(r"^\s*project_doc_max_bytes\s*=")
    replaced = False
    for index in range(first_table):
        if root_pattern.match(lines[index]):
            lines[index] = "project_doc_max_bytes = 0\n"
            replaced = True
            break
    if not replaced:
        lines.insert(0, "project_doc_max_bytes = 0\n")
    destination.write_text("".join(lines), encoding="utf-8")
    destination.chmod(0o600)


def _bind_to_json(bind: PersonaBind | None) -> dict[str, str] | None:
    return None if bind is None else {"src": str(bind.src), "dst": str(bind.dst)}


def _manifest(plan: PersonaPlan) -> dict[str, Any]:
    return {
        "manifest_version": plan.manifest_version,
        "terminal_id": plan.terminal_id,
        "provider": plan.provider,
        "profile_name": plan.profile_name,
        "generation": plan.generation,
        "policy_hash": plan.policy_hash,
        "canonical_cwd": str(plan.canonical_cwd),
        "persona_bind": _bind_to_json(plan.persona_bind),
        "credential_bind": _bind_to_json(plan.credential_bind),
        "leaf_binds": [_bind_to_json(bind) for bind in plan.leaf_binds],
        "shadow_binds": [_bind_to_json(bind) for bind in plan.shadow_binds],
        "created_mount_points": [str(path) for path in plan.created_mount_points],
        "bwrap_executable": str(plan.bwrap_executable) if plan.bwrap_executable else None,
        "env_set": dict(plan.env_set),
        "env_unset": list(plan.env_unset),
    }


def _write_manifest(plan: PersonaPlan) -> None:
    target = plan.generation_dir / "persona-manifest.json"
    fd, temporary = tempfile.mkstemp(prefix=".persona-manifest-", dir=plan.generation_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(_manifest(plan), stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def persona_wrapper_prefix(plan: PersonaPlan) -> list[str]:
    if plan.provider != "claude_code" or plan.bwrap_executable is None:
        raise PersonaContextError("persona_wrapper_provider_invalid")
    prefix = [
        str(plan.bwrap_executable),
        "--bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--unshare-pid",
        "--die-with-parent",
    ]
    for bind, mode in (
        *((((plan.persona_bind, "--bind"),) if plan.persona_bind else ())),
        *((((plan.credential_bind, "--ro-bind"),) if plan.credential_bind else ())),
    ):
        prefix.extend((mode, str(bind.src), str(bind.dst)))
    for bind in (*plan.leaf_binds, *plan.shadow_binds):
        prefix.extend(("--ro-bind", str(bind.src), str(bind.dst)))
    for name, value in plan.env_set.items():
        prefix.extend(("--setenv", name, value))
    for name in plan.env_unset:
        prefix.extend(("--unsetenv", name))
    return prefix


def _preflight(plan: PersonaPlan) -> None:
    if plan.provider != "claude_code":
        return
    prefix = persona_wrapper_prefix(plan)
    key = (str(plan.bwrap_executable), "\0".join(prefix))
    if key in _PREFLIGHTED_BWRAP:
        return
    completed = subprocess.run(
        prefix
        + [
            "/bin/sh",
            "-c",
            'test "$(sed -n 1p "$CLAUDE_CONFIG_DIR/CLAUDE.md")" = "# CAO persona context"',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "marker_readback_failed"
        raise PersonaContextError(f"persona_bwrap_preflight_failed:{detail}")
    _PREFLIGHTED_BWRAP.add(key)


def wrap_claude_persona(plan: PersonaPlan, command_parts: Sequence[str]) -> list[str]:
    """Wrap an already-built Claude command without changing its inner argv."""
    return persona_wrapper_prefix(plan) + list(command_parts)


def compose_persona_plan(
    terminal_id: str,
    provider: str,
    profile_name: str,
    policy: ContextPolicy,
    canonical_cwd: str | Path,
) -> PersonaPlan:
    """Compose and preflight one immutable terminal persona generation."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", terminal_id):
        raise PersonaContextError("persona_terminal_id_invalid")
    if provider not in {"claude_code", "codex"}:
        raise PersonaContextError(f"persona_provider_unsupported:{provider}")
    cwd = Path(canonical_cwd).resolve(strict=True)
    root = _persona_root()
    terminal_root = root / terminal_id
    terminal_root.mkdir(mode=0o700, exist_ok=True)
    if stat.S_IMODE(terminal_root.stat().st_mode) != 0o700:
        raise PersonaContextError("persona_terminal_root_mode_invalid")
    generation, generation_dir = _next_generation(terminal_root)
    staging = terminal_root / f".{generation}.tmp-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    policy_hash = _policy_hash(policy)
    selected: list[Path] = []
    try:
        claude_home = provider_home("claude_code").home.resolve()
        corpus = claude_home / "projects" / cwd_key(cwd) / "memory"
        selected = filter_native_memory(corpus, policy)
        _write_memory_tree(staging / "projects" / cwd_key(cwd) / "memory", selected)

        header = _header(profile_name, policy_hash)
        global_context = ""
        global_file = claude_home / "CLAUDE.md"
        if policy.globalClaudeMd and global_file.is_file():
            global_context = "\n" + global_file.read_text(encoding="utf-8")
        (staging / "CLAUDE.md").write_text(header + global_context, encoding="utf-8")
        (staging / "CLAUDE.md").chmod(0o600)
        (staging / "settings.json").write_text(
            json.dumps({"skipDangerousModePermissionPrompt": True}) + "\n", encoding="utf-8"
        )
        (staging / "settings.json").chmod(0o600)

        leaf_sources = _validated_leaf_sources(provider, policy.extraLeaves)
        persona_bind: PersonaBind | None = None
        credential_bind: PersonaBind | None = None
        leaf_binds: tuple[PersonaBind, ...] = ()
        shadow_binds: tuple[PersonaBind, ...] = ()
        created_mount_points: tuple[Path, ...] = ()
        bwrap_executable: Path | None = None
        env_set: Mapping[str, str] = {}
        env_unset: tuple[str, ...] = ()
        codex_home: Path | None = None

        if provider == "claude_code":
            real_credentials = claude_home / ".credentials.json"
            if real_credentials.is_symlink() or not real_credentials.is_file():
                raise PersonaContextError("persona_claude_credentials_invalid")
            shadow_root = staging / "cwd-shadow"
            shadow_claude_dir = shadow_root / ".claude"
            shadow_claude_dir.mkdir(parents=True, mode=0o700)
            (shadow_root / "CLAUDE.md").write_text(header, encoding="utf-8")
            (shadow_claude_dir / "CLAUDE.md").write_text(header, encoding="utf-8")
            created_mount_points = _ensure_shadow_mount_points(cwd)
            executable = shutil.which("bwrap")
            if executable is None:
                raise PersonaContextError("persona_bwrap_missing")
            bwrap_executable = Path(executable).resolve(strict=True)
            destination = claude_home
            persona_bind = PersonaBind(generation_dir, destination)
            credential_bind = PersonaBind(
                real_credentials.resolve(), destination / ".credentials.json"
            )
            leaf_binds = tuple(
                PersonaBind(source, destination / leaf) for leaf, source in leaf_sources
            )
            shadow_binds = (
                PersonaBind(generation_dir / "cwd-shadow" / "CLAUDE.md", cwd / "CLAUDE.md"),
                PersonaBind(generation_dir / "cwd-shadow" / ".claude", cwd / ".claude"),
            )
            env_set = {"CLAUDE_CONFIG_DIR": str(destination)}
            env_unset = PERSONA_ENV_UNSET
        else:
            real_codex_home = provider_home("codex").home.resolve()
            codex_home = generation_dir / "codex-home"
            (staging / "codex-home").mkdir(mode=0o700)
            _write_codex_config(real_codex_home / "config.toml", staging / "codex-home/config.toml")
            auth = real_codex_home / "auth.json"
            if auth.is_symlink() or not auth.is_file():
                raise PersonaContextError("persona_codex_auth_invalid")
            os.symlink(str(auth.resolve()), staging / "codex-home/auth.json")
            for leaf, source in leaf_sources:
                shutil.copyfile(source, staging / "codex-home" / leaf)
                (staging / "codex-home" / leaf).chmod(0o600)

        os.rename(staging, generation_dir)
        plan = PersonaPlan(
            manifest_version=MANIFEST_VERSION,
            terminal_id=terminal_id,
            provider=provider,
            profile_name=profile_name,
            generation=generation,
            policy_hash=policy_hash,
            canonical_cwd=cwd,
            generation_dir=generation_dir,
            persona_bind=persona_bind,
            credential_bind=credential_bind,
            leaf_binds=leaf_binds,
            shadow_binds=shadow_binds,
            created_mount_points=created_mount_points,
            bwrap_executable=bwrap_executable,
            env_set=env_set,
            env_unset=env_unset,
            codex_home=codex_home,
            memory_instructions=_render_memory(selected),
        )
        _write_manifest(plan)
        _preflight(plan)
        link = terminal_root / f".current-{uuid.uuid4().hex}"
        os.symlink(generation, link)
        os.replace(link, terminal_root / "current")
        logger.info(
            "Composed persona terminal=%s profile=%s generation=%s kept=%d filtered=%d "
            "leaves=%d shadow_binds=%d",
            terminal_id,
            profile_name,
            generation,
            len(selected),
            max(0, len(list(corpus.glob("*.md"))) - 1 - len(selected)) if corpus.is_dir() else 0,
            len(leaf_binds),
            len(shadow_binds),
        )
        return plan
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(generation_dir, ignore_errors=True)
        raise


def _absolute_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise PersonaContextError(f"persona_manifest_invalid:{field}")
    return Path(value)


def _bind_from_json(value: object, field: str, *, optional: bool = False) -> PersonaBind | None:
    if value is None and optional:
        return None
    if not isinstance(value, dict) or set(value) != {"src", "dst"}:
        raise PersonaContextError(f"persona_manifest_invalid:{field}")
    return PersonaBind(
        _absolute_path(value["src"], f"{field}.src"), _absolute_path(value["dst"], f"{field}.dst")
    )


def _binds_from_json(value: object, field: str) -> tuple[PersonaBind, ...]:
    if not isinstance(value, list):
        raise PersonaContextError(f"persona_manifest_invalid:{field}")
    result: list[PersonaBind] = []
    for item in value:
        bind = _bind_from_json(item, field)
        assert bind is not None
        result.append(bind)
    return tuple(result)


def load_persona_plan(terminal_id: str) -> PersonaPlan | None:
    """Validate and rehydrate a frozen plan from its current manifest."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", terminal_id):
        raise PersonaContextError("persona_terminal_id_invalid")
    manifest_path = _persona_root(create=False) / terminal_id / "current" / "persona-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PersonaContextError("persona_manifest_unreadable") from exc
    expected = {
        "manifest_version",
        "terminal_id",
        "provider",
        "profile_name",
        "generation",
        "policy_hash",
        "canonical_cwd",
        "persona_bind",
        "credential_bind",
        "leaf_binds",
        "shadow_binds",
        "created_mount_points",
        "bwrap_executable",
        "env_set",
        "env_unset",
    }
    if not isinstance(data, dict) or set(data) != expected:
        raise PersonaContextError("persona_manifest_invalid:fields")
    if data["manifest_version"] != MANIFEST_VERSION:
        raise PersonaContextError("persona_manifest_version_unsupported")
    if data["terminal_id"] != terminal_id:
        raise PersonaContextError("persona_manifest_terminal_mismatch")
    provider = data["provider"]
    if provider not in {"claude_code", "codex"}:
        raise PersonaContextError("persona_manifest_provider_invalid")
    for field in ("profile_name", "generation", "policy_hash"):
        if not isinstance(data[field], str) or not data[field]:
            raise PersonaContextError(f"persona_manifest_invalid:{field}")
    generation_dir = manifest_path.parent.resolve()
    if generation_dir.name != data["generation"]:
        raise PersonaContextError("persona_manifest_generation_mismatch")
    canonical_cwd = _absolute_path(data["canonical_cwd"], "canonical_cwd").resolve()
    persona_bind = _bind_from_json(data["persona_bind"], "persona_bind", optional=True)
    credential_bind = _bind_from_json(data["credential_bind"], "credential_bind", optional=True)
    leaf_binds = _binds_from_json(data["leaf_binds"], "leaf_binds")
    shadow_binds = _binds_from_json(data["shadow_binds"], "shadow_binds")
    created_raw = data["created_mount_points"]
    if not isinstance(created_raw, list):
        raise PersonaContextError("persona_manifest_invalid:created_mount_points")
    created_mount_points = tuple(
        _absolute_path(item, "created_mount_points") for item in created_raw
    )
    env_set = data["env_set"]
    env_unset = data["env_unset"]
    if not isinstance(env_set, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env_set.items()
    ):
        raise PersonaContextError("persona_manifest_invalid:env_set")
    if not isinstance(env_unset, list) or not all(isinstance(item, str) for item in env_unset):
        raise PersonaContextError("persona_manifest_invalid:env_unset")
    bwrap_raw = data["bwrap_executable"]
    bwrap = _absolute_path(bwrap_raw, "bwrap_executable") if bwrap_raw is not None else None
    if provider == "claude_code":
        expected_env = (
            {"CLAUDE_CONFIG_DIR": str(persona_bind.dst)} if persona_bind is not None else {}
        )
        if (
            persona_bind is None
            or credential_bind is None
            or bwrap is None
            or env_set != expected_env
            or tuple(env_unset) != PERSONA_ENV_UNSET
        ):
            raise PersonaContextError("persona_manifest_invalid:claude_wrapper")
        if persona_bind.src.resolve() != generation_dir:
            raise PersonaContextError("persona_manifest_invalid:persona_bind.src")
        if credential_bind.dst != persona_bind.dst / ".credentials.json":
            raise PersonaContextError("persona_manifest_invalid:credential_bind.dst")
        if credential_bind.src.is_symlink() or not credential_bind.src.is_file():
            raise PersonaContextError("persona_manifest_invalid:credential_bind.src")
        if not bwrap.is_file():
            raise PersonaContextError("persona_manifest_invalid:bwrap_executable")
        for bind in leaf_binds:
            if (
                bind.src.is_symlink()
                or not bind.src.is_file()
                or bind.dst.parent != persona_bind.dst
                or bind.dst.name in RESERVED_LEAVES
            ):
                raise PersonaContextError("persona_manifest_invalid:leaf_binds")
        expected_shadows = (
            PersonaBind(generation_dir / "cwd-shadow" / "CLAUDE.md", canonical_cwd / "CLAUDE.md"),
            PersonaBind(generation_dir / "cwd-shadow" / ".claude", canonical_cwd / ".claude"),
        )
        if shadow_binds != expected_shadows:
            raise PersonaContextError("persona_manifest_invalid:shadow_binds")
        allowed_mount_points = {canonical_cwd / ".claude", canonical_cwd / "CLAUDE.md"}
        if any(path not in allowed_mount_points for path in created_mount_points):
            raise PersonaContextError("persona_manifest_invalid:created_mount_points")
    elif any((persona_bind, credential_bind, bwrap, leaf_binds, shadow_binds, env_set, env_unset)):
        raise PersonaContextError("persona_manifest_invalid:codex_wrapper")
    if not re.fullmatch(r"gen-\d+", data["generation"]) or not re.fullmatch(
        r"[0-9a-f]{64}", data["policy_hash"]
    ):
        raise PersonaContextError("persona_manifest_invalid:identity")
    memory_dir = generation_dir / "projects" / cwd_key(canonical_cwd) / "memory"
    memory_files = [path for path in sorted(memory_dir.glob("*.md")) if path.name != "MEMORY.md"]
    plan = PersonaPlan(
        manifest_version=MANIFEST_VERSION,
        terminal_id=terminal_id,
        provider=provider,
        profile_name=data["profile_name"],
        generation=data["generation"],
        policy_hash=data["policy_hash"],
        canonical_cwd=canonical_cwd,
        generation_dir=generation_dir,
        persona_bind=persona_bind,
        credential_bind=credential_bind,
        leaf_binds=leaf_binds,
        shadow_binds=shadow_binds,
        created_mount_points=created_mount_points,
        bwrap_executable=bwrap,
        env_set=dict(env_set),
        env_unset=tuple(env_unset),
        codex_home=(generation_dir / "codex-home" if provider == "codex" else None),
        memory_instructions=_render_memory(memory_files),
    )
    _preflight(plan)
    return plan


def has_persona_plan(terminal_id: str) -> bool:
    return load_persona_plan(terminal_id) is not None


def resolve_codex_home(terminal_id: str | None) -> Path:
    """Resolve live, retained, or production Codex home in that order."""
    if terminal_id is None:
        return provider_home("codex").home
    plan = load_persona_plan(terminal_id)
    if plan is not None and plan.provider == "codex" and plan.codex_home is not None:
        if plan.codex_home.is_dir():
            return plan.codex_home
    from cli_agent_orchestrator.clients.database import get_retained_persona_home_for_terminal

    try:
        retained = get_retained_persona_home_for_terminal(terminal_id)
    except Exception as exc:
        if "no such table: provider_sessions" not in str(exc):
            raise
        retained = None
    if retained:
        path = Path(retained)
        try:
            retained_root = (_persona_root(create=False) / "retained").resolve()
            resolved = path.resolve()
            if resolved.parent == retained_root and resolved.is_dir():
                return resolved
        except PersonaContextError:
            pass
    return provider_home("codex").home


def retained_persona_destination(session_uuid: str) -> Path:
    if not session_uuid or "/" in session_uuid or "\\" in session_uuid or "\x00" in session_uuid:
        raise PersonaContextError("retained_persona_uuid_invalid")
    destination = _persona_root() / "retained" / session_uuid
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    if (
        destination.parent.stat().st_uid != os.getuid()
        or stat.S_IMODE(destination.parent.stat().st_mode) != 0o700
    ):
        raise PersonaContextError("retained_persona_root_invalid")
    return destination


def retain_codex_persona_home(terminal_id: str, intent: PersonaRetentionIntent) -> str | None:
    """Claim, move, and verify one Codex persona home for a close operation."""
    plan = load_persona_plan(terminal_id)
    if plan is None or plan.provider != "codex" or plan.codex_home is None:
        return None
    source = plan.codex_home
    if not source.is_dir():
        return None
    from cli_agent_orchestrator.clients.database import (
        claim_retained_persona_home,
        unclaim_retained_persona_home,
        verify_retained_persona_claim,
    )

    destination = intent.destination
    expected_destination = retained_persona_destination(intent.session_uuid)
    if destination != expected_destination:
        raise PersonaContextError("retained_persona_destination_mismatch")
    claimed = claim_retained_persona_home(intent.session_uuid, str(destination))
    if claimed < 1:
        return None
    try:
        os.rename(source, destination)
    except OSError as exc:
        unclaim_retained_persona_home(intent.session_uuid, str(destination))
        logger.warning("Retained persona move failed for %s: %s", intent.session_uuid, exc)
        return "retained_persona_move_failed"
    if verify_retained_persona_claim(intent.session_uuid, str(destination)) == 0:
        _remove_persona_tree(destination, missing_ok=True)
        logger.warning(
            "Compensated retained persona home after ownership disappeared: %s", destination
        )
    return None


def _remove_persona_tree(path: Path, *, missing_ok: bool = False) -> None:
    """Remove a persona-owned tree only after checking credential invariants."""
    if not path.exists():
        if missing_ok:
            return
        raise FileNotFoundError(path)
    for candidate in path.rglob(".credentials.json"):
        if candidate.is_file() and not candidate.is_symlink():
            raise PersonaContextError("persona_regular_credentials_file_detected")
    shutil.rmtree(path)


def persona_cleanup(session_uuid: str, *, candidate_path: str | None = None) -> None:
    """Clear a UUID claim and remove its retained home after last-owner retirement."""
    from cli_agent_orchestrator.clients.database import persona_cleanup_claim

    cleanup_allowed, claimed_path = persona_cleanup_claim(session_uuid)
    if not cleanup_allowed:
        return
    path = claimed_path or candidate_path
    if path is None:
        return
    try:
        root = (_persona_root() / "retained").resolve()
        candidate = Path(path).resolve()
        if candidate != root and root not in candidate.parents:
            raise PersonaContextError("retained_persona_path_escape")
        _remove_persona_tree(candidate)
    except FileNotFoundError:
        return
    except (OSError, PersonaContextError) as exc:
        logger.warning("Failed to remove retained persona home %s: %s", path, exc)


def reconcile_retained_persona_homes() -> None:
    """Production-only startup sweep for claims and directories left by crashes."""
    from cli_agent_orchestrator.utils.sandbox_guard import is_sandbox

    if is_sandbox():
        return
    raw = os.environ.get("XDG_RUNTIME_DIR", "")
    if not raw:
        return
    try:
        retained_root = _persona_root() / "retained"
        retained_root.mkdir(mode=0o700, exist_ok=True)
    except PersonaContextError:
        logger.warning("Skipping persona retained-home sweep: runtime root is unavailable")
        return
    from cli_agent_orchestrator.clients.database import (
        clear_missing_retained_persona_claim,
        list_retained_persona_claims,
    )

    claims = list_retained_persona_claims()
    claimed_paths: dict[str, str] = {}
    for row in claims:
        path = Path(row["retained_persona_home"])
        expected = retained_root / row["session_uuid"]
        if path != expected or not path.is_dir():
            clear_missing_retained_persona_claim(row["session_uuid"], str(path))
        else:
            claimed_paths[str(path)] = row["session_uuid"]
    for candidate in retained_root.iterdir():
        if candidate.is_dir() and str(candidate) not in claimed_paths:
            _remove_persona_tree(candidate, missing_ok=True)


def reap_persona_generations(terminal_id: str) -> None:
    """Remove generations no longer referenced by the current provider."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", terminal_id):
        raise PersonaContextError("persona_terminal_id_invalid")
    terminal_root = _persona_root(create=False) / terminal_id
    if not terminal_root.exists():
        return
    current_link = terminal_root / "current"
    try:
        current = current_link.resolve(strict=True)
    except OSError as exc:
        raise PersonaContextError("persona_current_generation_invalid") from exc
    if current.parent != terminal_root.resolve() or not re.fullmatch(r"gen-\d+", current.name):
        raise PersonaContextError("persona_current_generation_invalid")
    for candidate in terminal_root.iterdir():
        if candidate.is_dir() and re.fullmatch(r"gen-\d+", candidate.name):
            if candidate.resolve() != current:
                _remove_persona_tree(candidate)


def cleanup_persona(terminal_id: str) -> None:
    """Idempotently remove all live generations owned by one terminal."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", terminal_id):
        raise PersonaContextError("persona_terminal_id_invalid")
    try:
        terminal_root = _persona_root(create=False) / terminal_id
    except PersonaContextError:
        return
    if not terminal_root.exists():
        return
    _remove_persona_tree(terminal_root)
