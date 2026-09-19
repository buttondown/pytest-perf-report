from collections import Counter

from pytest_perf_report.analyze import (
    _nodeid_short,
    build_todos,
    compute,
    percentile,
    split_nodeid,
)


def make_merged(**overrides):
    per_test = overrides.pop(
        "per_test",
        [
            {
                "nodeid": f"test_mod.py::test_{i}",
                "outcome": "passed",
                "wall_s": 0.1,
                "cpu_s": 0.05,
                "setup_s": 0.02,
                "call_s": 0.07,
                "teardown_s": 0.01,
                "db_queries": 5,
                "db_time_s": 0.01,
                "http_calls": 0,
                "http_time_s": 0.0,
                "sleep_s": 0.0,
                "gc_s": 0.0,
                "file_opens": 1,
                "failure": None,
            }
            for i in range(30)
        ],
    )
    merged = {
        "meta": {},
        "suite": {"wall_s": 5.0, "startup_collect_s": 0.5, "workers": 0},
        "per_test": per_test,
        "shapes": {},
        "db_vendors": {},
        "db_outside": {"queries": 0, "time_s": 0.0},
        "http_hosts": {},
        "raw_http_hosts": set(),
        "net_endpoints": {},
        "files": {"top": Counter(), "total_opens": 30, "distinct": 5},
        "sleep_s": 0.0,
        "gc_s": 0.0,
        "gc_collections": 0,
        "fixtures": {},
        "warnings": Counter(),
        "io": {"read_bytes": 0, "write_bytes": 0},
    }
    merged.update(overrides)
    return merged


def test_percentile():
    assert percentile([], 0.5) == 0.0
    assert percentile([1.0], 0.99) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5


def test_compute_totals():
    merged = make_merged()
    stats = compute(merged)
    assert stats["tests"] == 30
    assert stats["outcomes"] == {"passed": 30}
    assert abs(stats["agg_wall_s"] - 3.0) < 1e-6
    assert stats["db_queries"] == 150
    assert 0 < stats["db_share"] < 1


def test_phases_account_for_the_whole_clock():
    merged = make_merged(
        suite={
            "wall_s": 5.0,
            "startup_collect_s": 1.4,
            "startup_preconfigure_s": 0.4,
            "startup_collection_s": 1.0,
            "workers": 0,
        },
        lanes=[{"first_test_setup_s": 0.5}],
    )
    stats = compute(merged)
    phases = dict(stats["phases"])
    assert phases["startup, imports"] == 0.4
    assert phases["collection"] == 1.0
    assert phases["session fixtures"] == 0.5
    # 30 tests × 0.1s, less the session fixture setup billed to the first one.
    assert abs(phases["test bodies"] - 2.5) < 1e-6
    # suite wall starts at pytest_configure, so orchestration is the part of it
    # the other phases don't explain and startup sits outside it entirely.
    assert abs(phases["orchestration"] - 1.0) < 1e-6
    assert abs(stats["total_wall_s"] - 5.4) < 1e-6


def test_healthy_suite_gets_a_single_benign_todo():
    todos = build_todos(make_merged(), compute(make_merged()))
    assert len(todos) == 1
    assert todos[0]["severity"] == "low"
    assert "No obvious problems" in todos[0]["title"]


def test_failures_produce_high_severity_todo_first():
    merged = make_merged()
    merged["per_test"][0]["outcome"] = "failed"
    merged["sleep_s"] = 2.0
    stats = compute(merged)
    todos = build_todos(merged, stats)
    assert todos[0]["severity"] == "high"
    assert "failing test" in todos[0]["title"]


def test_external_network_flagged():
    merged = make_merged(
        net_endpoints={
            "api.example.com:443": {"connects": 3, "time_s": 0.4, "external": True}
        }
    )
    todos = build_todos(merged, compute(merged))
    assert any("real network" in t["title"] for t in todos)


def test_sleep_todo():
    merged = make_merged(sleep_s=2.0)
    merged["per_test"][3]["sleep_s"] = 2.0
    todos = build_todos(merged, compute(merged))
    assert any("time.sleep" in t["title"] for t in todos)


def test_missing_worker_shards_outrank_everything():
    merged = make_merged()
    merged["meta"]["missing_workers"] = 2
    todos = build_todos(merged, compute(merged))
    assert todos[0]["severity"] == "high"
    assert "worker shard" in todos[0]["title"]


def test_mocked_http_counts_as_cpu_not_wait():
    merged = make_merged(
        http_hosts={
            "api.stripe.com": {"calls": 100, "time_s": 5.0},
            "real.example.com": {"calls": 2, "time_s": 1.0},
        },
        raw_http_hosts={"real.example.com"},
    )
    merged["per_test"][0]["http_time_s"] = 6.0
    merged["per_test"][0]["http_calls"] = 102
    stats = compute(merged)
    decomposition = dict(stats["decomposition"])
    assert decomposition["HTTP (real)"] == 1.0  # mocked stripe time excluded
    assert stats["http_time_s"] == 6.0  # but still reported in total


def test_startup_todo_cites_decomposition_and_evidence():
    merged = make_merged(
        suite={
            "wall_s": 20.0,
            "startup_collect_s": 8.0,
            "startup_preconfigure_s": 2.0,
            "startup_collection_s": 6.0,
            "collect_modifyitems_s": 1.2,
            "workers": 4,
        },
        collect_modules={"tests/test_glob_at_import.py": 0.9},
        startup_imports={"myapp.settings": [0.6, 3.0]},
    )
    todos = build_todos(merged, compute(merged))
    todo = next(t for t in todos if "Startup + collection" in t["title"])
    body = todo["body"]
    assert "2.00s" in body and "6.00s" in body  # the split
    assert "test_glob_at_import.py" in body  # slow module named
    assert "pytest_collection_modifyitems" in body  # hook time named
    assert "myapp.settings" in body  # heavy import named
    assert "4×" in body  # per-worker multiplier
    assert "file paths" in body  # -k advice


def test_file_reopen_todo_requires_real_volume():
    # 100 opens/test trips the threshold either way; what changes is the body.
    files_heavy = {
        "top": Counter({"big-spec.json": 50}),
        "total_opens": 3000,
        "distinct": 5,
        "sizes": {"big-spec.json": 1024 * 1024},  # 50MB re-read
    }
    todos = build_todos(
        make_merged(files=files_heavy), compute(make_merged(files=files_heavy))
    )
    todo = next(t for t in todos if "file opens" in t["title"])
    assert "big-spec.json" in todo["body"]
    assert "re-read" in todo["body"]

    # Same open count, but the top file is reopened rarely and is small:
    # no caching advice, no example file held up as a finding.
    files_light = {
        "top": Counter({"small-spec.json": 5}),
        "total_opens": 3000,
        "distinct": 2900,
        "sizes": {"small-spec.json": 880 * 1024},
    }
    todos = build_todos(
        make_merged(files=files_light), compute(make_merged(files=files_light))
    )
    todo = next(t for t in todos if "file opens" in t["title"])
    assert "small-spec.json" not in todo["body"]
    assert "worth caching" in todo["body"]


def test_n_plus_one_todo():
    merged = make_merged(
        shapes={
            "SELECT ? FROM t WHERE id = ?": {
                "count": 900,
                "db_time_s": 0.5,
                "example": "SELECT 1 FROM t WHERE id = 1",
                "op": "SELECT",
                "target": "t",
                "tests": 30,
                "max_in_one_test": 30,
            }
        }
    )
    todos = build_todos(merged, compute(merged))
    assert any("N+1" in t["title"] for t in todos)


def test_split_nodeid():
    assert split_nodeid("m.py::test_x") == ("m.py::", "test_x", "")
    assert split_nodeid("a/m.py::C::test_x[case-1]") == (
        "a/m.py::C::",
        "test_x",
        "case-1",
    )
    # Parametrize ids may contain the separators the nodeid itself uses.
    assert split_nodeid("m.py::test_x[a::b[c]]") == ("m.py::", "test_x", "a::b[c]")
    assert split_nodeid("test_x") == ("", "test_x", "")


def test_nodeid_short_keeps_the_test_name():
    nodeid = "a/very/long/path/to/plain/views/webhook--test.py::test_auto_tag[flagged-when-human-relabels]"
    short = _nodeid_short(nodeid, 60)
    assert len(short) <= 60
    assert "test_auto_tag" in short
    long_param = "m.py::test_x[" + "p" * 200 + "]"
    short = _nodeid_short(long_param, 40)
    assert len(short) <= 40
    assert short.startswith("test_x[p")
    assert _nodeid_short("m.py::test_x", 90) == "m.py::test_x"


def test_gc_todo_needs_more_than_a_rounding_error():
    # 4ms of GC in a small suite is noise, not a finding.
    quiet = make_merged(gc_s=0.004)
    assert not any(
        "Garbage collection" in t["title"] for t in build_todos(quiet, compute(quiet))
    )
    loud = make_merged(gc_s=3.0)
    assert any(
        "Garbage collection" in t["title"] for t in build_todos(loud, compute(loud))
    )
