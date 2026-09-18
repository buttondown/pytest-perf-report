"""Plugin lifecycle: options, output paths, outcomes, baselines, nested sessions."""

import builtins
import json

import pytest

from helpers import read_json
from pytest_perf_report.merge import SCHEMA_VERSION


def test_disabled_by_default(pytester):
    pytester.makepyfile("def test_ok(): pass")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert not (pytester.path / "perf-report.html").exists()


def test_flaky_reruns_counted(pytester):
    pytest.importorskip("pytest_rerunfailures")
    pytester.makepyfile(
        """
        import pathlib

        def test_flaky():
            marker = pathlib.Path("flake-marker")
            if not marker.exists():
                marker.write_text("x")
                raise AssertionError("first attempt always fails")
        """
    )
    result = pytester.runpytest("--reruns", "2", "--perf-report-json=report.json")
    outcomes = result.parseoutcomes()
    assert outcomes.get("passed") == 1 and outcomes.get("rerun") == 1
    data = read_json(pytester.path / "report.json")
    rec = data["per_test"][0]
    assert rec["reruns"] == 1
    assert rec["outcome"] == "passed"
    assert data["todos"][0]["severity"] == "high"
    assert "flaky" in data["todos"][0]["title"]


def test_baseline_comparison(pytester):
    pytester.makepyfile(
        """
        def test_a(): pass
        def test_b(): pass
        """
    )
    pytester.runpytest("--perf-report-json=base.json").assert_outcomes(passed=2)
    result = pytester.runpytest(
        "--perf-report=report.html",
        "--perf-report-json=report.json",
        "--perf-report-baseline=base.json",
    )
    result.assert_outcomes(passed=2)
    data = read_json(pytester.path / "report.json")
    assert data["schema"] == SCHEMA_VERSION
    delta = data["baseline_delta"]
    assert delta["common_tests"] == 2
    assert delta["baseline_schema"] == SCHEMA_VERSION
    assert {t["key"]: t["delta"] for t in delta["totals"]}["tests"] == 0
    html = (pytester.path / "report.html").read_text()
    assert "Vs baseline" in html
    assert "schema mismatch" not in html
    result.stdout.fnmatch_lines(["*vs baseline:*"])
    assert "WARNING: baseline JSON" not in result.stdout.str()


def test_baseline_schema_mismatch_is_flagged(pytester):
    pytester.makepyfile("def test_a(): pass")
    pytester.runpytest("--perf-report-json=base.json").assert_outcomes(passed=1)
    base = read_json(pytester.path / "base.json")
    base["schema"] = 1
    (pytester.path / "base.json").write_text(json.dumps(base))
    result = pytester.runpytest(
        "--perf-report=report.html", "--perf-report-baseline=base.json"
    )
    result.assert_outcomes(passed=1)
    assert "schema mismatch" in (pytester.path / "report.html").read_text()
    result.stdout.fnmatch_lines(["*WARNING: baseline JSON is schema 1*"])


def test_missing_baseline_rejected(pytester):
    pytester.makepyfile("def test_ok(): pass")
    result = pytester.runpytest(
        "--perf-report-json=report.json", "--perf-report-baseline=nope.json"
    )
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*does not exist*"])


def test_swallowed_positional_arg_is_rejected_not_overwritten(pytester):
    test_file = pytester.makepyfile("def test_ok(): pass")
    result = pytester.runpytest("--perf-report", str(test_file))
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*refusing to overwrite*"])
    assert "def test_ok" in test_file.read_text()  # file untouched


def test_directory_as_report_path_is_rejected(pytester):
    pytester.makepyfile("def test_ok(): pass")
    result = pytester.runpytest("--perf-report", str(pytester.path))
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*is a directory*"])


def test_teardown_failure_does_not_mask_call_failure(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def broken_teardown():
            yield
            raise RuntimeError("teardown boom")

        def test_assertion(broken_teardown):
            assert 1 == 2, "the real failure"
        """
    )
    pytester.runpytest("--perf-report-json=report.json")
    data = read_json(pytester.path / "report.json")
    rec = data["per_test"][0]
    assert rec["outcome"] == "failed"  # not downgraded to "error"
    assert "the real failure" in rec["failure"]  # not the teardown traceback


def test_nested_in_process_session_does_not_corrupt_outer(pytester):
    inner = pytester.makepyfile(
        inner_suite="""
        def test_inner_a(): pass
        def test_inner_b(): pass
        """
    )
    outer = pytester.makepyfile(
        f"""
        import pytest

        def test_outer():
            ret = pytest.main(["-p", "no:cacheprovider", "-q", {str(inner)!r}])
            assert ret == 0
        """
    )
    result = pytester.runpytest(str(outer), "--perf-report-json=report.json")
    result.assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    assert data["stats"]["tests"] == 1  # inner tests not double-recorded
    assert data["per_test"][0]["nodeid"].endswith("test_outer")


def test_collection_costs_recorded(pytester):
    pytester.makeconftest(
        """
        import time

        def pytest_collection_modifyitems(config, items):
            time.sleep(0.06)
        """
    )
    pytester.makepyfile(
        test_slow_import="""
        import time

        time.sleep(0.1)  # module-scope work, runs at collection

        def test_ok(): pass
        """,
        test_fast_import="def test_ok(): pass",
    )
    pytester.runpytest(
        "--perf-report=report.html", "--perf-report-json=report.json"
    ).assert_outcomes(passed=2)
    data = read_json(pytester.path / "report.json")
    modules = data["collect_modules"]
    slow = next(v for k, v in modules.items() if "test_slow_import" in k)
    assert slow >= 0.08
    fast = next(v for k, v in modules.items() if "test_fast_import" in k)
    assert fast < slow
    assert data["suite"]["collect_modifyitems_s"] >= 0.05
    # The startup split travels: preconfigure + collection wall.
    assert data["suite"]["startup_collection_s"] >= 0.1
    assert "startup_preconfigure_s" in data["suite"]
    html = (pytester.path / "report.html").read_text()
    assert "Slowest test modules to collect" in html
    assert "test_slow_import" in html


def test_startup_imports_captured(pytester):

    orig_import = builtins.__import__
    # A nested chain: the outer module's cost is almost entirely its child,
    # so self/cumulative attribution (and outer-frame bookkeeping) is exercised.
    pytester.makepyfile(
        slow_startup_inner="""
        import time
        time.sleep(0.08)
        """,
        slow_startup_outer="import slow_startup_inner",
    )
    pytester.makeconftest("import slow_startup_outer")
    pytester.makepyfile(test_x="def test_ok(): pass")
    pytester.runpytest("--perf-report-json=report.json").assert_outcomes(passed=1)
    # The __import__ wrapper must come off at configure time.
    assert builtins.__import__ is orig_import
    data = read_json(pytester.path / "report.json")
    inner = data["startup_imports"]["slow_startup_inner"]
    assert inner[0] >= 0.05  # self
    assert inner[1] >= inner[0]  # cumulative includes self
    outer = data["startup_imports"]["slow_startup_outer"]
    assert outer[1] >= inner[1]  # outer's cumulative includes the child
    assert outer[0] < 0.02  # ...but its self time is its own (empty) body
