"""Reconcile live provider configuration with the workspace template."""

from __future__ import annotations

import fcntl
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

import click

if sys.version_info >= (3, 11):
    import tomllib as toml
else:  # pragma: no cover - exercised on Python 3.10
    import tomli as toml  # type: ignore[import-not-found]

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.services.settings_service import (
    get_agent_dirs,
    get_disabled_agent_dirs,
    get_extra_agent_dirs,
)
from cli_agent_orchestrator.services.verification_service import cli_deploy_root, git_root
from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles
from cli_agent_orchestrator.utils.sandbox_guard import require_not_sandbox_mutation

_PROVIDERS_FILENAME = "providers.toml"
_TEMPLATE_FILENAME = "providers.toml.default"


def _workspace_root() -> Path:
    return cli_deploy_root(git_root()).parent


def _load_template(workspace_root: Path) -> tuple[bytes, dict[str, Any]]:
    template_path = workspace_root / _TEMPLATE_FILENAME
    try:
        template_bytes = template_path.read_bytes()
        template = toml.loads(template_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, toml.TOMLDecodeError) as exc:
        raise click.ClickException(f"invalid providers template: {exc}") from exc
    return template_bytes, template


def _load_live(target: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not target.exists():
        return None, "missing"
    try:
        return toml.loads(target.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, toml.TOMLDecodeError):
        return None, "invalid"


def _differing_keys(
    live: Any,
    template: Any,
    prefix: tuple[str, ...] = (),
) -> list[str]:
    if isinstance(live, dict) and isinstance(template, dict):
        differences: list[str] = []
        for key in sorted(set(live) | set(template)):
            path = (*prefix, str(key))
            if key not in live or key not in template:
                differences.append(".".join(path))
            else:
                differences.extend(_differing_keys(live[key], template[key], path))
        return differences
    return [".".join(prefix) or "<root>"] if live != template else []


def _emit_template_drift(
    target: Path,
    template: dict[str, Any],
    *,
    show_empty: bool = False,
) -> None:
    live, error = _load_live(target)
    if error == "missing":
        click.echo(
            "drift: providers.toml is missing; run with --force-providers to reset it",
            err=True,
        )
        return
    if error == "invalid":
        click.echo(
            "drift: providers.toml is invalid TOML; run with --force-providers to reset it",
            err=True,
        )
        return
    differences = _differing_keys(live, template)
    for key in differences:
        click.echo(
            f"drift: providers.toml differs at {key}; " "run with --force-providers to reset it",
            err=True,
        )
    if show_empty and not differences:
        click.echo("providers.toml diff: no semantic key differences", err=True)


def _built_in_profile_names() -> set[str]:
    store = resources.files("cli_agent_orchestrator.agent_store")
    return {item.name[:-3] for item in store.iterdir() if item.name.endswith(".md")}


def _profile_directories() -> list[Path]:
    candidates = [
        CAO_HOME_DIR / "agent-store",
        CAO_HOME_DIR / "agent-context",
        *(Path(value) for value in get_agent_dirs().values()),
        *(Path(value) for value in get_extra_agent_dirs()),
    ]
    directories: list[Path] = []
    seen: set[Path] = set()
    disabled = {Path(value).expanduser().resolve() for value in get_disabled_agent_dirs()}
    for candidate in candidates:
        normalized = candidate.expanduser().resolve()
        if normalized not in seen and normalized not in disabled:
            seen.add(normalized)
            directories.append(normalized)
    return directories


def _profile_locations(name: str) -> list[Path]:
    locations: list[Path] = []
    for directory in _profile_directories():
        flat = directory / f"{name}.md"
        nested = directory / name / "agent.md"
        if flat.is_file():
            locations.append(directory)
        elif nested.is_file():
            locations.append(directory)
    return locations


def _emit_profile_drift(workspace_root: Path, live: dict[str, Any] | None) -> None:
    try:
        profiles = list_agent_profiles()
        built_in_names = _built_in_profile_names()
        workspace_names = {path.stem for path in (workspace_root / "profiles").glob("*.md")}
        by_name = {str(profile.get("name")): profile for profile in profiles}

        for name, profile in sorted(by_name.items()):
            if name in workspace_names:
                continue
            locations = _profile_locations(name)
            if name in built_in_names:
                duplicated_in = profile.get("duplicated_in")
                shadows_builtin = (
                    profile.get("source") != "built-in"
                    and isinstance(duplicated_in, list)
                    and "built-in" in duplicated_in
                )
                if shadows_builtin:
                    rendered = ", ".join(str(path) for path in locations) or str(
                        profile.get("source")
                    )
                    click.echo(
                        f"drift: SHADOW profile {name} in {rendered} shadows a built-in profile",
                        err=True,
                    )
                continue
            rendered_locations = locations or [Path(str(profile.get("source", "unknown")))]
            for directory in rendered_locations:
                click.echo(f"drift: orphan profile {name} in {directory}", err=True)

        if live is None:
            return
        for provider, provider_defaults in sorted(live.items()):
            if not isinstance(provider_defaults, dict):
                continue
            stanza_profiles = provider_defaults.get("profiles")
            if not isinstance(stanza_profiles, dict):
                continue
            for name in sorted(stanza_profiles):
                stanza_profile = by_name.get(str(name))
                stanza = f"[{provider}.profiles.{name}]"
                if stanza_profile is None:
                    click.echo(f"drift: {stanza} names no known profile", err=True)
                elif not bool(stanza_profile.get("loadable")):
                    click.echo(
                        f"drift: {stanza} names a profile that fails to load",
                        err=True,
                    )
    except Exception as exc:
        click.echo(f"drift: profile audit unavailable ({type(exc).__name__})", err=True)


def _audit(
    workspace_root: Path, target: Path, template: dict[str, Any], *, show_empty: bool
) -> None:
    live, _ = _load_live(target)
    _emit_profile_drift(workspace_root, live)
    _emit_template_drift(target, template, show_empty=show_empty)


@contextmanager
def _config_lock() -> Iterator[None]:
    lock_path = CAO_HOME_DIR / f"{_PROVIDERS_FILENAME}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise click.ClickException("another redeploy holds the config lock") from exc
        yield


def _backup_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_backup(target: Path) -> Path:
    base = target.with_name(f"{target.name}.bak.{_backup_timestamp()}")
    suffix = 1
    while True:
        backup = base if suffix == 1 else base.with_name(f"{base.name}-{suffix}")
        try:
            with backup.open("xb") as stream:
                stream.write(target.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            return backup
        except FileExistsError:
            suffix += 1
        except Exception:
            backup.unlink(missing_ok=True)
            raise


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_publish(target: Path, template_bytes: bytes) -> None:
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(template_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(target.parent)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _reconcile_config(
    workspace_root: Path,
    *,
    force_providers: bool,
    audit_only: bool,
) -> None:
    template_bytes, template = _load_template(workspace_root)
    target = (CAO_HOME_DIR / _PROVIDERS_FILENAME).resolve()

    if audit_only:
        _audit(workspace_root, target, template, show_empty=False)
        return

    if target.exists() and not force_providers:
        _audit(workspace_root, target, template, show_empty=False)
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    audit_after_publish = False
    with _config_lock():
        target_exists = target.exists()
        # Another seed may win between the unlocked existence check and lock acquisition.
        if target_exists and not force_providers:
            audit_after_publish = True
        elif target_exists:
            _audit(workspace_root, target, template, show_empty=True)
            _write_backup(target)
            _atomic_publish(target, template_bytes)
        else:
            _atomic_publish(target, template_bytes)
            audit_after_publish = True
    if audit_after_publish:
        _audit(workspace_root, target, template, show_empty=False)


@click.command(name="reconcile")
@click.option(
    "--force-providers",
    is_flag=True,
    help="Back up and reset providers.toml from the workspace template.",
)
@click.option(
    "--audit-only",
    is_flag=True,
    help="Report drift without seeding, backing up, or writing providers.toml.",
)
def reconcile(force_providers: bool, audit_only: bool) -> None:
    """Seed or reconcile providers.toml and report configuration drift."""
    if not audit_only:
        require_not_sandbox_mutation("config reconcile")
    try:
        _reconcile_config(
            _workspace_root(),
            force_providers=force_providers,
            audit_only=audit_only,
        )
    except click.ClickException:
        raise
    except OSError as exc:
        raise click.ClickException(f"config reconcile failed: {exc}") from exc
