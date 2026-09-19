"""Fixture timing, self time, autouse taxes, and setup-phase query attribution."""

import pytest

from helpers import read_json


def test_fixture_timing_recorded(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def slow_fixture():
            import time
            time.sleep(0.03)
            return 1

        def test_uses_fixture(slow_fixture):
            assert slow_fixture == 1
        """
    )
    pytester.runpytest("--perf-report-json=report.json").assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    assert data["fixtures"]["slow_fixture"]["count"] == 1
    assert data["fixtures"]["slow_fixture"]["total_s"] >= 0.02


def test_fixture_self_time_subtracts_dynamic_children(pytester):
    pytester.makepyfile(
        """
        import time
        import pytest

        @pytest.fixture
        def child():
            time.sleep(0.05)
            return 1

        @pytest.fixture
        def parent(request):
            time.sleep(0.01)
            return request.getfixturevalue("child")

        def test_uses_parent(parent):
            assert parent == 1
        """
    )
    pytester.runpytest("--perf-report-json=report.json").assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    parent = data["fixtures"]["parent"]
    child = data["fixtures"]["child"]
    # parent's total includes the dynamic child setup; its self time must not.
    assert parent["total_s"] >= 0.055
    assert parent["self_s"] < 0.04
    assert child["self_s"] >= 0.04
    assert parent["scope"] == "function"


def test_autouse_fixtures_reported_as_per_test_tax(pytester):
    pytester.makepyfile(
        """
        import time
        import pytest

        @pytest.fixture(autouse=True)
        def per_test_tax():
            time.sleep(0.02)

        @pytest.fixture(scope="session", autouse=True)
        def session_setup():
            time.sleep(0.01)

        def test_one(): pass
        def test_two(): pass
        """
    )
    pytester.runpytest(
        "--perf-report=report.html", "--perf-report-json=report.json"
    ).assert_outcomes(passed=2)
    data = read_json(pytester.path / "report.json")
    tax = data["fixtures"]["per_test_tax"]
    assert tax["autouse"] is True
    assert tax["scope"] == "function"
    assert tax["count"] == 2
    session_row = data["fixtures"]["session_setup"]
    assert session_row["autouse"] is True
    assert session_row["scope"] == "session"
    assert session_row["count"] == 1
    html = (pytester.path / "report.html").read_text()
    assert "Per-test taxes (autouse fixtures)" in html
    assert "per_test_tax" in html
    # Session-scoped fixtures are setup, not a per-test tax.
    assert "session_setup" not in html.split("Per-test taxes")[1].split("<h2>")[1]


def test_fixture_query_attribution(pytester):
    pytest.importorskip("sqlalchemy")
    pytester.makepyfile(
        """
        import pytest
        import sqlalchemy

        @pytest.fixture
        def seeded_db():
            engine = sqlalchemy.create_engine("sqlite://")
            with engine.connect() as conn:
                conn.execute(sqlalchemy.text("CREATE TABLE t (id INTEGER)"))
                conn.execute(sqlalchemy.text("INSERT INTO t VALUES (1)"))
                conn.execute(sqlalchemy.text("SELECT id FROM t"))
            return engine

        def test_uses_db(seeded_db):
            pass
        """
    )
    pytester.runpytest("--perf-report-json=report.json").assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    row = data["fixtures"]["seeded_db"]
    assert row["db_queries"] >= 3
    assert row["db_writes"] >= 1  # the INSERT
    assert row["db_time_s"] >= 0


def test_first_test_flagged_for_session_setup(pytester):
    pytester.makepyfile(
        """
        import time
        import pytest

        @pytest.fixture(scope="session", autouse=True)
        def expensive_session_setup():
            time.sleep(0.08)

        def test_first(): pass
        def test_second(): pass
        """
    )
    pytester.runpytest(
        "--perf-report=report.html", "--perf-report-json=report.json"
    ).assert_outcomes(passed=2)
    data = read_json(pytester.path / "report.json")
    (lane,) = data["lanes"]
    assert lane["first_test_nodeid"].endswith("test_first")
    html = (pytester.path / "report.html").read_text()
    assert "includes session setup" in html
