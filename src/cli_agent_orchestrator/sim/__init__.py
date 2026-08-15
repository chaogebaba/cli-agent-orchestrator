"""DST (Deterministic Simulation Testing) substrate for the message-delivery subsystem.

This package provides the simulation clock, seeded RNG, fault classes, tick driver,
and the SimWorld synchronous interface. Production code imports NOTHING from this
package — the seams live in the production modules and sim/ binds them.

See: orchestrator/blueprints/dst-liveness-harness.md
"""
