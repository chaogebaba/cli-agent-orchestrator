"""Tests for the cao ledger check command (F224)."""

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.ledger import ledger


# --- Fixtures: HANDOFF.md content variants ---

_CANONICAL_TABLE = """\
## Overview

Some overview text.

## Live ledger

| Feature | Load-bearing assertion | Owning commit | Activation path | Status | Notes |
|---------|------------------------|---------------|-----------------|--------|-------|
| F100 | assertion A | abc1234 | /path/a | drained-pass | done |
| F101 | assertion B | def5678 | /path/b | drained-fail | failed |
| F102 | assertion C | ghi9012 | /path/c | pending | waiting |
| F103 | assertion D | jkl3456 | /path/d | PENDING | dup |
| F104 | assertion E | mno7890 | /path/e | pending-activation | act |

## POST-RESTART RE-ENTRY

Re-entry checklist:
- Verify F100 still works
- Check F200 status

## Other section
"""

_LEGACY_BULLETS = """\
## Overview

Some overview text.

## Live ledger additions (migration)

- F100 ... status: drained-pass
- F101 ... status: DRAINED-FAIL
- F102 ... status: PENDING
- F103 ... status: pending
- F104 ... status: pending-activation

## POST-RESTART RE-ENTRY

Re-entry checklist:
- Verify F100 status
- Check F200 status

## Other section
"""

_UNRELATED_TABLE_OUTSIDE = """\
## Other section with table

| Feature | Col2 | Col3 | Col4 | Status | Col6 |
|---------|------|------|------|--------|------|
| Fake1 | x | y | z | pending | note |
| Fake2 | x | y | z | pending | note |
| Fake3 | x | y | z | pending | note |

## Live ledger

| Feature | Load-bearing assertion | Owning commit | Activation path | Status | Notes |
|---------|------------------------|---------------|-----------------|--------|-------|
| F200 | real assertion | abc | /x | pending | real |

## POST-RESTART RE-ENTRY

Nothing here.
"""

_MALFORMED_AND_UNKNOWN = """\
## Live ledger

| Feature | Load-bearing assertion | Owning commit | Activation path | Status | Notes |
|---------|------------------------|---------------|-----------------|--------|-------|
| F300 | assertion | abc | /x | pending | ok |
| F301 | assertion | def | /y | banana | bad |
| F302 | assertion | ghi | /z | | empty |
| F303 | assertion | jkl | /w | drained-pass | ok |

## POST-RESTART RE-ENTRY

Nothing.
"""

_STALE_REENTRY = """\
## Live ledger

| Feature | Load-bearing assertion | Owning commit | Activation path | Status | Notes |
|---------|------------------------|---------------|-----------------|--------|-------|
| F400 | assertion A | abc | /x | drained-pass | done |
| F401 | assertion B | def | /y | pending | waiting |

## POST-RESTART RE-ENTRY

Re-entry checklist:
- Verify F400 integration
- Check F401 status
"""

_NO_LEDGER_SECTION = """\
## Overview

Some overview text.

## POST-RESTART RE-ENTRY

Just re-entry, no ledger section at all.
"""

# B1: exact-match boundary fixtures
_EXACT_MATCH_POSITIVE = """\
## Live ledger

| Feature | Load-bearing assertion | Owning commit | Activation path | Status | Notes |
|---------|------------------------|---------------|-----------------|--------|-------|
| F20 | assertion A | abc | /x | drained-pass | done |
| D7-A | assertion B | def | /y | drained-fail | done |

## POST-RESTART RE-ENTRY

Re-entry checklist:
- Verify F20 integration
- Check (D7-A) status
"""

_EXACT_MATCH_NEGATIVE = """\
## Live ledger

| Feature | Load-bearing assertion | Owning commit | Activation path | Status | Notes |
|---------|------------------------|---------------|-----------------|--------|-------|
| F20 | assertion A | abc | /x | drained-pass | done |

## POST-RESTART RE-ENTRY

Re-entry checklist:
- Check F203 integration
- Verify F200 status
"""

# S1: verified status fixture (real historical shape)
_VERIFIED_HISTORICAL = """\
## Live ledger additions (2026-08-15 arm)

- F203/F206 batch ... status: VERIFIED 2026-08-15 post-deploy

## POST-RESTART RE-ENTRY

Re-entry checklist:
- Validate F203/F206 batch results
"""

# S2: archive + multiple canonical headings (use last)
_MULTIPLE_HEADINGS_WITH_ARCHIVE = """\
## Live ledger archive

| Feature | Load-bearing assertion | Owning commit | Activation path | Status | Notes |
|---------|------------------------|---------------|-----------------|--------|-------|
| OLD1 | old assertion | old | /old | pending | archive |
| OLD2 | old assertion | old | /old | pending | archive |

## Live ledger

| Feature | Load-bearing assertion | Owning commit | Activation path | Status | Notes |
|---------|------------------------|---------------|-----------------|--------|-------|
| MID1 | mid assertion | mid | /mid | pending | first canonical |

## Live ledger additions (2026-08-15 arm)

| Feature | Load-bearing assertion | Owning commit | Activation path | Status | Notes |
|---------|------------------------|---------------|-----------------|--------|-------|
| NEW1 | new assertion | new | /new | pending | latest |

## POST-RESTART RE-ENTRY

Nothing.
"""


class TestLedgerCheckCanonicalTable:
    """Case 1: canonical table 2 drained + 3 pending => count 3."""

    def test_canonical_table_counts(self, tmp_path: Path):
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_CANONICAL_TABLE)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        assert "pending-row count: 3" in result.output


class TestLedgerCheckLegacyBullets:
    """Case 2: legacy bullets same statuses => count 3."""

    def test_legacy_bullet_counts(self, tmp_path: Path):
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_LEGACY_BULLETS)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        assert "pending-row count: 3" in result.output


class TestLedgerCheckUnrelatedTableIgnored:
    """Case 3: unrelated 6-column table outside live ledger => ignored."""

    def test_unrelated_table_outside_ledger_ignored(self, tmp_path: Path):
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_UNRELATED_TABLE_OUTSIDE)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        # Only the one real pending row inside the ledger section.
        assert "pending-row count: 1" in result.output


class TestLedgerCheckMalformedAndUnknownStatus:
    """Case 4: malformed row and unknown status => explicit warning, no false pending."""

    def test_unknown_status_warns_not_pending(self, tmp_path: Path):
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_MALFORMED_AND_UNKNOWN)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        # Only F300 is pending; F301 (banana) and F302 (empty) are unknown.
        assert "pending-row count: 1" in result.output
        assert "warning: unrecognized ledger status 'banana'" in result.output
        assert "warning: unrecognized ledger status ''" in result.output


class TestLedgerCheckStaleReentry:
    """Case 5: stale re-entry warning fires for drained feature, not pending."""

    def test_stale_reentry_only_for_drained(self, tmp_path: Path):
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_STALE_REENTRY)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        # F400 is drained and named in re-entry => warning.
        assert "warning: POST-RESTART RE-ENTRY names drained feature: F400" in result.output
        # F401 is pending and named in re-entry => NO stale warning.
        assert "F401" not in result.output.replace("pending-row count: 1", "")
        assert "pending-row count: 1" in result.output


class TestLedgerCheckNoLedgerSection:
    """Case 6: no live ledger section => explicit warning."""

    def test_no_ledger_section_warns(self, tmp_path: Path):
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_NO_LEDGER_SECTION)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        assert "warning: no live ledger section found" in result.output
        assert "pending-row count: 0" in result.output


class TestLedgerCheckExactMatchB1:
    """B1: exact alphanumeric-boundary matching for stale re-entry."""

    def test_exact_match_positive_punctuation_delimited(self, tmp_path: Path):
        """F20 and D7-A are found when delimited by non-alnum in re-entry."""
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_EXACT_MATCH_POSITIVE)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        assert "warning: POST-RESTART RE-ENTRY names drained feature: F20" in result.output
        assert "warning: POST-RESTART RE-ENTRY names drained feature: D7-A" in result.output

    def test_exact_match_negative_f20_vs_f203_f200(self, tmp_path: Path):
        """F20 must NOT match F203 or F200 in re-entry text."""
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_EXACT_MATCH_NEGATIVE)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        # F20 is drained but re-entry only mentions F203/F200 — no stale warning.
        assert "warning: POST-RESTART RE-ENTRY names drained feature" not in result.output
        assert "pending-row count: 0" in result.output


class TestLedgerCheckVerifiedStatusS1:
    """S1: verified status treated as drained-pass for stale checks."""

    def test_verified_historical_no_unknown_warning(self, tmp_path: Path):
        """Real historical VERIFIED bullet: no unknown warning, stale fires."""
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_VERIFIED_HISTORICAL)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        assert "warning: unrecognized" not in result.output
        assert "pending-row count: 0" in result.output
        # Feature is drained (verified) and named in re-entry.
        assert "warning: POST-RESTART RE-ENTRY names drained feature: F203/F206 batch" in result.output


class TestLedgerCheckHeadingS2:
    """S2: canonical heading acceptance and last-wins with archive exclusion."""

    def test_archive_not_accepted_as_ledger(self, tmp_path: Path):
        """## Live ledger archive must NOT be treated as a canonical ledger heading."""
        content = """\
## Live ledger archive

| Feature | Load-bearing assertion | Owning commit | Activation path | Status | Notes |
|---------|------------------------|---------------|-----------------|--------|-------|
| X1 | assertion | abc | /x | pending | archived |

## POST-RESTART RE-ENTRY

Nothing.
"""
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(content)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        assert "warning: no live ledger section found" in result.output
        assert "pending-row count: 0" in result.output

    def test_multiple_canonical_uses_last(self, tmp_path: Path):
        """With archive + old canonical + new canonical, only new canonical counted."""
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_MULTIPLE_HEADINGS_WITH_ARCHIVE)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            result = runner.invoke(ledger, ["check"])
        assert result.exit_code == 0
        # Only the LAST canonical heading (## Live ledger additions ...) is used.
        # That section has 1 row: NEW1 pending.
        assert "pending-row count: 1" in result.output


class TestLedgerCheckMutationScopeRemoval:
    """Mutation: removing ledger-section scoping causes unrelated table to inflate count."""

    def test_mutation_scope_removal_detected(self, tmp_path: Path):
        """If _extract_ledger_section is bypassed (returns full text),
        unrelated table inflates pending count."""
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_UNRELATED_TABLE_OUTSIDE)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            # Normal invocation should pass with count 1.
            result = runner.invoke(ledger, ["check"])
            assert "pending-row count: 1" in result.output

            # Mutant: bypass scoping by making _extract_ledger_section return all text.
            with patch(
                "cli_agent_orchestrator.cli.commands.ledger._extract_ledger_section",
                return_value=handoff.read_text(),
            ):
                mutant_result = runner.invoke(ledger, ["check"])
                # Mutant sees unrelated table rows => count > 1, proving the test catches it.
                assert "pending-row count: 1" not in mutant_result.output


class TestLedgerCheckMutationUnknownAsPending:
    """Mutation: turning unknown status into pending causes test_unknown_status to fail."""

    def test_mutation_unknown_as_pending_detected(self, tmp_path: Path):
        """If unknown statuses count as pending, the pending count inflates."""
        handoff = tmp_path / "HANDOFF.md"
        handoff.write_text(_MALFORMED_AND_UNKNOWN)
        runner = CliRunner()
        with patch(
            "cli_agent_orchestrator.cli.commands.ledger.find_workspace_file",
            return_value=handoff,
        ):
            # Normal: only 1 pending.
            result = runner.invoke(ledger, ["check"])
            assert "pending-row count: 1" in result.output
