"""The published Python surface the orchestrator skill may import.

The one-paragraph law in the bridge repository's ``CLAUDE.md`` states the arrow:
*skill -> public CAO APIs, never the reverse* (wp-arch-modular-core A.1, "not
superseded, still binding").  ``cao-orchestrator`` and the command modules under
``cli/orchestrator_commands/`` are the skill side of that arrow, so they import
from this package and from nothing else inside ``cli_agent_orchestrator``.

That restriction is mechanised by the ``skill-cli-only-via-public-api``
import-linter contract (``pyproject.toml``).  Import-linter sees modules, not
names, which is exactly why the surface is a package: with every other base
module forbidden, no private *name* of the base package is reachable from the
seam either.

Stability: what this package exports is API.  Renaming or re-typing anything
here is a breaking change to the skill CLI and must be made in both repositories
in the same batch; the underlying ``services``/``cli`` internals it wraps stay
free to change.
"""

from __future__ import annotations
