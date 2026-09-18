"""Derived statistics and the opinionated TODO engine.

``compute()`` turns the merged shard data into the numbers the report shows;
``build_todos()`` turns those numbers into a short, prioritized list of
concrete next steps. Every TODO names its evidence so it reads as a finding,
not a platitude.
"""

from __future__ import annotations

import heapq
import math
from typing import Any

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

# A SELECT shape repeating at least this often inside a single test is called
# an N+1 suspect; report.py renders its table from the same constant.
N_PLUS_ONE_THRESHOLD = 25
# GC always costs something. It only earns a TODO when it is both a real slice
# of CPU and enough wall time that reclaiming it is worth the work.
GC_MIN_S = 1.0
GC_CPU_SHARE = 0.02


def percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = (len(sorted_values) - 1) * q
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (idx - lo)


def fmt_seconds(s: float) -> str:
    if s >= 120:
        return f"{int(s // 60)}m {int(s % 60):02d}s"
    if s >= 10:
        return f"{s:.1f}s"
    if s >= 1:
        return f"{s:.2f}s"
    return f"{s * 1000:.0f}ms"


def fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:,.0f}{unit}" if unit == "B" else f"{value:,.1f}{unit}"
        value /= 1024
    return f"{value:,.1f}GB"


def compute(merged: dict[str, Any]) -> dict[str, Any]:
    per_test = merged["per_test"]
    outcomes: dict[str, int] = {}
    agg_wall = cpu = db_time = http_time = setup_s = call_s = teardown_s = 0.0
    db_queries = http_calls = 0
    walls: list[float] = []
    for t in per_test:
        outcomes[t["outcome"]] = outcomes.get(t["outcome"], 0) + 1
        agg_wall += t["wall_s"]
        cpu += t["cpu_s"]
        db_time += t["db_time_s"]
        http_time += t["http_time_s"]
        setup_s += t["setup_s"]
        call_s += t["call_s"]
        teardown_s += t["teardown_s"]
        db_queries += t["db_queries"]
        http_calls += t["http_calls"]
        walls.append(t["wall_s"])
    walls.sort()

    sleep = merged["sleep_s"]
    gc_s = merged["gc_s"]
    # Only HTTP time spent against hosts that ever hit a real socket counts as
    # wait; calls answered by transport-level mocks (responses, respx) are
    # pure CPU and must not be double-counted against it.
    raw_hosts = merged["raw_http_hosts"]
    http_real_time = sum(
        row["time_s"] for host, row in merged["http_hosts"].items() if host in raw_hosts
    )

    # Approximate decomposition of aggregate in-test time. DB and real-HTTP
    # waits are wall-clock spans during which this process is mostly idle; GC
    # is a subset of CPU; the remainder splits into "other CPU" and
    # unattributed wait (subprocesses, locks, disk...). Components are clamped
    # so rounding and overlap can't produce negatives.
    cpu_other = max(0.0, cpu - gc_s)
    wait_other = max(0.0, agg_wall - cpu - db_time - http_real_time - sleep)
    decomposition = [
        ("DB", db_time),
        ("HTTP (real)", http_real_time),
        ("sleep", sleep),
        ("GC", gc_s),
        ("other CPU", cpu_other),
        ("other wait", wait_other),
    ]

    suite = merged["suite"]
    workers = max(1, suite.get("workers", 0))
    suite_wall = suite.get("wall_s", 0.0)
    parallel_efficiency = (
        agg_wall / (suite_wall * workers) if suite_wall and workers else None
    )

    # Wall-clock-shaped decomposition of the whole run, for the report header.
    # Startup and collection are per-process costs, so the heaviest process
    # stands for the critical path; session fixtures are what pytest bills to
    # each process's first test; test bodies are the rest of an average
    # worker's in-test time; orchestration is the pytest-visible wall time
    # those three don't explain (scheduling, teardown, reporting).
    session_fixture_s = max(
        (lane.get("first_test_setup_s", 0.0) for lane in merged.get("lanes", [])),
        default=0.0,
    )
    per_worker_test_s = agg_wall / workers
    session_fixture_s = min(session_fixture_s, per_worker_test_s)
    startup_s = suite.get("startup_preconfigure_s", 0.0)
    collection_s = suite.get("startup_collection_s", 0.0)
    body_s = per_worker_test_s - session_fixture_s
    phases = [
        ("startup, imports", startup_s),
        ("collection", collection_s),
        ("session fixtures", session_fixture_s),
        ("test bodies", body_s),
        # suite_wall starts at pytest_configure, so it covers every phase but
        # startup; the true end-to-end clock is startup plus suite_wall.
        (
            "orchestration",
            max(0.0, suite_wall - collection_s - session_fixture_s - body_s),
        ),
    ]

    return {
        "outcomes": outcomes,
        "tests": len(per_test),
        "reruns": sum(t.get("reruns", 0) for t in per_test),
        "peak_rss_bytes": max(
            (lane.get("peak_rss_bytes", 0) for lane in merged.get("lanes", [])),
            default=0,
        ),
        "agg_wall_s": agg_wall,
        "cpu_s": cpu,
        "db_queries": db_queries + merged["db_outside"]["queries"],
        "db_queries_in_tests": db_queries,
        "db_time_s": db_time,
        "db_time_outside_s": merged["db_outside"]["time_s"],
        "db_share": db_time / agg_wall if agg_wall else 0.0,
        "http_calls": http_calls,
        "http_time_s": http_time,
        "http_real_time_s": http_real_time,
        "sleep_s": sleep,
        "gc_s": gc_s,
        "file_opens": merged["files"]["total_opens"],
        "setup_s": setup_s,
        "call_s": call_s,
        "teardown_s": teardown_s,
        "p50": percentile(walls, 0.50),
        "p90": percentile(walls, 0.90),
        "p99": percentile(walls, 0.99),
        "mean": agg_wall / len(per_test) if per_test else 0.0,
        "decomposition": decomposition,
        "phases": phases,
        # End-to-end, including the interpreter boot and imports that happen
        # before pytest_configure can start a clock.
        "total_wall_s": sum(v for _, v in phases),
        "parallel_efficiency": parallel_efficiency,
        "suite_wall_s": suite_wall,
        "startup_collect_s": suite.get("startup_collect_s", 0.0),
        "startup_preconfigure_s": suite.get("startup_preconfigure_s", 0.0),
        "startup_collection_s": suite.get("startup_collection_s", 0.0),
        "collect_modifyitems_s": suite.get("collect_modifyitems_s", 0.0),
        "workers": suite.get("workers", 0),
        "warnings_total": sum(merged["warnings"].values()),
    }


def split_nodeid(nodeid: str) -> tuple[str, str, str]:
    """Split a nodeid into (path prefix, test name, parametrize id).

    ``pkg/mod.py::Class::test_name[case]`` becomes
    ``("pkg/mod.py::Class::", "test_name", "case")``; the id is ``""`` when
    the test is not parametrized. Parametrize ids can themselves contain
    ``::`` and brackets, so the id is read as everything between the first
    ``[`` and the trailing ``]``.
    """
    base, param = nodeid, ""
    if nodeid.endswith("]"):
        start = nodeid.find("[")
        if start != -1:
            base, param = nodeid[:start], nodeid[start + 1 : -1]
    prefix, sep, name = base.rpartition("::")
    return prefix + sep, name or base, param


def _nodeid_short(nodeid: str, limit: int = 90) -> str:
    """Shorten a nodeid for prose. The test name survives every cut: the path
    is dropped first, then the tail of the parametrize id."""
    if len(nodeid) <= limit:
        return nodeid
    _, name, param = split_nodeid(nodeid)
    if len(name) + len(param) + 2 <= limit - 1:
        return "…" + nodeid[-(limit - 1) :]
    if not param:
        return name[: limit - 1] + "…"
    return f"{name}[{param[: max(0, limit - len(name) - 3)]}…]"


# Cost totals compared against a baseline run, as (stats key, label, formatter).
BASELINE_TOTALS = [
    ("tests", "tests", lambda v: f"{v:,.0f}"),
    ("agg_wall_s", "aggregate test time", fmt_seconds),
    ("cpu_s", "CPU time", fmt_seconds),
    ("db_queries", "DB queries", lambda v: f"{v:,.0f}"),
    ("db_time_s", "in-DB time", fmt_seconds),
    ("http_calls", "HTTP calls", lambda v: f"{v:,.0f}"),
    ("sleep_s", "sleep time", fmt_seconds),
    ("file_opens", "file opens", lambda v: f"{v:,.0f}"),
    ("suite_wall_s", "suite wall time", fmt_seconds),
]


def build_baseline_delta(
    baseline: dict[str, Any], merged: dict[str, Any], stats: dict[str, Any]
) -> dict[str, Any]:
    """Diff this run against a previous --perf-report-json file."""
    b_stats = baseline.get("stats") or {}
    totals = []
    for key, label, fmt in BASELINE_TOTALS:
        before = b_stats.get(key, 0) or 0
        after = stats.get(key, 0) or 0
        totals.append(
            {
                "key": key,
                "label": label,
                "before": before,
                "after": after,
                "delta": after - before,
                "pct": (after - before) / before if before else None,
                "before_fmt": fmt(before),
                "after_fmt": fmt(after),
            }
        )

    b_shapes = baseline.get("shapes") or {}
    shape_changes = []
    for shape in set(b_shapes) | set(merged["shapes"]):
        before = b_shapes.get(shape, {}).get("count", 0)
        after = merged["shapes"].get(shape, {}).get("count", 0)
        if before != after:
            shape_changes.append(
                {
                    "shape": shape,
                    "before": before,
                    "after": after,
                    "delta": after - before,
                }
            )
    shape_changes.sort(key=lambda c: abs(c["delta"]), reverse=True)

    # Per-test wall regressions, restricted to tests present in both runs so
    # selection changes don't masquerade as slowdowns.
    b_walls = {t["nodeid"]: t["wall_s"] for t in baseline.get("per_test", [])}
    regressions = []
    common = 0
    for t in merged["per_test"]:
        before = b_walls.get(t["nodeid"])
        if before is None:
            continue
        common += 1
        delta = t["wall_s"] - before
        if delta > max(0.05, 0.5 * before):
            regressions.append(
                {
                    "nodeid": t["nodeid"],
                    "before": before,
                    "after": t["wall_s"],
                    "delta": delta,
                }
            )
    regressions.sort(key=lambda r: r["delta"], reverse=True)

    return {
        "baseline_meta": baseline.get("meta") or {},
        # Report JSON written before 0.4.0 carries no schema key.
        "baseline_schema": baseline.get("schema"),
        "totals": totals,
        "shape_changes": shape_changes[:12],
        "test_regressions": regressions[:10],
        "common_tests": common,
    }


def build_todos(
    merged: dict[str, Any],
    stats: dict[str, Any],
    baseline_delta: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    todos: list[dict[str, str]] = []
    per_test = merged["per_test"]
    agg_wall = stats["agg_wall_s"]

    def add(severity: str, title: str, body: str) -> None:
        todos.append({"severity": severity, "title": title, "body": body})

    if merged["meta"].get("missing_workers"):
        add(
            "high",
            f"{merged['meta']['missing_workers']} xdist worker shard(s) are missing",
            "A worker crashed (or doesn't share a filesystem with the controller), "
            "so every total below under-reports. Fix the worker crash before "
            "trusting the numbers.",
        )

    flaky = [t for t in per_test if t.get("reruns", 0) > 0]
    if flaky:
        worst = sorted(flaky, key=lambda t: t["reruns"], reverse=True)[:3]
        names = ", ".join(
            f"{_nodeid_short(t['nodeid'], 60)} ({t['reruns']}×)" for t in worst
        )
        add(
            "high",
            f"{len(flaky)} flaky test{'s' if len(flaky) != 1 else ''} needed reruns to pass",
            f"Reruns hide real race conditions and tax every run: {names}. "
            "Make them deterministic (frozen time, seeded randomness, event-driven "
            "waits) rather than retrying.",
        )

    if baseline_delta and baseline_delta["common_tests"] >= 20:
        by_key = {t["key"]: t for t in baseline_delta["totals"]}
        tests_d = by_key["tests"]
        # Normalize by test count so a bigger selection doesn't read as a
        # regression.
        for key, label in (("db_queries", "queries"), ("agg_wall_s", "test time")):
            row = by_key[key]
            if not (row["before"] and tests_d["before"] and tests_d["after"]):
                continue
            per_before = row["before"] / tests_d["before"]
            per_after = row["after"] / tests_d["after"]
            growth = (per_after - per_before) / per_before if per_before else 0
            if growth > 0.10:
                add(
                    "medium",
                    f"{label.capitalize()} per test regressed {growth:.0%} vs baseline",
                    f"{row['before_fmt']} → {row['after_fmt']} overall "
                    f"({per_before:,.1f} → {per_after:,.1f} per test). See the "
                    "“Vs baseline” section for the query shapes and tests that moved.",
                )

    failed = [t for t in per_test if t["outcome"] in ("failed", "error")]
    if failed:
        names = ", ".join(_nodeid_short(t["nodeid"], 70) for t in failed[:5])
        more = f" (+{len(failed) - 5} more)" if len(failed) > 5 else ""
        add(
            "high",
            f"Fix the {len(failed)} failing test{'s' if len(failed) != 1 else ''}",
            f"Nothing below matters until the suite is green: {names}{more}.",
        )

    external = sorted(
        (
            (endpoint, row)
            for endpoint, row in merged["net_endpoints"].items()
            if row["external"]
        ),
        key=lambda kv: kv[1]["connects"],
        reverse=True,
    )
    if external:
        endpoints = ", ".join(e for e, _ in external[:5])
        total_connects = sum(r["connects"] for _, r in external)
        add(
            "high",
            "Tests are talking to the real network",
            f"{total_connects} socket connection(s) left the machine ({endpoints}). "
            "Real network calls make tests slow, flaky, and dependent on third-party "
            "uptime. Mock them (responses/respx/vcrpy) and enforce hermeticity with "
            "pytest-socket.",
        )

    if stats["sleep_s"] > max(1.0, 0.02 * agg_wall):
        sleepers = heapq.nlargest(3, per_test, key=lambda t: t["sleep_s"])
        names = ", ".join(
            f"{_nodeid_short(t['nodeid'], 60)} ({fmt_seconds(t['sleep_s'])})"
            for t in sleepers
            if t["sleep_s"] > 0
        )
        add(
            "high" if stats["sleep_s"] > 0.10 * agg_wall else "medium",
            f"Remove {fmt_seconds(stats['sleep_s'])} of time.sleep()",
            f"Sleeping is pure dead time — worst offenders: {names}. Replace polling "
            "sleeps with event-driven waits, or stub time.sleep to a no-op in an "
            "autouse fixture if the delays are production pacing, not test logic.",
        )

    n_plus_one = sorted(
        (
            (shape, row)
            for shape, row in merged["shapes"].items()
            if row.get("max_in_one_test", 0) >= N_PLUS_ONE_THRESHOLD
            and row["op"] == "SELECT"
        ),
        key=lambda kv: kv[1]["max_in_one_test"],
        reverse=True,
    )
    if n_plus_one:
        shape, row = n_plus_one[0]
        where = (
            f" (in {_nodeid_short(row['max_test'], 70)})" if row.get("max_test") else ""
        )
        add(
            "medium",
            f"Probable N+1 queries: {len(n_plus_one)} query shape(s) repeat "
            f"≥{N_PLUS_ONE_THRESHOLD}× inside a single test",
            f"Worst: {row['op']} on {row['target']} runs up to {row['max_in_one_test']}× "
            f"in one test{where} — {row['count']:,} times suite-wide. Batch it "
            "(select_related/prefetch_related, joins, or bulk fetches) — that usually "
            "speeds up production too.",
        )

    if agg_wall > 0 and stats["db_share"] > 0.35:
        add(
            "medium",
            f"{stats['db_share']:.0%} of test time is spent inside the database",
            f"{stats['db_queries_in_tests']:,} queries took {fmt_seconds(stats['db_time_s'])}. "
            "Cut per-test setup writes: share read-only fixtures at session scope, "
            "prefer rollback-per-test over flush/truncate, and skip writes no test "
            "asserts on (audit/history rows, request logs).",
        )

    if len(per_test) >= 20:
        slowest = heapq.nlargest(10, per_test, key=lambda t: t["wall_s"])
        slow_share = sum(t["wall_s"] for t in slowest) / agg_wall if agg_wall else 0
        # Require genuine outliers, not just the top decile of a uniform suite.
        if slow_share > 0.25 and slowest[0]["wall_s"] > 4 * stats["p50"]:
            setup_dominated = (
                sum(1 for t in slowest if t["setup_s"] > 0.8 * t["wall_s"])
                >= len(slowest) // 2
            )
            if setup_dominated:
                body = (
                    "Their time is almost entirely the *setup* phase, not the test "
                    "body — pytest charges session-scoped fixture setup (DB "
                    "creation, cache warming) to the first test that needs it, once "
                    "per xdist worker. Cheapen that one-time setup (e.g. restore "
                    "the test database from a template instead of migrating) "
                    "before blaming the tests themselves."
                )
            else:
                body = (
                    "A handful of outliers dominate the suite — start with "
                    f"{_nodeid_short(slowest[0]['nodeid'], 70)} "
                    f"({fmt_seconds(slowest[0]['wall_s'])}). "
                    "Profile them individually before any suite-wide effort."
                )
            add(
                "medium",
                f"10 tests account for {slow_share:.0%} of total test time",
                body,
            )

    if stats["setup_s"] > stats["call_s"] and stats["tests"] >= 20:
        fixtures = heapq.nlargest(
            3, merged["fixtures"].items(), key=lambda kv: kv[1]["total_s"]
        )
        names = ", ".join(
            f"{name} ({fmt_seconds(row['total_s'])})" for name, row in fixtures
        )
        add(
            "medium",
            "Fixture setup costs more than the tests themselves",
            f"Setup totals {fmt_seconds(stats['setup_s'])} vs {fmt_seconds(stats['call_s'])} "
            f"of test bodies. Costliest fixtures: {names}. Widen fixture scope "
            "(module/session) for read-only state, and build less per test.",
        )

    # Autouse function-scoped fixtures run for every test — a per-test tax no
    # test opted into. Self time only, so a parent fixture's children aren't
    # billed twice.
    autouse_taxes = [
        (name, row)
        for name, row in merged["fixtures"].items()
        if row.get("autouse") and row.get("scope") == "function"
    ]
    autouse_total = sum(row.get("self_s", 0.0) for _, row in autouse_taxes)
    if autouse_taxes and autouse_total > max(1.0, 0.05 * agg_wall):
        worst = heapq.nlargest(3, autouse_taxes, key=lambda kv: kv[1].get("self_s", 0))
        names = ", ".join(
            f"{name} ({fmt_seconds(row.get('self_s', 0.0))}"
            + (f", {row['db_queries']:,} queries" if row.get("db_queries") else "")
            + ")"
            for name, row in worst
        )
        add(
            "medium",
            f"Autouse fixtures tax every test {fmt_seconds(autouse_total)} in total",
            f"Function-scoped autouse fixtures run for all {stats['tests']:,} tests "
            f"whether needed or not. Heaviest: {names}. Make the expensive ones "
            "opt-in, move read-only seeding to session scope, or gate the work on "
            "the markers/fixtures that actually need it.",
        )

    if stats["workers"] == 0 and agg_wall > 60:
        add(
            "medium",
            "Parallelize the suite",
            f"{fmt_seconds(agg_wall)} of tests ran on a single process. "
            "pytest-xdist with `-n auto` typically cuts wall time by the core count; "
            "this report already merges per-worker data.",
        )

    if stats["startup_collect_s"] > max(5.0, 0.10 * stats["suite_wall_s"]):
        pre = stats["startup_preconfigure_s"]
        collect = stats["startup_collection_s"]
        split = (
            f" ≈{fmt_seconds(pre)} is interpreter/plugin/conftest imports before "
            f"collection even starts; {fmt_seconds(collect)} is collection."
            if (pre or collect)
            else ""
        )
        workers_note = (
            f" Every xdist worker re-pays it ({stats['workers']}× in CPU this run)."
            if stats["workers"] >= 2
            else ""
        )
        evidence_bits = []
        slow_modules = [
            (path, secs)
            for path, secs in sorted(
                merged.get("collect_modules", {}).items(),
                key=lambda kv: kv[1],
                reverse=True,
            )
            if secs >= 0.2
        ][:3]
        if slow_modules:
            names = ", ".join(f"{p} ({fmt_seconds(s)})" for p, s in slow_modules)
            evidence_bits.append(
                f"slowest test modules to collect — module-scope work runs at "
                f"import: {names}"
            )
        if stats["collect_modifyitems_s"] >= 0.3:
            evidence_bits.append(
                f"pytest_collection_modifyitems hooks took "
                f"{fmt_seconds(stats['collect_modifyitems_s'])} — look for conftest "
                "hooks that touch every item"
            )
        top_imports = [
            (name, row)
            for name, row in sorted(
                merged.get("startup_imports", {}).items(),
                key=lambda kv: kv[1][0],
                reverse=True,
            )
            if row[0] >= 0.1
        ][:3]
        if top_imports:
            names = ", ".join(f"{n} ({fmt_seconds(r[0])})" for n, r in top_imports)
            evidence_bits.append(f"heaviest startup imports: {names}")
        evidence = (
            " Where it goes: " + "; ".join(evidence_bits) + "." if evidence_bits else ""
        )
        add(
            "low",
            f"Startup + collection takes {fmt_seconds(stats['startup_collect_s'])}",
            f"That tax is paid on every run, even single-test runs.{workers_note}"
            f"{split}{evidence} For tight dev loops, pass test file paths instead of "
            "a bare `-k` so only those files are collected; the Startup tab has the "
            "full breakdown.",
        )

    if stats["warnings_total"] > 100:
        top = merged["warnings"].most_common(1)
        example = f" Most common: {top[0][0][0]} ({top[0][1]:,}×)." if top else ""
        add(
            "low",
            f"{stats['warnings_total']:,} warnings emitted",
            f"Warnings rot fast and hide real deprecations.{example} Fix the top "
            "offenders, then enforce with `filterwarnings = error` plus targeted "
            "ignores.",
        )

    skipped = stats["outcomes"].get("skipped", 0)
    if stats["tests"] and skipped / stats["tests"] > 0.05:
        add(
            "low",
            f"{skipped} tests ({skipped / stats['tests']:.0%}) are skipped",
            "Skips silently stop protecting you. Re-enable what can run, delete what "
            "can't, and put a reason + owner on the rest.",
        )

    if stats["gc_s"] >= GC_MIN_S and stats["gc_s"] > GC_CPU_SHARE * stats["cpu_s"]:
        add(
            "low",
            f"Garbage collection burned {fmt_seconds(stats['gc_s'])} "
            f"({merged['gc_collections']:,} collections)",
            "After your app imports settle, call gc.freeze() and raise the gen-0 "
            "threshold in a session fixture — long-lived module objects shouldn't be "
            "re-scanned on every test's allocations.",
        )

    big_growers = [
        t for t in per_test if t.get("rss_growth_bytes", 0) > 100 * 1024 * 1024
    ]
    if big_growers:
        worst = max(big_growers, key=lambda t: t["rss_growth_bytes"])
        add(
            "low",
            f"{len(big_growers)} test(s) raised peak memory by >100MB",
            f"Worst: {_nodeid_short(worst['nodeid'], 70)} "
            f"(+{fmt_bytes(worst['rss_growth_bytes'])}). Peak RSS never comes back "
            "down, so one hungry test sets the memory floor for the whole worker — "
            "stream instead of materializing, or shrink the fixture data.",
        )

    if stats["tests"] and stats["file_opens"] / max(1, stats["tests"]) > 50:
        # Caching advice is only honest when re-read *volume* is real: a 5×
        # reopen of a small file costs milliseconds and is not a finding.
        sizes = merged["files"].get("sizes", {})
        heavy = [
            (path, n, sizes[path])
            for path, n in merged["files"]["top"].most_common(10)
            if n >= 10 and sizes.get(path) and n * sizes[path] >= 5 * 1024 * 1024
        ]
        if heavy:
            path, n, size = max(heavy, key=lambda row: row[1] * row[2])
            body = (
                f"Re-reading the same file is cheap to cache at module or session "
                f"scope. Start with {path}: {n:,} opens × {fmt_bytes(size)} "
                f"≈ {fmt_bytes(n * size)} re-read."
            )
        else:
            body = (
                "No single file is re-read enough to be worth caching (the top "
                "re-reads are small — milliseconds at best). The volume is spread "
                "across distinct files, which usually means per-test tmp/fixture "
                "churn; check whether tests write artifacts nothing reads."
            )
        add(
            "low",
            f"{stats['file_opens']:,} file opens (~{stats['file_opens'] / stats['tests']:.0f} per test)",
            body,
        )

    todos.sort(key=lambda t: SEVERITY_RANK[t["severity"]])
    if not todos:
        add(
            "low",
            "No obvious problems found",
            "The suite is green, hermetic, and has no dominant time sink. Next lever: "
            "track this report over time and fail the build on regressions.",
        )
    return todos
