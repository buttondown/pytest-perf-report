"""The rendered HTML: structure through pytester, content through a snapshot."""

import datetime
import os
import pathlib
import types
from collections import Counter

from pytest_perf_report import analyze, report
from pytest_perf_report.merge import SCHEMA_VERSION


def test_report_tabs_and_single_tests_table(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def fx():
            return 1

        def test_a(fx): pass
        def test_b(): pass
        """
    )
    pytester.runpytest("--perf-report=report.html").assert_outcomes(passed=2)
    html = (pytester.path / "report.html").read_text()
    # Tab strip with one panel per populated section; TODOs stay outside.
    assert '<div class="tabs" role="tablist">' in html
    assert '<button data-tab="tests"' in html
    assert '<button data-tab="fixtures"' in html
    assert html.index("What to do about it") > html.index('class="tab-panel"')
    # Slowest/fastest merged into one all-tests table.
    assert "Slowest tests" not in html and "Fastest tests" not in html
    assert '<h2 class="row">Tests' in html
    # Headline cards carry tooltips in a single grid.
    assert html.count('<div class="cards">') == 1
    assert 'title="End-to-end clock for the whole run' in html


def test_report_header_leads_with_the_clock(pytester):
    pytester.makepyfile(
        """
        def test_a(): pass
        def test_b(): pass
        """
    )
    pytester.runpytest("--perf-report=report.html").assert_outcomes(passed=2)
    html = (pytester.path / "report.html").read_text()
    # One number at the top, then the phases of the run that add up to it.
    assert '<h1 class="hero"' in html
    assert '<div class="phasebar">' in html
    assert "startup, imports" in html and "test bodies" in html
    # Four headline cards, each a number and a label and nothing else.
    assert html.count('<div class="card"') == 4
    assert '<div class="strip">' in html
    # A clean run says nothing where the callout used to be.
    assert "All tests passed" not in html
    # The run timeline and time decomposition moved behind an Overview tab,
    # and the busiest tabs carry their count.
    assert '<button data-tab="overview"' in html
    assert html.index("Where the time went") > html.index('data-tab="overview"')
    assert '<button data-tab="tests" aria-selected="false">Tests' in html
    assert '<span class="badge">2</span>' in html


def test_parametrized_cases_group_into_one_family_row(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.parametrize("case", ["one", "two", "three"])
        def test_param(case): pass

        def test_plain(): pass
        """
    )
    pytester.runpytest("--perf-report=report.html").assert_outcomes(passed=4)
    html = (pytester.path / "report.html").read_text()
    # One family row totalling the three cases, and the three cases under it.
    assert html.count('<tr class="fam" data-fam="0">') == 1
    assert html.count('<tr class="case" data-fam="0">') == 3
    assert '<span class="pill neutral">3 cases</span>' in html
    # Each case keeps its own parametrize id, and the family row has none.
    for case in ("one", "two", "three"):
        assert f'<span class="param" title="{case}">{case}</span>' in html
    # The unparametrized test stays a plain row, and the toggle sits in the
    # heading so the whole table can be flattened again.
    assert html.count("test_plain") >= 1
    assert '<h2 class="row">Tests<button class="rowtoggle" id="group-toggle"' in html


# ---- snapshot ---------------------------------------------------------------
#
# One rich, fixed dataset that exercises every section of the report (baseline,
# CPU profile, HTTP, files, warnings, xdist lanes). Regenerate on purpose with
# UPDATE_SNAPSHOTS=1 and review the diff of tests/snapshots/report.html.

SNAPSHOT = pathlib.Path(__file__).parent / "snapshots" / "report.html"

MB = 1024 * 1024


def _test(nodeid, wall, **overrides):
    row = {
        "nodeid": nodeid,
        "outcome": "passed",
        "wall_s": wall,
        "cpu_s": wall * 0.6,
        "setup_s": wall * 0.2,
        "call_s": wall * 0.7,
        "teardown_s": wall * 0.1,
        "db_queries": 4,
        "db_time_s": wall * 0.1,
        "http_calls": 0,
        "http_time_s": 0.0,
        "sleep_s": 0.0,
        "gc_s": 0.0,
        "file_opens": 2,
        "reruns": 0,
        "rss_growth_bytes": 0,
        "failure": None,
    }
    row.update(overrides)
    return row


def _lane(shard_id, **overrides):
    lane = {
        "shard_id": shard_id,
        "is_worker": True,
        "tests": 3,
        "wall_s": 3.5,
        "preconfigure_cpu_s": 1.0,
        "epoch_configure": 100.2,
        "epoch_collect_done": 100.7,
        "epoch_first_test": 100.8,
        "epoch_last_test": 103.5,
        "epoch_finish": 103.7,
        "first_test_setup_s": 2.0,
        "first_test_nodeid": "tests/test_b.py::TestX::test_slow",
        "peak_rss_bytes": 512 * MB,
    }
    lane.update(overrides)
    return lane


SELECT_SHAPE = 'SELECT "u"."id" FROM "u" WHERE "u"."id" = ?'
INSERT_SHAPE = 'INSERT INTO "u" (?) VALUES (?)'


def rich_merged():
    return {
        "meta": {
            "project": "demo",
            "started": "2026-01-01 09:00",
            "python": "3.13.0",
            "platform": "Linux",
            "pytest": "9.0.0",
            "plugin": "0.4.0",
            "args": "pytest --perf-report -n 2",
            "invocation_dir": "/demo",
        },
        "suite": {
            "wall_s": 4.0,
            "startup_collect_s": 1.5,
            "startup_preconfigure_s": 1.0,
            "startup_collection_s": 0.5,
            "collect_modifyitems_s": 0.1,
            "workers": 2,
        },
        "lanes": [
            _lane(
                "main",
                is_worker=False,
                tests=0,
                wall_s=4.0,
                epoch_configure=100.0,
                epoch_collect_done=100.5,
                epoch_first_test=0.0,
                epoch_last_test=0.0,
                epoch_finish=104.0,
                first_test_setup_s=0.0,
                first_test_nodeid="",
                peak_rss_bytes=300 * MB,
            ),
            _lane("gw0"),
            _lane("gw1", wall_s=3.4, epoch_last_test=103.3, epoch_finish=103.6),
        ],
        "per_test": [
            _test("tests/test_a.py::test_p[one]", 0.30),
            _test(
                "tests/test_a.py::test_p[two]",
                0.20,
                outcome="failed",
                failure="AssertionError: boom",
            ),
            _test("tests/test_a.py::test_p[three]", 0.10),
            _test(
                "tests/test_b.py::TestX::test_slow",
                2.50,
                setup_s=2.0,
                sleep_s=0.4,
                http_calls=2,
                http_time_s=0.2,
                rss_growth_bytes=150 * MB,
            ),
            _test("tests/test_b.py::test_flaky", 0.05, reruns=2),
            _test("tests/test_c.py::test_skip", 0.001, outcome="skipped"),
        ],
        "shapes": {
            SELECT_SHAPE: {
                "count": 120,
                "db_time_s": 0.30,
                "example": 'SELECT "u"."id" FROM "u" WHERE "u"."id" = 1',
                "op": "SELECT",
                "target": "u",
                "origin_first": "app/models.py:42",
                "tests": 3,
                "max_in_one_test": 40,
                "max_test": "tests/test_a.py::test_p[one]",
                "origins": {"app/models.py:42": 100, "app/views.py:7": 20},
            },
            INSERT_SHAPE: {
                "count": 30,
                "db_time_s": 0.05,
                "example": "INSERT INTO \"u\" (name) VALUES ('x')",
                "op": "INSERT",
                "target": "u",
                "origin_first": "tests/conftest.py:9",
                "tests": 6,
                "max_in_one_test": 5,
                "max_test": "tests/test_b.py::test_flaky",
            },
        },
        "db_vendors": {"postgresql": 150},
        "db_outside": {"queries": 12, "time_s": 0.05},
        "http_hosts": {
            "api.example.com": {"calls": 2, "time_s": 0.2},
            "mock.local": {"calls": 5, "time_s": 0.01},
        },
        "raw_http_hosts": {"api.example.com"},
        "net_endpoints": {
            "93.184.216.34:443": {"connects": 2, "time_s": 0.05, "external": True},
            "127.0.0.1:5432": {"connects": 3, "time_s": 0.002, "external": False},
        },
        "files": {
            "top": Counter({"fixtures/big.json": 12, "settings.toml": 3}),
            "total_opens": 400,
            "distinct": 40,
            "sizes": {"fixtures/big.json": 2 * MB},
        },
        "sleep_s": 0.4,
        "gc_s": 0.2,
        "gc_collections": 30,
        "fixtures": {
            "db": {
                "total_s": 2.0,
                "self_s": 1.8,
                "count": 6,
                "max_s": 1.9,
                "db_queries": 20,
                "db_time_s": 0.1,
                "db_writes": 8,
                "scope": "session",
                "autouse": False,
            },
            "clear_cache": {
                "total_s": 0.3,
                "self_s": 0.3,
                "count": 6,
                "max_s": 0.08,
                "db_queries": 0,
                "db_time_s": 0.0,
                "db_writes": 0,
                "scope": "function",
                "autouse": True,
            },
        },
        "warnings": Counter(
            {
                ("DeprecationWarning", "old thing"): 40,
                ("ResourceWarning", "unclosed file"): 3,
            }
        ),
        "cpu_profile": {
            "app/models.py:10:save": [0.9, 300],
            "<built-in method builtins.len>": [0.1, 9000],
        },
        "collect_modules": {"tests/test_a.py": 0.30, "tests/test_b.py": 0.05},
        "startup_imports": {"django": [0.4, 0.9], "app.settings": [0.2, 0.6]},
        "io": {"read_bytes": 10 * MB, "write_bytes": 2 * MB},
    }


def rich_baseline(stats):
    # Shape deltas are distinct so their order does not depend on set order.
    return {
        "schema": SCHEMA_VERSION,
        "meta": {"started": "2025-12-31 09:00"},
        "stats": {**stats, "tests": 5, "db_queries": 120, "agg_wall_s": 2.0},
        "shapes": {SELECT_SHAPE: {"count": 80}, "DELETE FROM old": {"count": 5}},
        "per_test": [
            {"nodeid": "tests/test_b.py::TestX::test_slow", "wall_s": 1.0},
            {"nodeid": "tests/test_a.py::test_p[one]", "wall_s": 0.3},
        ],
    }


def test_render_matches_snapshot(monkeypatch):
    merged = rich_merged()
    stats = analyze.compute(merged)
    delta = analyze.build_baseline_delta(rich_baseline(stats), merged, stats)
    todos = analyze.build_todos(merged, stats, delta)
    frozen = types.SimpleNamespace(
        datetime=types.SimpleNamespace(now=lambda: datetime.datetime(2026, 1, 1, 9, 5))
    )
    monkeypatch.setattr(report, "datetime", frozen)

    html = report.render(merged, stats, todos, delta)

    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(html)
    assert html == SNAPSHOT.read_text(), (
        "Report HTML changed. If intended: UPDATE_SNAPSHOTS=1 pytest tests/test_report.py"
    )
