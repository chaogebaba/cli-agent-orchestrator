"""Diagnostic rendering for ``cao diag`` (WP-ARCH phase 1, AC7).

Kept out of ``cli/`` on purpose.  The CLI's job is argument parsing and opening
the database read-only; deciding what an operator needs to see when a worker has
stalled is application logic, and it is testable only when it is a function from
rows to text rather than a Click callback.
"""
