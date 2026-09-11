"""F926 (#778): the shipped herdr agent-detection overrides must be usable.

These TOML files are consumed by a Rust binary, not by CAO, so nothing in the
test suite would otherwise notice if one stopped parsing, lost the rule that is
its whole reason for existing, or quietly reinstated the upstream catch-all this
override exists to remove.

Why each manifest ships (measured against herdr 0.9.0, protocol 22):

* ``pi`` — upstream's only rule is ``working_literal``, ``contains =
  ["Working..."]``. pi 0.85.1 renders ``── ⠴ Working ────`` in its composer
  border: a braille spinner, the bare word, no ellipsis of any kind. The rule
  cannot match, nothing else exists, and herdr falls back to
  ``default_known_agent_idle_fallback`` — so a pi pane mid-turn reports IDLE and
  CAO would deliver into a worker that is still thinking.
* ``cline`` — upstream ends with ``default_cline_working``, ``regex = ['(?s).+']``
  at priority -10, which matches any non-empty screen. A cline pane is therefore
  pinned at ``working`` for its whole life and never reports idle (upstream
  ogulcancelik/herdr#2396). A seat that is never idle is a seat nothing can be
  delivered to.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

MANIFEST_DIR = Path(__file__).resolve().parents[2] / (
    "src/cli_agent_orchestrator/backends/herdr_manifests"
)
EXPECTED = {"pi", "cline"}

#: herdr's own lifecycle vocabulary (`PaneAgentState` in `herdr api schema`).
VALID_STATES = {"idle", "working", "blocked", "unknown"}


def _load(name: str) -> dict:
    return tomllib.loads((MANIFEST_DIR / f"{name}.toml").read_text(encoding="utf-8"))


def test_the_directory_ships_exactly_the_manifests_we_claim():
    assert MANIFEST_DIR.is_dir(), MANIFEST_DIR
    found = {p.stem for p in MANIFEST_DIR.glob("*.toml")}
    assert found == EXPECTED, found


@pytest.mark.parametrize("name", sorted(EXPECTED))
class TestEachManifest:
    def test_it_parses(self, name):
        _load(name)

    def test_its_id_matches_its_filename(self, name):
        """herdr keys an override on the manifest it replaces; a mismatched id
        is refused and the remote manifest silently wins again."""
        assert _load(name)["id"] == name

    def test_it_declares_an_engine_version(self, name):
        assert _load(name)["min_engine_version"] >= 1

    def test_it_carries_an_idle_rule(self, name):
        """The point of the exercise: without one, a detected agent's idle is
        either the engine's generic fallback (pi) or never reached at all
        (cline)."""
        states = {r["state"] for r in _load(name)["rules"]}
        assert "idle" in states, states

    def test_it_carries_a_working_rule(self, name):
        states = {r["state"] for r in _load(name)["rules"]}
        assert "working" in states, states

    def test_every_rule_is_well_formed(self, name):
        for rule in _load(name)["rules"]:
            assert rule["id"], rule
            assert rule["state"] in VALID_STATES, rule
            assert isinstance(rule["priority"], int), rule
            assert rule["region"], rule

    def test_rule_ids_are_unique(self, name):
        ids = [r["id"] for r in _load(name)["rules"]]
        assert len(ids) == len(set(ids)), ids


class TestTheDefectsStayFixed:
    """Guard the specific upstream shapes these overrides exist to replace."""

    def test_cline_does_not_reinstate_the_catch_all(self):
        """`default_cline_working` is why a cline pane is never idle. Copying
        upstream's file wholesale to tune something else would bring it back."""
        rules = _load("cline")["rules"]
        assert not any(r["id"] == "default_cline_working" for r in rules)
        for rule in rules:
            if rule["state"] == "working":
                assert rule.get("regex") != ["(?s).+"], rule

    def test_cline_keeps_the_upstream_approval_rule(self):
        """An override REPLACES the manifest rather than merging with it, so a
        rule dropped here is a rule herdr no longer has."""
        rules = {r["id"]: r for r in _load("cline")["rules"]}
        assert "tool_permission" in rules
        assert rules["tool_permission"]["state"] == "blocked"

    def test_pi_matches_the_spinner_form_it_actually_renders(self):
        """pi 0.85.1 renders `⠴ Working` — spinner, bare word, no ellipsis."""
        import re

        rules = _load("pi")["rules"]
        patterns = [p for r in rules if r["state"] == "working" for p in r.get("line_regex", [])]
        assert patterns, "pi needs a working rule keyed on the rendered border"
        sample = "── ⠴ Working ──────────────────────────────"
        rust_class = r"[\x{2800}-\x{28FF}]"
        assert any(
            re.search(p.replace(rust_class, r"[⠀-⣿]"), sample) for p in patterns
        ), patterns

    def test_pi_working_rule_outranks_the_stale_upstream_one(self):
        """Upstream's `working_literal` is kept for a future pi that renders the
        literal string; ours must win while it does not."""
        rules = {r["id"]: r for r in _load("pi")["rules"]}
        assert "working_literal" in rules
        assert (
            rules["cao_composer_border_working"]["priority"] > rules["working_literal"]["priority"]
        )
