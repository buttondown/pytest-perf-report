"""The pytest-perf-report console script."""

import sys

from helpers import read_json


def test_cli_runs_pytest_with_the_report_enabled(pytester):
    pytester.makepyfile("def test_ok(): pass")
    result = pytester.run(
        sys.executable, "-m", "pytest_perf_report.cli", "--perf-report-json=out.json"
    )
    assert result.ret == 0
    assert "cannot be rewritten" not in result.stdout.str()
    assert (pytester.path / "perf-report.html").exists()
    assert read_json(pytester.path / "out.json")["stats"]["tests"] == 1
