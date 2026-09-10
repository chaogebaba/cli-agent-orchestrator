"""F786 (#643) stage 1 — D8 composed store + writer, D11 fallback, D12b clause check.

Uses a temp CAO_HOME with a minimal positions/overlays store so composition and
the composed/ store are exercised for real (not mocked).
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def cao_home(tmp_path, monkeypatch):
    """A temp CAO home with a minimal positions store and a provider overlay.

    F786 (#643) r2: this fixture ``importlib.reload(constants)`` so the
    module-level store/DB paths pick up CAO_HOME_DIR. That reload is NOT undone
    by monkeypatch's env revert, so WITHOUT an explicit teardown reload the
    reloaded ``constants`` (incl. ``DATABASE_FILE``) stays bound to this
    now-deleted tmp home — poisoning every LATER test on the same xdist worker
    with ``unable to open database file`` / ``init_db`` failures (the r2 A/B
    head-only leak: test_worktree_branch_integrity, test_seam_parity_promotion).
    We reload constants (and agent_profiles, which caches store dirs at import)
    a SECOND time on teardown, after restoring the real CAO_HOME_DIR, so module
    state re-binds to the real home.
    """
    import os

    import cli_agent_orchestrator.constants as constants

    _prev_home = os.environ.get("CAO_HOME_DIR")

    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    store = tmp_path / "agent-store"
    (store / "positions").mkdir(parents=True)
    (store / "overlays").mkdir(parents=True)
    (store / "positions" / "dev.md").write_text(
        "---\nname: dev\nrole: developer\n---\n\n# DEV persona\n"
    )
    (store / "positions" / "general.md").write_text(
        "---\nname: general\nrole: developer\n---\n\n# GENERAL persona\n"
    )
    (store / "overlays" / "kiro_cli.md").write_text(
        "---\nprovider: kiro_cli\n---\n\n# kiro overlay\n"
    )
    # Reload constants ONLY so CAO_HOME_DIR takes effect for the module-level
    # LOCAL_AGENT_STORE_DIR constant used by some helpers. (agent_profiles is
    # NOT reloaded — reloading it swaps its class objects, an xdist-order skew;
    # the composed store is read via the call-time composed_store_dir().)
    importlib.reload(constants)
    try:
        yield tmp_path
    finally:
        # Restore the real CAO_HOME_DIR BEFORE the reload so module-level paths
        # re-bind to the real home (monkeypatch's own env revert runs only after
        # this fixture unwinds, which would be too late for the reload).
        if _prev_home is None:
            os.environ.pop("CAO_HOME_DIR", None)
        else:
            os.environ["CAO_HOME_DIR"] = _prev_home
        # Reload ONLY constants: that is what carries DATABASE_FILE and the store
        # dirs the leak poisoned. Do NOT reload agent_profiles here — reloading a
        # module swaps its class objects (e.g. AssignmentResolutionError), and
        # any test holding a module-import reference to the OLD class would then
        # see pytest.raises(...) miss a NEW-class instance (an xdist-order skew of
        # the same shape as the DB leak). constants defines no such classes.
        importlib.reload(constants)


# ---------------------------------------------------------------------------
# AC8 — composed writer + load + fail-closed
# ---------------------------------------------------------------------------
class TestAC8ComposedStore:
    def test_writer_materialises_and_load_finds_it(self, cao_home):
        from cli_agent_orchestrator.utils.agent_profiles import (
            load_agent_profile,
            write_composed_profile_for_spawn,
        )

        path = write_composed_profile_for_spawn("dev-kiro_cli", "kiro_cli")
        assert path is not None and path.exists()
        assert path.parent.name == "composed"
        prof = load_agent_profile("dev-kiro_cli")
        assert prof.name == "dev-kiro_cli"
        assert prof.position == "dev"
        assert prof.provider == "kiro_cli"

    def test_writer_is_idempotent(self, cao_home):
        from cli_agent_orchestrator.utils.agent_profiles import write_composed_profile_for_spawn

        p1 = write_composed_profile_for_spawn("dev-kiro_cli", "kiro_cli")
        b1 = p1.read_bytes()
        p2 = write_composed_profile_for_spawn("dev-kiro_cli", "kiro_cli")
        assert p2 == p1
        assert p2.read_bytes() == b1

    def test_writer_noop_for_legacy_name(self, cao_home):
        """MUTANT: composed write skipped for one provider → this would still be None,
        but the fail-closed loader (below) catches the real skip."""
        from cli_agent_orchestrator.utils.agent_profiles import write_composed_profile_for_spawn

        assert write_composed_profile_for_spawn("kiro_dev", "kiro_cli") is None

    def test_bytes_equal_in_memory_composition(self, cao_home):
        """AC8 parity: the written source equals compose_position_profile_for_spawn's."""
        from cli_agent_orchestrator.utils.agent_profiles import (
            compose_position_profile_for_spawn,
            write_composed_profile_for_spawn,
        )

        _prof, source = compose_position_profile_for_spawn("dev-kiro_cli", "kiro_cli")
        path = write_composed_profile_for_spawn("dev-kiro_cli", "kiro_cli")
        assert path.read_text(encoding="utf-8") == source

    def test_missing_composed_name_fails_closed(self, cao_home):
        """AC8 / MUTANT #9: profile-less fallback restored at terminal_service
        (``if agent_profile:`` → ``if False:``). A composed NAMED profile with
        no store file must drive ``create_terminal`` to raise E-PROFILE-MISSING
        BEFORE any tmux window or DB row — not silently become a None profile
        and a native spawn.

        The prior version of this test only asserted ``load_agent_profile`` (a
        pure helper) raised FileNotFoundError and never entered
        ``create_terminal``, so it could not observe the fail-closed raise and
        the mutant survived (EMPIRICAL gate r1 B3). This drives the real
        create_terminal test seam so the raise site is exercised."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from cli_agent_orchestrator.services.terminal_service import (
            ProfileMissingError,
            create_terminal,
        )

        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.load_agent_profile",
                side_effect=FileNotFoundError("no such composed profile"),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.generate_terminal_id",
                return_value="tid-b3",
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.db_create_terminal"
            ) as mock_db_create,
            patch("cli_agent_orchestrator.services.terminal_service.provider_manager"),
            patch("cli_agent_orchestrator.backends.registry._backend") as mock_backend,
            patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR"),
            patch("cli_agent_orchestrator.services.terminal_service.fifo_manager"),
            patch("cli_agent_orchestrator.services.terminal_service.status_monitor"),
        ):
            # A never-existing session so the mutant path (which continues past
            # the raise) cannot incidentally trip the "session exists" guard —
            # the ONLY lawful exit is the E-PROFILE-MISSING raise.
            mock_backend.session_exists = MagicMock(return_value=False)
            mock_backend.create_session = MagicMock()
            mock_backend.create_window = MagicMock()

            async def _drive():
                with pytest.raises(ProfileMissingError, match="E-PROFILE-MISSING"):
                    await create_terminal(
                        provider="kiro_cli",
                        agent_profile="empirical_reviewer-codex",  # NAMED, no store file
                        new_session=True,
                        allowed_tools=["*"],
                    )

            asyncio.run(_drive())
            # Fail-closed BEFORE any tmux window / DB row.
            mock_backend.create_window.assert_not_called()
            mock_db_create.assert_not_called()


class TestAC8SeamWitness:
    """AC8 / MUTANT #10 — the D8 composed-writer SEAM in ``_assign_impl``.

    r2 BLOCKER (B-1): mutant #10 is the server-seam guard
    ``if _resolved_provider:`` → ``if False:`` at
    ``mcp_server/server.py`` (the ``write_composed_profile_for_spawn`` call).
    Neither named AC test killed it: ``TestAC8ComposedStore`` calls the writer
    DIRECTLY (never through the mutated seam), and
    ``test_d7_position_plus_provider_spawns_composed`` patches ``_create_terminal``
    and asserts only the RESOLVED NAME — never the ``composed/`` side-effect.

    This drives the REAL ``_assign_impl`` for a position + provider against the
    ``cao_home`` temp store and asserts the composed profile is materialised
    under ``composed_store_dir()`` with the expected name and byte-content.
    Terminal creation is stubbed at the LOWEST seam BELOW the writer — the
    module-level ``cao_http`` HTTP client that ``_create_terminal`` calls — NOT
    ``_create_terminal`` itself (which the r2 AC3 test patched, above the seam
    of interest). With mutant #10 applied the ``composed/`` write is skipped and
    the file never appears, so this FAILS; unmutated it PASSES.
    """

    def test_assign_impl_materialises_composed_profile_through_seam(self, cao_home, monkeypatch):
        # F868 #724 — a position-name assign (routing-driven OR explicit
        # provider=) now runs the cell-certification gate before the D8 writer.
        # Certify general (provider gate) + the dev cell for kiro_cli at their
        # CURRENT shas, and bind dev→kiro_cli in a routing.toml, so this
        # seam-witness reaches the writer instead of being refused
        # E-CELL-UNCERTIFIED. Uses the f497 helpers (they hash the actual files).
        from test.mcp_server.test_f497_routing_d9 import _certify, _write
        from unittest.mock import MagicMock, patch

        from cli_agent_orchestrator.constants import composed_store_dir
        from cli_agent_orchestrator.mcp_server import server
        from cli_agent_orchestrator.utils.agent_profiles import (
            compose_position_profile_for_spawn,
            load_agent_profile,
        )

        positions = cao_home / "agent-store" / "positions"
        # The fixture omits a clause table; the cert gate + resolver need one.
        # Give dev/general EMPTY required sets so the row-clause check trivially
        # passes and cell certification is what gates.
        _write(
            positions / "_clauses.toml",
            '[clauses.callback-contract]\nmarker = "<!-- clause:callback-contract -->"\n\n'
            "[required]\ngeneral = []\ndev = []\n\n"
            "[budget]\ngeneral = 2500\ndev = 6000\noverlay = 1200\ncomposed_slack = 500\n",
        )
        _certify(positions, "general", "kiro_cli", "PASS")
        _certify(positions, "dev", "kiro_cli", "PASS")
        rt = cao_home / "routing.toml"
        _write(
            rt,
            """\
            [[binding]]
            position = "dev"
            provider = "kiro_cli"
            kind = "cao"
            """,
        )
        monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))

        # A CAO_TERMINAL_ID is required so _assign_impl / _create_terminal take
        # the existing-session branch (the only branch that stubs cleanly and
        # the one a real assign uses).
        monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

        # LOWEST seam below the writer: _create_terminal's two HTTP calls.
        # .get -> the caller's terminal metadata (status 200, provider+session);
        # .post -> the created worker terminal row. Everything ABOVE this in
        # _assign_impl — including the D8 composed-writer guard — runs for real.
        def _fake_get(path, *a, **k):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "provider": "kiro_cli",
                    "session_name": "sess-witness",
                    "allowed_tools": ["*"],
                }
            )
            return resp

        def _fake_post(path, *a, **k):
            resp = MagicMock()
            resp.status_code = 201
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"id": "worker77"})
            return resp

        fake_http = MagicMock()
        fake_http.get = MagicMock(side_effect=_fake_get)
        fake_http.post = MagicMock(side_effect=_fake_post)

        target = composed_store_dir() / "dev-kiro_cli.md"
        assert not target.exists()  # nothing written before the assign

        with patch.object(server, "cao_http", fake_http):
            result = server._assign_impl(
                "dev", "implement the widget", working_directory="/repo", provider="kiro_cli"
            )

        # The assign resolved and reached the create seam (post was hit) …
        assert result["success"] is True, result
        assert result["terminal_id"] == "worker77"
        fake_http.post.assert_called_once()

        # … and the D8 seam materialised the composed profile under composed/.
        assert target.exists(), "composed/dev-kiro_cli.md not materialised by the assign seam"
        assert target.parent.name == "composed"

        # Byte-content parity with the direct composer (same source the writer used).
        _prof, expected_source = compose_position_profile_for_spawn("dev-kiro_cli", "kiro_cli")
        assert target.read_text(encoding="utf-8") == expected_source

        # The materialised file loads as the composed profile it names.
        loaded = load_agent_profile("dev-kiro_cli")
        assert loaded.name == "dev-kiro_cli"
        assert loaded.position == "dev"
        assert loaded.provider == "kiro_cli"


class TestProfileMissingError:
    def test_error_is_valueerror_with_code(self):
        from cli_agent_orchestrator.services.terminal_service import ProfileMissingError

        assert issubclass(ProfileMissingError, ValueError)
        err = ProfileMissingError("E-PROFILE-MISSING: x")
        assert "E-PROFILE-MISSING" in str(err)


# ---------------------------------------------------------------------------
# D11 — the general fallback derives general-<provider> (no stub scan)
# ---------------------------------------------------------------------------
class TestD11FallbackDerivation:
    def test_fallback_name_is_general_hyphen_provider(self):
        """MUTANT: fallback scans the flat store for a stub instead of composing
        general-<provider>. Assert the pure derivation."""
        from cli_agent_orchestrator.utils.agent_profiles import _synthesise_position_profile_name

        assert _synthesise_position_profile_name("general", "cline_cli") == "general-cline_cli"

    def test_find_alias_for_cell_is_deleted(self):
        """The stub-scan helper and its E-ALIAS-MISSING path are gone (D2b/D11)."""
        from cli_agent_orchestrator.utils import agent_profiles

        assert not hasattr(agent_profiles, "_find_alias_for_cell")
