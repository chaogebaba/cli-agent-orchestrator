"""Commands owned by the optional orchestrator skill, not by base CAO.

Every module here is free to read skill-owned knowledge paths (the closed list
in ``wp-arch-modular-core.md`` A.2). Nothing under this package is imported by
``cli/main.py`` or by any service, so a project that never runs the separate
``cao-orchestrator`` console script never loads a line of it.
"""
