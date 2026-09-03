"""WP-ARCH phase 1 (F725 #581) — pure domain layer.

``core`` is the innermost package of the strangler tree described in
``orchestrator/blueprints/wp-arch-strangler.md`` §2 and the audit §2.1.  It holds
identifiers, the worker event vocabulary, the worker-state transition authority,
the port Protocols the outer layers implement, and every phase-1 duration.

Purity is enforced, not merely intended: the ``core-is-pure`` import-linter
contract forbids ``sqlite3``, ``sqlalchemy``, ``libtmux``, ``fastapi`` and
``httpx`` anywhere under this package, and ``new-code-never-imports-legacy``
forbids reaching into the pre-existing tree.  Dependencies are the standard
library and pydantic only.
"""
