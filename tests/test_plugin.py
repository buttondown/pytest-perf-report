import builtins
import json
import time

import pytest


def read_json(path):
    with open(path) as f:
        return json.load(f)


def test_disabled_by_default(pytester):
    pytester.makepyfile("def test_ok(): pass")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert not (pytester.path / "perf-report.html").exists()


def test_basic_report_outcomes_and_probes(pytester):
    orig_sleep, orig_open = time.sleep, builtins.open
    pytester.makepyfile(
        """
        import time
        import pytest

        def test_sleepy(tmp_path):
            (tmp_path / "data.txt").write_text("hello")
            assert (tmp_path / "data.txt").read_text() == "hello"
            time.sleep(0.05)

        def test_fails():
            assert 1 == 2, "intentional"

        @pytest.mark.skip(reason="nope")
        def test_skipped():
            pass
        """
    )
    result = pytester.runpytest(
        "--perf-report=report.html", "--perf-report-json=report.json"
    )
    result.assert_outcomes(passed=1, failed=1, skipped=1)
    # Probes are fully uninstalled after the session.
    assert time.sleep is orig_sleep
    assert builtins.open is orig_open

    data = read_json(pytester.path / "report.json")
    stats = data["stats"]
    assert stats["tests"] == 3
    assert stats["outcomes"] == {"passed": 1, "failed": 1, "skipped": 1}
    assert stats["sleep_s"] >= 0.04
    assert stats["file_opens"] >= 1
    assert stats["peak_rss_bytes"] > 0  # getrusage-based, POSIX
    assert data["todos"][0]["severity"] == "high"
    assert "failing test" in data["todos"][0]["title"]

    html = (pytester.path / "report.html").read_text()
    assert "Test suite performance report" in html
    assert "test_fails" in html
    assert "intentional" in html  # captured failure text
    assert "What to do about it" in html
    assert "Run timeline" in html

    (lane,) = data["lanes"]
    assert lane["shard_id"] == "main"
    assert lane["epoch_first_test"] > 0
    assert lane["epoch_finish"] >= lane["epoch_last_test"] > 0

    result.stdout.fnmatch_lines(["*perf report*", "*report: file://*"])


def test_http_and_socket_probes(pytester):
    pytester.makepyfile(
        """
        import http.server
        import threading
        import urllib.request

        def test_local_http():
            server = http.server.HTTPServer(
                ("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                    assert resp.status == 200
            finally:
                server.shutdown()
        """
    )
    result = pytester.runpytest("--perf-report-json=report.json")
    result.assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    assert data["stats"]["http_calls"] >= 1
    assert "127.0.0.1" in data["http_hosts"]
    assert data["http_hosts"]["127.0.0.1"]["time_s"] > 0
    assert "127.0.0.1" in data["raw_http_hosts"]
    loopback = [r for r in data["net_endpoints"].values() if not r["external"]]
    assert loopback and sum(r["connects"] for r in loopback) >= 1
    # Nothing here left the machine.
    assert not any(r["external"] for r in data["net_endpoints"].values())


def test_sqlalchemy_probe_and_n_plus_one(pytester):
    pytest.importorskip("sqlalchemy")
    pytester.makepyfile(
        """
        import sqlalchemy

        def test_queries():
            engine = sqlalchemy.create_engine("sqlite://")
            with engine.connect() as conn:
                conn.execute(sqlalchemy.text("CREATE TABLE t (id INTEGER, name TEXT)"))
                for i in range(30):
                    conn.execute(
                        sqlalchemy.text("SELECT id FROM t WHERE id = :i"), {"i": i}
                    )
        """
    )
    result = pytester.runpytest("--perf-report-json=report.json")
    result.assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    assert data["stats"]["db_queries"] >= 31
    assert "sqlite" in data["db_vendors"]
    select_shapes = {s: r for s, r in data["shapes"].items() if r["op"] == "SELECT"}
    assert any(r["max_in_one_test"] >= 30 for r in select_shapes.values())
    assert any("N+1" in t["title"] for t in data["todos"])


def test_django_probe_and_n_plus_one(pytester):
    pytest.importorskip("django")
    pytester.makepyfile(
        """
        import django
        from django.conf import settings

        settings.configure(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            }
        )
        django.setup()

        from django.db import connection

        def test_queries():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TABLE t (id INTEGER, name TEXT)")
                for i in range(30):
                    cursor.execute("SELECT id FROM t WHERE id = %s", [i])
        """
    )
    # A subprocess: Django settings can only be configured once per process.
    result = pytester.runpytest_subprocess("--perf-report-json=report.json")
    result.assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    assert data["stats"]["db_queries"] >= 31
    assert "sqlite" in data["db_vendors"]
    select_shapes = {s: r for s, r in data["shapes"].items() if r["op"] == "SELECT"}
    assert any(r["max_in_one_test"] >= 30 for r in select_shapes.values())
    assert any("N+1" in t["title"] for t in data["todos"])


def test_sql_origins_opt_in(pytester):
    pytest.importorskip("sqlalchemy")
    pytester.makepyfile(
        """
        import sqlalchemy

        def test_query():
            engine = sqlalchemy.create_engine("sqlite://")
            with engine.connect() as conn:
                conn.execute(sqlalchemy.text("SELECT 1"))
        """
    )
    result = pytester.runpytest(
        "--perf-report-json=report.json", "--perf-report-sql-origins"
    )
    result.assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    origins = [o for row in data["shapes"].values() for o in row.get("origins", {})]
    assert any("test_sql_origins_opt_in" in o for o in origins)
    # Without the flag, no origins are captured (it's the expensive path).
    data_plain_run = pytester.runpytest("--perf-report-json=plain.json")
    data_plain_run.assert_outcomes(passed=1)
    plain = read_json(pytester.path / "plain.json")
    assert not any("origins" in row for row in plain["shapes"].values())


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
    delta = data["baseline_delta"]
    assert delta["common_tests"] == 2
    assert {t["key"]: t["delta"] for t in delta["totals"]}["tests"] == 0
    assert "Vs baseline" in (pytester.path / "report.html").read_text()
    result.stdout.fnmatch_lines(["*vs baseline:*"])


def test_missing_baseline_rejected(pytester):
    pytester.makepyfile("def test_ok(): pass")
    result = pytester.runpytest(
        "--perf-report-json=report.json", "--perf-report-baseline=nope.json"
    )
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*does not exist*"])


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


def test_pathlib_opens_are_counted(pytester):
    pytester.makepyfile(
        """
        def test_pathlib(tmp_path):
            p = tmp_path / "data.txt"
            p.write_text("hello")
            assert p.read_text() == "hello"
        """
    )
    pytester.runpytest("--perf-report-json=report.json").assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    assert any("data.txt" in path for path, _ in data["files"]["top"])
    # Top files are stat'ed at session finish so the report can estimate
    # re-read volume.
    assert any("data.txt" in path for path in data["files"]["sizes"])


def test_failed_dns_is_not_flagged_as_network_egress(pytester):
    pytester.makepyfile(
        """
        import socket
        import pytest

        def test_unresolvable():
            s = socket.socket()
            try:
                with pytest.raises(socket.gaierror):
                    s.connect(("nonexistent.invalid", 80))
            finally:
                s.close()
        """
    )
    pytester.runpytest("--perf-report-json=report.json").assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    assert not any(r["external"] for r in data["net_endpoints"].values())


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


def test_shape_peak_test_and_first_origin_always_recorded(pytester):
    pytest.importorskip("sqlalchemy")
    pytester.makepyfile(
        """
        import sqlalchemy

        def test_n_plus_one_style():
            engine = sqlalchemy.create_engine("sqlite://")
            with engine.connect() as conn:
                conn.execute(sqlalchemy.text("CREATE TABLE t (id INTEGER)"))
                for i in range(30):
                    conn.execute(
                        sqlalchemy.text("SELECT id FROM t WHERE id = :i"), {"i": i}
                    )
        """
    )
    # No --perf-report-sql-origins: first-occurrence origins still recorded.
    pytester.runpytest(
        "--perf-report=report.html", "--perf-report-json=report.json"
    ).assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    select_rows = [r for r in data["shapes"].values() if r["op"] == "SELECT"]
    repeated = next(r for r in select_rows if r["max_in_one_test"] >= 30)
    assert "test_n_plus_one_style" in repeated["max_test"]
    assert "test_shape_peak_test_and_first_origin" in repeated["origin_first"]
    html = (pytester.path / "report.html").read_text()
    assert "first issued from" in html
    assert "peak 30× in" in html
    # The N+1 TODO names the worst test, not just the shape.
    n_plus_one_todo = next(t for t in data["todos"] if "N+1" in t["title"])
    assert "test_n_plus_one_style" in n_plus_one_todo["body"]


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


def test_cpu_profile_opt_in(pytester):
    pytester.makepyfile(
        """
        def burn():
            return sum(i * i for i in range(200_000))

        def test_burns_cpu():
            assert burn() > 0
        """
    )
    pytester.runpytest(
        "--perf-report=report.html",
        "--perf-report-json=report.json",
        "--perf-report-cpu-profile",
    ).assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    assert data["cpu_profile"], "profile data should be captured"
    assert any("burn" in key for key in data["cpu_profile"])
    html = (pytester.path / "report.html").read_text()
    assert "Where the CPU went (cProfile)" in html
    # Off by default: no profile data, no section.
    pytester.runpytest("--perf-report-json=plain.json").assert_outcomes(passed=1)
    plain = read_json(pytester.path / "plain.json")
    assert not plain.get("cpu_profile")


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
    import builtins

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
    # Without the flags, the timer never installs (the zero-overhead promise);
    # nothing to observe directly here beyond the wrapper being gone, which
    # the first assert already covers for the flagged run.


def test_non_path_open_arguments_are_sanitized(pytester):
    pytester.makepyfile(
        """
        def test_opens_content_as_path():
            blob = b"<?xml version='1.0'?>" + b"x" * 400
            try:
                open(blob)
            except (OSError, ValueError):
                pass
        """
    )
    pytester.runpytest("--perf-report-json=report.json").assert_outcomes(passed=1)
    data = read_json(pytester.path / "report.json")
    keys = [k for k, _ in data["files"]["top"]]
    assert "<non-path open() argument>" in keys
    assert not any("xml" in k for k in keys)


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
