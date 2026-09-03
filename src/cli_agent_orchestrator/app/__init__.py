"""WP-ARCH phase 1 (F725 #581) — application services.

``app`` imports ``core`` and nothing else in the tree.  It never names an
adapter: the ``adapters-only-via-composition-root`` contract forbids it, and
``cli_agent_orchestrator/bootstrap.py`` hands in every dependency as a
``core.ports`` Protocol.  That is what lets the projector be tested against an
in-memory double with no SQLite anywhere in the test.
"""
