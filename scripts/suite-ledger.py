#!/usr/bin/env python3
"""suite-ledger: ranked wall-time analysis of JUnit XML test results.

AC6.1 — Reads one or more junit-results.xml files and emits:
  1. Slowest test FILES by summed wall-time
  2. Slowest individual TESTS
  3. Tail ratio: fraction of total time consumed by the slowest 5% of tests

AC6.3 — Runnable against a local junit file:
  python3 scripts/suite-ledger.py path/to/junit-results.xml [...]

Pure stdlib XML parse — NO NEW DEPENDENCY.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def parse_junit(path: str) -> tuple[dict[str, float], list[tuple[str, str, float]]]:
    """Parse a JUnit XML file, returning per-file times and per-test entries."""
    tree = ET.parse(path)
    root = tree.getroot()
    file_times: dict[str, float] = defaultdict(float)
    tests: list[tuple[str, str, float]] = []
    if root.tag == "testsuites":
        suites = root.findall("testsuite")
    elif root.tag == "testsuite":
        suites = [root]
    else:
        suites = []
    for suite in suites:
        for tc in suite.iter("testcase"):
            classname = tc.get("classname", "unknown")
            name = tc.get("name", "unknown")
            time_s = float(tc.get("time", "0"))
            file_times[classname] += time_s
            tests.append((classname, name, time_s))
    return dict(file_times), tests


def compute_tail_ratio(tests, percentile=0.05):
    """Fraction of total time consumed by the slowest percentile of tests."""
    if not tests:
        return 0.0
    times = sorted((t[2] for t in tests), reverse=True)
    total = sum(times)
    if total == 0:
        return 0.0
    n_tail = max(1, int(len(times) * percentile))
    tail_sum = sum(times[:n_tail])
    return tail_sum / total


def format_table(rows, header, max_rows=25):
    """Format a simple two-column table."""
    lines = []
    col1_w = max(len(header[0]), max((len(r[0]) for r in rows[:max_rows]), default=0))
    col2_w = max(len(header[1]), max((len(r[1]) for r in rows[:max_rows]), default=0))
    lines.append(f"  {'\u2500' * (col1_w + 2)}\u252c{'\u2500' * (col2_w + 2)}")
    lines.append(f"  {header[0]:<{col1_w}}  \u2502 {header[1]:>{col2_w}}")
    lines.append(f"  {'\u2500' * (col1_w + 2)}\u253c{'\u2500' * (col2_w + 2)}")
    for name, val in rows[:max_rows]:
        lines.append(f"  {name:<{col1_w}}  \u2502 {val:>{col2_w}}")
    if len(rows) > max_rows:
        lines.append(f"  ... ({len(rows) - max_rows} more)")
    lines.append(f"  {'\u2500' * (col1_w + 2)}\u2534{'\u2500' * (col2_w + 2)}")
    return "\n".join(lines)


def generate_ledger(paths, *, top_files=25, top_tests=25, github_summary=False):
    """Generate the full ledger report."""
    all_file_times = defaultdict(float)
    all_tests = []
    for p in paths:
        file_times, tests = parse_junit(p)
        for k, v in file_times.items():
            all_file_times[k] += v
        all_tests.extend(tests)
    if not all_tests:
        return "No test cases found in the provided JUnit XML file(s)."
    total_time = sum(t[2] for t in all_tests)
    tail_ratio = compute_tail_ratio(all_tests)
    n_tail = max(1, int(len(all_tests) * 0.05))
    sorted_files = sorted(all_file_times.items(), key=lambda x: x[1], reverse=True)
    sorted_tests = sorted(all_tests, key=lambda x: x[2], reverse=True)
    if github_summary:
        return _format_github_summary(sorted_files[:top_files], sorted_tests[:top_tests], total_time, tail_ratio, n_tail, len(all_tests))
    lines = []
    lines.append(f"Suite Ledger \u2014 {len(all_tests)} tests, {total_time:.1f}s total wall-time")
    lines.append(f"Tail ratio (slowest 5% = {n_tail} tests): {tail_ratio:.1%} of total time")
    lines.append("")
    lines.append(f"\u2500\u2500 Slowest test files (top {min(top_files, len(sorted_files))}) \u2500\u2500")
    file_rows = [(name, f"{secs:.2f}s") for name, secs in sorted_files[:top_files]]
    lines.append(format_table(file_rows, ("File (classname)", "Time")))
    lines.append("")
    lines.append(f"\u2500\u2500 Slowest individual tests (top {min(top_tests, len(sorted_tests))}) \u2500\u2500")
    test_rows = [(f"{cn}::{tn}", f"{s:.2f}s") for cn, tn, s in sorted_tests[:top_tests]]
    lines.append(format_table(test_rows, ("Test", "Time")))
    return "\n".join(lines)


def _format_github_summary(files, tests, total_time, tail_ratio, n_tail, n_tests):
    """Markdown for GitHub step summary (AC6.2)."""
    lines = []
    lines.append("### Performance Ledger")
    lines.append("")
    lines.append(f"**{n_tests} tests** | **{total_time:.1f}s** total | tail ratio (slowest 5% = {n_tail} tests): **{tail_ratio:.1%}**")
    lines.append("")
    lines.append("<details><summary>Top-10 slowest test files</summary>")
    lines.append("")
    lines.append("| # | File | Time |")
    lines.append("|---|------|------|")
    for i, (name, secs) in enumerate(files[:10], 1):
        lines.append(f"| {i} | `{name}` | {secs:.2f}s |")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    lines.append("<details><summary>Top-10 slowest individual tests</summary>")
    lines.append("")
    lines.append("| # | Test | Time |")
    lines.append("|---|------|------|")
    for i, (cn, tn, secs) in enumerate(tests[:10], 1):
        lines.append(f"| {i} | `{cn}::{tn}` | {secs:.2f}s |")
    lines.append("")
    lines.append("</details>")
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Ranked wall-time analysis of JUnit XML test results.",
        epilog="Example: python3 scripts/suite-ledger.py junit-results.xml",
    )
    parser.add_argument("files", nargs="+", help="One or more junit-results.xml paths")
    parser.add_argument("--top-files", type=int, default=25, help="Number of slowest files (default: 25)")
    parser.add_argument("--top-tests", type=int, default=25, help="Number of slowest tests (default: 25)")
    parser.add_argument("--github-summary", action="store_true", help="Emit markdown for GITHUB_STEP_SUMMARY (AC6.2)")
    args = parser.parse_args()
    for f in args.files:
        if not Path(f).exists():
            print(f"Error: file not found: {f}", file=sys.stderr)
            return 1
    output = generate_ledger(args.files, top_files=args.top_files, top_tests=args.top_tests, github_summary=args.github_summary)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
