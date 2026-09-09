"""herdr adapter leaf (WP-HERDR H1, blueprint §4).

``adapters/herdr/`` holds the single herdr transport leaf both seams call.
Per the blueprint §4 boundary table there is exactly ONE herdr client in the
tree at every point of the migration: ``client.py`` here is the socket/JSON-RPC
transport, and the legacy ``backends/herdr_backend.py`` shim imports it (legacy
importing new code is permitted; the reverse is not).

Nothing in this package may import the legacy tree or ``app``/``api``/
``services`` — it is a leaf under the ``adapters-are-leaves`` and
``new-code-never-imports-legacy`` import-linter contracts, so it depends on
``core.*`` and the stdlib only.
"""

from __future__ import annotations
