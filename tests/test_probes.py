"""Probes: what each instrumentation seam records, end to end through pytester."""

import builtins
import time

import pytest

from helpers import read_json


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
