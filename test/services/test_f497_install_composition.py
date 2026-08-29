"""F497 P2 Ruling 1 — kiro install-time context-file composition (D2 addendum).

kiro delivers its persona via the INSTALL-TIME context file
(``agent-context/<name>.md``), which stores the UNRESOLVED source so ``${VAR}``
defers to runtime. A composition stub has an EMPTY body, so install must write
the COMPOSED body to the context file — the D2 blueprint assumed compose-at-spawn
covers every provider, but kiro composes at install (recorded as a D2 addendum).

Invariant under test: the install-time composed context body EQUALS the
spawn-time composed persona (the two composition paths agree). Plus: a
non-composition profile's context file is byte-identical to the raw source
(legacy path untouched).
"""

from __future__ import annotations

import pathlib
import shutil

import frontmatter
import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_PROFILES_ENV = "CAO_F497_PROFILES_DIR"


def _profiles_dir() -> "pathlib.Path | None":
    import os

    env = os.environ.get(_PROFILES_ENV, "").strip()
    cands = [pathlib.Path(env)] if env else []
    for anc in _HERE.parents:
        sib = anc / "profiles"
        if (sib / "positions").is_dir():
            cands.append(sib)
    for c in cands:
        if (c / "positions").is_dir():
            return c
    return None


_PROFILES = _profiles_dir()


@pytest.fixture()
def store(monkeypatch, tmp_path):
    if _PROFILES is None:
        pytest.skip("drafted persona corpus not found; set CAO_F497_PROFILES_DIR")
    store_root = tmp_path / "agent-store"
    (store_root / "positions").mkdir(parents=True)
    (store_root / "overlays").mkdir(parents=True)
    for f in (_PROFILES / "positions").glob("*.md"):
        shutil.copy(f, store_root / "positions" / f.name)
    for f in (_PROFILES / "overlays").glob("*.md"):
        shutil.copy(f, store_root / "overlays" / f.name)

    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.utils import agent_profiles

    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(constants, "LOCAL_AGENT_STORE_DIR", store_root)
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", store_root)
    return store_root


def _stub(store_root, name, position, provider, description="d", role="developer"):
    post = frontmatter.Post("")
    post["name"] = name
    post["provider"] = provider
    post["extends"] = position
    post["description"] = description
    post["role"] = role
    (store_root / f"{name}.md").write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def test_install_context_body_equals_raw_spawn_fragments(store, monkeypatch, tmp_path):
    """AC16 (D2 addendum): the install-written context body is byte-equal to the
    RAW composed fragments the spawn path feeds its resolver (pre-${VAR}).

    Both paths share ONE fragment merger — ``compose_agent_profile_source`` /
    ``compose_source_body`` in utils/agent_profiles — never two implementations.
    """
    from cli_agent_orchestrator.services.install_service import _write_context_file
    from cli_agent_orchestrator.utils.agent_profiles import (
        _resolve_composition_layers,
        read_agent_profile_source,
    )
    from cli_agent_orchestrator.utils.profile_composition import compose_source_body

    name = "kiro_er_probe"
    _stub(store, name, "empirical_reviewer", "kiro_cli")

    # Install path: write the context file from the raw stub source.
    raw = read_agent_profile_source(name)
    ctx_path = _write_context_file(name, raw)
    ctx = frontmatter.loads(ctx_path.read_text(encoding="utf-8"))
    install_body = ctx.content.strip()

    # RAW fragments the spawn path would merge (resolve_env=False = pre-${VAR}).
    raw_layers = _resolve_composition_layers("empirical_reviewer", "kiro_cli", resolve_env=False)
    raw_fragment_body = compose_source_body(raw_layers)

    assert install_body == raw_fragment_body, "install body != raw spawn-fragment composition"
    # And the context body is a REAL persona, not the empty stub.
    assert "Frozen Authority Pin protocol (F129)" in install_body
    # Inline clause markers survive verbatim into the context file (AC14 note).
    assert "<!-- clause:callback-contract -->" in install_body
    # Context file frontmatter carries the resolver-internal position axis.
    assert ctx.metadata.get("position") == "empirical_reviewer"


def test_legacy_context_file_is_byte_identical_to_raw(store, tmp_path):
    """A non-composition profile's context file is byte-identical to its source."""
    from cli_agent_orchestrator.services.install_service import _write_context_file

    legacy_src = "---\nname: plain\ndescription: d\nprovider: codex\n---\n\n# Plain\n\nbody\n"
    (store / "plain.md").write_text(legacy_src, encoding="utf-8")
    ctx_path = _write_context_file("plain", legacy_src)
    assert ctx_path.read_text(encoding="utf-8") == legacy_src


def test_var_deferral_preserved_in_context_body(store):
    """${VAR} in a fragment survives into the context file UNRESOLVED."""
    from cli_agent_orchestrator.services.install_service import _write_context_file
    from cli_agent_orchestrator.utils.agent_profiles import read_agent_profile_source

    # Append a ${VAR} line to the position fragment used by this stub.
    pos = store / "positions" / "empirical_reviewer.md"
    pos.write_text(pos.read_text(encoding="utf-8") + "\n\nEnv token: ${CAO_TEST_TOKEN_XYZ}\n")

    name = "kiro_er_var"
    _stub(store, name, "empirical_reviewer", "kiro_cli")
    raw = read_agent_profile_source(name)
    ctx_path = _write_context_file(name, raw)
    body = ctx_path.read_text(encoding="utf-8")
    assert "${CAO_TEST_TOKEN_XYZ}" in body, "install context file must NOT resolve ${VAR}"


def test_reinstall_recomposes_after_overlay_edit(store, monkeypatch, tmp_path):
    """Ruling 1(b): editing an overlay changes the installed context body on
    re-install — no stale caching (install always reads current fragments)."""
    from cli_agent_orchestrator.services.install_service import _write_context_file
    from cli_agent_orchestrator.utils.agent_profiles import read_agent_profile_source

    name = "kiro_er_reinstall"
    _stub(store, name, "empirical_reviewer", "kiro_cli")

    raw = read_agent_profile_source(name)
    body_before = _write_context_file(name, raw).read_text(encoding="utf-8")

    # Edit the kiro overlay: add a distinctive provider-note line.
    ov = store / "overlays" / "kiro_cli.empirical_reviewer.md"
    ov.write_text(ov.read_text(encoding="utf-8") + "\n\nDISTINCTIVE-REINSTALL-MARKER\n")

    body_after = _write_context_file(name, read_agent_profile_source(name)).read_text(
        encoding="utf-8"
    )
    assert "DISTINCTIVE-REINSTALL-MARKER" in body_after
    assert body_after != body_before


def test_d8_hash_invalidates_on_position_and_overlay_edit(store):
    """D8: the composed hash changes when EITHER the position OR an overlay input
    changes (compute-only in P2; not stamped until F127 #130)."""
    from cli_agent_orchestrator.utils.agent_profiles import _resolve_composition_layers
    from cli_agent_orchestrator.utils.profile_composition import composed_profile_hash

    layers = _resolve_composition_layers("empirical_reviewer", "kiro_cli", resolve_env=False)
    h0 = composed_profile_hash(layers)

    # Edit the position fragment → hash changes.
    pos = store / "positions" / "empirical_reviewer.md"
    pos.write_text(pos.read_text(encoding="utf-8") + "\n\nposition-edit\n")
    h1 = composed_profile_hash(
        _resolve_composition_layers("empirical_reviewer", "kiro_cli", resolve_env=False)
    )
    assert h1 != h0

    # Edit an overlay fragment → hash changes again.
    ov = store / "overlays" / "kiro_cli.empirical_reviewer.md"
    ov.write_text(ov.read_text(encoding="utf-8") + "\n\noverlay-edit\n")
    h2 = composed_profile_hash(
        _resolve_composition_layers("empirical_reviewer", "kiro_cli", resolve_env=False)
    )
    assert h2 != h1
