"""Tests for scripts/suite-ledger.py (AC6.1, AC6.3)."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add scripts/ to path so we can import suite-ledger
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import importlib
suite_ledger = importlib.import_module("suite-ledger")


SAMPLE_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="0" failures="0" skipped="0" tests="5" time="10.0">
    <testcase classname="test.fast" name="test_a" time="0.5"/>
    <testcase classname="test.fast" name="test_b" time="0.3"/>
    <testcase classname="test.slow" name="test_c" time="4.0"/>
    <testcase classname="test.slow" name="test_d" time="3.0"/>
    <testcase classname="test.medium" name="test_e" time="2.2"/>
  </testsuite>
</testsuites>"""


@pytest.fixture
def junit_file(tmp_path):
    p = tmp_path / "junit-results.xml"
    p.write_text(SAMPLE_JUNIT)
    return str(p)


class TestParseJunit:
    def test_parses_file_times(self, junit_file):
        file_times, tests = suite_ledger.parse_junit(junit_file)
        assert file_times["test.slow"] == pytest.approx(7.0)
        assert file_times["test.fast"] == pytest.approx(0.8)
        assert file_times["test.medium"] == pytest.approx(2.2)

    def test_parses_individual_tests(self, junit_file):
        _, tests = suite_ledger.parse_junit(junit_file)
        assert len(tests) == 5
        assert tests[0] == ("test.fast", "test_a", pytest.approx(0.5))

    def test_handles_testsuite_root(self, tmp_path):
        """JUnit with <testsuite> as root (no <testsuites> wrapper)."""
        xml = """<?xml version="1.0"?>
<testsuite name="pytest" tests="2" time="3.0">
  <testcase classname="mod.a" name="t1" time="1.5"/>
  <testcase classname="mod.a" name="t2" time="1.5"/>
</testsuite>"""
        p = tmp_path / "single.xml"
        p.write_text(xml)
        file_times, tests = suite_ledger.parse_junit(str(p))
        assert file_times["mod.a"] == pytest.approx(3.0)
        assert len(tests) == 2


class TestTailRatio:
    def test_basic(self):
        # 5 tests, slowest 5% = 1 test (max(1, int(5*0.05))=1)
        tests = [("a", "t1", 10.0), ("b", "t2", 1.0), ("c", "t3", 1.0),
                 ("d", "t4", 1.0), ("e", "t5", 1.0)]
        ratio = suite_ledger.compute_tail_ratio(tests)
        # slowest 1 test = 10.0 / 14.0
        assert ratio == pytest.approx(10.0 / 14.0)

    def test_empty(self):
        assert suite_ledger.compute_tail_ratio([]) == 0.0

    def test_all_zero(self):
        tests = [("a", "t", 0.0), ("b", "t", 0.0)]
        assert suite_ledger.compute_tail_ratio(tests) == 0.0


class TestGenerateLedger:
    def test_plain_text(self, junit_file):
        output = suite_ledger.generate_ledger([junit_file])
        assert "Suite Ledger" in output
        assert "5 tests" in output
        assert "10.0s" in output
        assert "test.slow" in output

    def test_github_summary(self, junit_file):
        output = suite_ledger.generate_ledger([junit_file], github_summary=True)
        assert "### Performance Ledger" in output
        assert "| # | File | Time |" in output
        assert "`test.slow`" in output

    def test_multiple_files(self, tmp_path):
        xml1 = """<testsuites><testsuite tests="1"><testcase classname="a" name="t" time="2.0"/></testsuite></testsuites>"""
        xml2 = """<testsuites><testsuite tests="1"><testcase classname="a" name="t2" time="3.0"/></testsuite></testsuites>"""
        f1 = tmp_path / "j1.xml"
        f2 = tmp_path / "j2.xml"
        f1.write_text(xml1)
        f2.write_text(xml2)
        output = suite_ledger.generate_ledger([str(f1), str(f2)])
        assert "2 tests" in output
        assert "5.0s" in output

    def test_no_tests(self, tmp_path):
        xml = """<testsuites><testsuite tests="0"></testsuite></testsuites>"""
        p = tmp_path / "empty.xml"
        p.write_text(xml)
        output = suite_ledger.generate_ledger([str(p)])
        assert "No test cases found" in output
