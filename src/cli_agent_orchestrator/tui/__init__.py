"""F702 (#557): the `cao-fleet` Textual TUI package.

Modules here are deliberately import-light: :mod:`status_cell` and
:mod:`columns` are pure and depend only on ``rich``, so they can be unit-tested
without a Textual app, a server, or the fetcher loop.
"""
