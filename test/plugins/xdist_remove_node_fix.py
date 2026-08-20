"""Monkey-patch for pytest-xdist#1362: LoadScopeScheduling.remove_node stale entry.

Problem (upstream unfixed as of xdist 3.8.0):
    LoadScopeScheduling.remove_node() pops the dead worker from
    ``assigned_work`` but leaves a stale entry in ``registered_collections``
    and does not decrement ``numnodes``. The stale entry satisfies
    ``collection_is_completed`` (``len(registered_collections) >= numnodes``)
    before a replacement worker finishes collecting, causing
    ``_assign_work_unit()`` to raise ``KeyError`` on the not-yet-registered
    node — crashing the entire run.

    This also affects ``--dist loadgroup`` because LoadGroupScheduling
    subclasses LoadScopeScheduling.

Fix:
    After calling the original ``remove_node``, purge the dead node from
    ``registered_collections`` and decrement ``numnodes`` so the collection
    gate waits for the replacement worker.

Tracking:
    - Upstream: https://github.com/pytest-dev/pytest-xdist/issues/1362
    - Fork issue: chaogebaba/cli-subagents#186 (F331)
    - Remove this plugin once xdist ships a release with the fix.
"""

from __future__ import annotations


def pytest_configure(config):
    """Apply the monkey-patch early, before any xdist scheduling begins."""
    try:
        from xdist.scheduler.loadscope import LoadScopeScheduling
    except ImportError:
        # xdist not installed (e.g. serial-only run) — nothing to patch.
        return

    _original_remove_node = LoadScopeScheduling.remove_node

    def _patched_remove_node(self, node):
        result = _original_remove_node(self, node)

        # Purge the stale entry that causes the KeyError race.
        self.registered_collections.pop(node, None)

        # Decrement numnodes so collection_is_completed waits for the
        # replacement worker (if one is spawned).
        if self.numnodes > 0:
            self.numnodes -= 1

        return result

    LoadScopeScheduling.remove_node = _patched_remove_node
