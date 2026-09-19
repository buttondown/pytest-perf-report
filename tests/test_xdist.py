"""Worker shards merge into one report, without double counting."""

import pytest

from helpers import read_json


def test_xdist_warnings_not_double_counted(pytester):
    pytest.importorskip("xdist")
    pytester.makepyfile(
        """
        import warnings

        def _warn(): warnings.warn("perf-report-canary", UserWarning)
        def test_a(): _warn()
        def test_b(): _warn()
        def test_c(): _warn()
        def test_d(): _warn()
        """
    )
    result = pytester.runpytest_subprocess("-n", "2", "--perf-report-json=report.json")
    result.assert_outcomes(passed=4)
    data = read_json(pytester.path / "report.json")
    canary = sum(
        n
        for (cat, msg), n in ((tuple(key), count) for key, count in data["warnings"])
        if "perf-report-canary" in msg
    )
    assert canary == 4


def test_xdist_workers_merge(pytester):
    pytest.importorskip("xdist")
    pytester.makepyfile(
        """
        import time

        def test_a(): time.sleep(0.01)
        def test_b(): time.sleep(0.01)
        def test_c(): time.sleep(0.01)
        def test_d(): time.sleep(0.01)
        """
    )
    result = pytester.runpytest_subprocess(
        "-n", "2", "--perf-report=report.html", "--perf-report-json=report.json"
    )
    result.assert_outcomes(passed=4)
    data = read_json(pytester.path / "report.json")
    assert data["stats"]["tests"] == 4
    assert data["suite"]["workers"] == 2
    assert data["stats"]["sleep_s"] >= 0.03
    # Worker startup (interpreter + imports + collection) must be attributed,
    # not reported as the controller's ~0.
    assert data["stats"]["startup_collect_s"] > 0.05
    # Timeline lanes: controller first, then both workers.
    assert [lane["shard_id"] for lane in data["lanes"]] == ["main", "gw0", "gw1"]
    assert all(lane["epoch_first_test"] > 0 for lane in data["lanes"][1:])
    assert (pytester.path / "report.html").exists()
