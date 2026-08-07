"""F107 AC7 — KAS engine-persistence round-trip (A2 + A3 + B5)."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.providers.manager import ProviderManager


def test_kas_engine_persistence_round_trip_ac7(tmp_path, monkeypatch):
    """Pinned 5-step sequence from blueprints/f107-kiro-kas-enablement.md AC7.

    (1) create KAS terminal row + assert engine
    (2) live-cache get_provider returns SAME object (A2)
    (3) cleanup_provider evicts map, preserves row
    (4) get_provider restores NEW provider from metadata['engine'] (A3/B5)
    (5) ordinary cleanup
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'terminals.db'}")
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        db_mod.get_terminal_metadata,
    )

    # (1) create KAS terminal row and assert engine column
    created = db_mod.create_terminal(
        "kasterm1",
        "cao-session",
        "kas-window",
        ProviderType.KIRO_CLI.value,
        "developer",
        engine="kas",
    )
    assert created["engine"] == "kas"
    assert db_mod.get_terminal_metadata("kasterm1")["engine"] == "kas"

    manager = ProviderManager()
    first = manager.create_provider(
        ProviderType.KIRO_CLI.value,
        terminal_id="kasterm1",
        tmux_session="cao-session",
        tmux_window="kas-window",
        agent_profile="developer",
        engine=KiroEngine.KAS,
    )
    assert isinstance(first, KiroCliProvider)
    assert first._engine == KiroEngine.KAS

    # (2) live-cache: same object through get_provider (grades A2)
    cached = manager.get_provider("kasterm1")
    assert cached is first

    # (3) evict in-memory mapping while preserving the row
    manager.cleanup_provider("kasterm1")
    assert "kasterm1" not in manager._providers
    assert db_mod.get_terminal_metadata("kasterm1")["engine"] == "kas"

    # (4) restore constructs a NEW provider from metadata["engine"] (A3/B5)
    restored = manager.get_provider("kasterm1")
    assert isinstance(restored, KiroCliProvider)
    assert restored is not first
    assert restored._engine == KiroEngine.KAS
    # second live-cache hit returns the restored instance
    assert manager.get_provider("kasterm1") is restored

    # (5) ordinary cleanup
    manager.cleanup_provider("kasterm1")
    assert "kasterm1" not in manager._providers
