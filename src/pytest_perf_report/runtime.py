"""Mutable session state shared between the pytest hooks and the probes.

The probes (monkeypatched stdlib / driver functions) are only installed while
a profiled session is active, and they all funnel through the ``record_*``
functions here. Everything tolerates ``STATE is None`` so a stray late call
from a lingering thread can never break the suite under test. Counter updates
from background threads are best-effort (lossy under races, never crashing).
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from pytest_perf_report.sqlshape import classify, normalize_sql

# Caps that keep memory bounded on very large or very weird suites.
MAX_TRACKED_FILES = 20_000
MAX_TRACKED_SHAPES = 10_000
MAX_FAILURE_CHARS = 4_000
MAX_EXAMPLE_CHARS = 500
MAX_ORIGINS_PER_SHAPE = 8

OVERFLOW_SHAPE = "<other shapes past the tracking cap>"

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclass
class TestRecord:
    nodeid: str
    outcome: str = "passed"
    wall_s: float = 0.0
    cpu_s: float = 0.0
    setup_s: float = 0.0
    call_s: float = 0.0
    teardown_s: float = 0.0
    db_queries: int = 0
    db_time_s: float = 0.0
    http_calls: int = 0
    http_time_s: float = 0.0
    sleep_s: float = 0.0
    gc_s: float = 0.0
    file_opens: int = 0
    # Times this test was rerun by a flake plugin (pytest-rerunfailures).
    reruns: int = 0
    # How much this test raised the process's peak RSS (ru_maxrss delta) —
    # nonzero only for tests that set a new memory high-water mark.
    rss_growth_bytes: int = 0
    failure: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodeid": self.nodeid,
            "outcome": self.outcome,
            "wall_s": round(self.wall_s, 6),
            "cpu_s": round(self.cpu_s, 6),
            "setup_s": round(self.setup_s, 6),
            "call_s": round(self.call_s, 6),
            "teardown_s": round(self.teardown_s, 6),
            "db_queries": self.db_queries,
            "db_time_s": round(self.db_time_s, 6),
            "http_calls": self.http_calls,
            "http_time_s": round(self.http_time_s, 6),
            "sleep_s": round(self.sleep_s, 6),
            "gc_s": round(self.gc_s, 6),
            "file_opens": self.file_opens,
            "reruns": self.reruns,
            "rss_growth_bytes": self.rss_growth_bytes,
            "failure": self.failure,
        }


@dataclass
class SessionState:
    html_path: str | None
    json_path: str | None
    shard_dir: str
    shard_id: str
    is_worker: bool

    # --perf-report-sql-origins: capture the project call-site of every query
    # (a per-query stack walk; meaningful overhead, so opt-in). Independent of
    # the flag, the call-site of each shape's *first* occurrence is always
    # recorded — one stack walk per distinct shape is noise.
    sql_origins: bool = False
    origin_root: str = ""
    # --perf-report-cpu-profile: cProfile every test and aggregate function
    # self-times, to decompose the "other CPU" bucket. Meaningful overhead,
    # opt-in. "relpath:lineno:function" -> [self_s, calls]
    cpu_profile: bool = False
    cpu_profile_funcs: dict[str, list[float]] = field(default_factory=dict)

    config: Any = None
    t_configure: float = 0.0
    t_collect_done: float = 0.0
    t_finish: float = 0.0
    # Process CPU consumed before pytest_configure ran: interpreter boot,
    # plugin and conftest imports.
    preconfigure_cpu: float = 0.0
    # Startup-phase import costs, captured by the __import__ timer between
    # initial-conftest loading and pytest_configure: module -> [self_s, cum_s].
    startup_imports: dict[str, list[float]] = field(default_factory=dict)
    # Module nodeid -> seconds spent collecting it (which is where the module
    # import happens — module-scope work lands here).
    collect_modules: dict[str, float] = field(default_factory=dict)
    # Total wall time inside pytest_collection_modifyitems implementations
    # (conftest hooks that walk every collected item live here).
    collect_modifyitems_s: float = 0.0
    # Epoch (time.time()) phase boundaries for the run timeline — unlike
    # perf_counter these are comparable across local processes, so worker
    # lanes can share the controller's time axis.
    epoch_configure: float = 0.0
    epoch_collect_done: float = 0.0
    epoch_first_test: float = 0.0
    epoch_last_test: float = 0.0
    epoch_finish: float = 0.0
    first_test_setup_s: float = 0.0
    peak_rss_bytes: int = 0
    baseline_path: str | None = None

    current: TestRecord | None = None
    per_test: list[TestRecord] = field(default_factory=list)

    # shape -> {count, db_time_s, example, op, target, tests, max_in_one_test,
    #           _cur (count within the in-flight test)}
    shapes: dict[str, dict[str, Any]] = field(default_factory=dict)
    shapes_touched: set[str] = field(default_factory=set)
    db_vendors: Counter = field(default_factory=Counter)
    db_queries_outside: int = 0
    db_time_outside: float = 0.0

    # host -> {calls, time_s}
    http_hosts: dict[str, dict[str, float]] = field(default_factory=dict)
    raw_http_hosts: set[str] = field(default_factory=set)

    # "host:port" -> {connects, time_s, external}
    net_endpoints: dict[str, dict[str, Any]] = field(default_factory=dict)

    file_opens: Counter = field(default_factory=Counter)
    file_opens_overflow: int = 0

    sleep_total_s: float = 0.0
    gc_total_s: float = 0.0
    gc_collections: int = 0

    # fixture name -> {total_s, self_s, count, max_s, db_queries, db_time_s,
    #                  db_writes, scope, autouse}
    fixtures: dict[str, dict[str, Any]] = field(default_factory=dict)
    # In-flight fixture setups (innermost last): {"name", "child_s"}. Lets
    # self-time subtract nested setups (request.getfixturevalue chains) and
    # lets record_query attribute setup-phase queries to the fixture running
    # them.
    fixture_stack: list[dict[str, Any]] = field(default_factory=list)
    # nodeid of this process's first test — pytest charges all session-scoped
    # fixture setup to it, so the report flags it instead of letting it
    # masquerade as a slow test.
    first_test_nodeid: str = ""

    # (category, message head) -> count
    warnings: Counter = field(default_factory=Counter)
    # Set when xdist workers exist: their forwarded warnings must not be
    # re-counted on the controller (each worker's shard already carries them).
    xdist_active: bool = False
    expected_workers: int = 0

    io_start: tuple[int, int] = (0, 0)
    io_read_bytes: int = 0
    io_write_bytes: int = 0

    meta: dict[str, Any] = field(default_factory=dict)
    summary_lines: list[str] = field(default_factory=list)
    owns_shard_dir: bool = False


STATE: SessionState | None = None

# Re-entrancy depth for HTTP client layers, so a requests/httpx call doesn't
# get double-counted when it bottoms out in http.client. A ContextVar (not a
# threading.local) so concurrent asyncio tasks on one thread don't suppress
# each other's unrelated calls.
_http_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "perf_report_http_depth", default=0
)


def http_depth() -> int:
    return _http_depth.get()


@contextlib.contextmanager
def timed_http_call(host_fn: Callable[[], str]) -> Iterator[None]:
    """Shared bracket for client-level HTTP probes (requests/httpx)."""
    t0 = perf_counter()
    token = _http_depth.set(_http_depth.get() + 1)
    try:
        yield
    finally:
        _http_depth.reset(token)
        try:
            host = host_fn() or "?"
        except Exception:
            host = "?"
        record_http_call(host, perf_counter() - t0)


def _query_origin(root: str) -> str:
    """Deepest project-level call-site above the driver, as 'relpath:lineno'."""
    frame = sys._getframe(2)  # record_query <- probe hook <- caller
    depth = 0
    while frame is not None and depth < 60:
        filename = frame.f_code.co_filename
        if (
            filename.startswith(root)
            and not filename.startswith(_PACKAGE_DIR)
            and f"{os.sep}site-packages{os.sep}" not in filename
            and f"{os.sep}.venv{os.sep}" not in filename
        ):
            return f"{os.path.relpath(filename, root)}:{frame.f_lineno}"
        frame = frame.f_back
        depth += 1
    return "?"


def new_fixture_row() -> dict[str, Any]:
    return {
        "total_s": 0.0,
        "self_s": 0.0,
        "count": 0,
        "max_s": 0.0,
        "db_queries": 0,
        "db_time_s": 0.0,
        "db_writes": 0,
    }


_WRITE_OPS = frozenset({"INSERT", "UPDATE", "DELETE"})


def record_query(sql: str, duration: float, vendor: str = "?") -> None:
    state = STATE
    if state is None:
        return
    rec = state.current
    if rec is not None:
        rec.db_queries += 1
        rec.db_time_s += duration
    else:
        state.db_queries_outside += 1
        state.db_time_outside += duration
    state.db_vendors[vendor] += 1

    shape = normalize_sql(sql)
    row = state.shapes.get(shape)
    if row is None:
        if len(state.shapes) >= MAX_TRACKED_SHAPES:
            shape = OVERFLOW_SHAPE
            row = state.shapes.get(shape)
        if row is None:
            op, target = classify(sql)
            try:
                origin_first = _query_origin(state.origin_root)
            except Exception:
                origin_first = "?"
            row = {
                "count": 0,
                "db_time_s": 0.0,
                "example": sql[:MAX_EXAMPLE_CHARS],
                "op": op,
                "target": target,
                "origin_first": origin_first,
                "tests": 0,
                "max_in_one_test": 0,
                "max_test": "",
                "_cur": 0,
            }
            state.shapes[shape] = row
    row["count"] += 1
    row["db_time_s"] += duration
    if state.sql_origins:
        try:
            origins = row.setdefault("_origins", Counter())
            origins[_query_origin(state.origin_root)] += 1
        except Exception:
            pass
    if rec is not None:
        if row["_cur"] == 0:
            row["tests"] += 1
            state.shapes_touched.add(shape)
        row["_cur"] += 1

    # Attribute setup-phase queries to the innermost in-flight fixture, so the
    # fixture table can say what each fixture costs in DB work, not just time.
    if state.fixture_stack:
        try:
            fixture_row = state.fixtures.setdefault(
                state.fixture_stack[-1]["name"], new_fixture_row()
            )
            fixture_row["db_queries"] += 1
            fixture_row["db_time_s"] += duration
            if row["op"] in _WRITE_OPS:
                fixture_row["db_writes"] += 1
        except Exception:  # lossy under races with background threads
            pass


def flush_test_shapes(state: SessionState, nodeid: str = "") -> None:
    """Roll per-test shape counters into max_in_one_test; called between tests.

    Iterates a snapshot: a background thread may record a query (and add to
    shapes_touched) mid-flush; that late entry is picked up on the next flush
    instead of crashing this one.
    """
    touched = tuple(state.shapes_touched)
    state.shapes_touched.clear()
    for shape in touched:
        row = state.shapes.get(shape)
        if row is None:
            continue
        if row["_cur"] > row["max_in_one_test"]:
            row["max_in_one_test"] = row["_cur"]
            row["max_test"] = nodeid
        row["_cur"] = 0


def record_http_call(host: str, duration: float) -> None:
    state = STATE
    if state is None:
        return
    host = host.lower()
    rec = state.current
    if rec is not None:
        rec.http_calls += 1
        rec.http_time_s += duration
    row = state.http_hosts.setdefault(host, {"calls": 0, "time_s": 0.0})
    row["calls"] += 1
    row["time_s"] += duration


def record_raw_http_host(host: str) -> None:
    state = STATE
    if state is None:
        return
    state.raw_http_hosts.add(host.lower())


def record_net_connect(endpoint: str, duration: float, external: bool) -> None:
    state = STATE
    if state is None:
        return
    row = state.net_endpoints.setdefault(
        endpoint, {"connects": 0, "time_s": 0.0, "external": external}
    )
    row["connects"] += 1
    row["time_s"] += duration


def record_sleep(duration: float) -> None:
    state = STATE
    if state is None:
        return
    state.sleep_total_s += duration
    if state.current is not None:
        state.current.sleep_s += duration


def record_gc(duration: float) -> None:
    state = STATE
    if state is None:
        return
    state.gc_total_s += duration
    state.gc_collections += 1
    if state.current is not None:
        state.current.gc_s += duration


def record_file_open(path: Any) -> None:
    state = STATE
    if state is None:
        return
    if state.current is not None:
        state.current.file_opens += 1
    try:
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        key = str(path)
        # open() accepts bytes, so libraries sometimes probe content as a
        # path (feedparser tries open(<feed body>) before treating it as
        # data). Keying those by content would fill the table with blobs.
        if len(key) > 260 or "\n" in key or "\x00" in key:
            key = "<non-path open() argument>"
    except Exception:
        key = "<unrepresentable>"
    if key in state.file_opens or len(state.file_opens) < MAX_TRACKED_FILES:
        state.file_opens[key] += 1
    else:
        state.file_opens_overflow += 1
