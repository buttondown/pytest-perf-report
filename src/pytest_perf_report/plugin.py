"""pytest hooks: lifecycle, per-test bracketing, xdist sharding, rendering.

Everything is gated on ``--perf-report`` / ``--perf-report-json``; without a
flag the plugin registers its options and otherwise never touches a clock.

Under pytest-xdist, each worker records its own tests and writes one JSON
shard into a temp directory the controller hands out via ``workerinput``; the
controller merges the shards (plus its own in-memory data) and renders. A
plain (non-xdist) run is the degenerate zero-worker case of the same pipeline.
Workers must share a filesystem with the controller (local xdist); when a
worker's shard is missing the report says so rather than under-reporting
silently.
"""

from __future__ import annotations

import builtins
import functools
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from datetime import datetime
from typing import Any

import pytest

try:
    import resource

    def _peak_rss_bytes() -> int:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS, kilobytes everywhere else.
        return peak if sys.platform == "darwin" else peak * 1024

except ImportError:  # Windows

    def _peak_rss_bytes() -> int:
        return 0


from pytest_perf_report import __version__, runtime
from pytest_perf_report.analyze import (
    build_baseline_delta,
    build_todos,
    compute,
    fmt_seconds,
)
from pytest_perf_report.merge import (
    SCHEMA_VERSION,
    load_shards,
    merge_shards,
    state_to_shard,
    write_shard,
)
from pytest_perf_report.probes import Probe, build_probes
from pytest_perf_report.runtime import (
    MAX_FAILURE_CHARS,
    SessionState,
    TestRecord,
    flush_test_shapes,
    new_fixture_row,
)

_PROBES: list[Probe] = []
# Probes that actually override refresh(); recomputed at install so the
# per-test loop doesn't pay for eight no-ops.
_REFRESH_PROBES: list[Probe] = []


def _resolve_import_name(name: str, globals_: Any, level: int) -> str:
    """Best-effort absolute name for a relative import (``from . import x``)."""
    if not level:
        return name
    try:
        base = (globals_ or {}).get("__package__") or (globals_ or {}).get(
            "__name__", ""
        )
        if level > 1:
            base = base.rsplit(".", level - 1)[0]
        return f"{base}.{name}" if name else base
    except Exception:
        return "." * level + name


class _ImportTimer:
    """importtime-style timer around ``builtins.__import__``.

    Active only from initial-conftest loading until pytest_configure, so it
    sees exactly the startup imports (plugins, conftests, and whatever they
    pull in — e.g. a Django app graph) and none of the test-module imports,
    which the per-module collection timer attributes instead. Self time is
    elapsed minus nested imports, tracked with a stack; already-cached plain
    imports skip timing entirely so the wrapper stays cheap. Bookkeeping is
    best-effort under threads (lossy, never crashing), matching the probes.
    """

    def __init__(self) -> None:
        self.totals: dict[str, list[float]] = {}  # name -> [self_s, cum_s]
        self._stack: list[list[float]] = []
        self._orig: Any = None
        self._wrapper: Any = None

    def install(self) -> None:
        self._orig = orig = builtins.__import__

        @functools.wraps(orig)
        def import_(
            name: str,
            globals: Any = None,
            locals: Any = None,
            fromlist: Any = (),
            level: int = 0,
        ) -> Any:
            if not level and not fromlist and name in sys.modules:
                return orig(name, globals, locals, fromlist, level)
            frame = [0.0]
            self._stack.append(frame)
            t0 = time.perf_counter()
            try:
                return orig(name, globals, locals, fromlist, level)
            finally:
                elapsed = time.perf_counter() - t0
                try:
                    # Identity, not equality: distinct frames compare equal
                    # ([0.0] == [0.0]), so list.remove would pop an ancestor.
                    if self._stack and self._stack[-1] is frame:
                        self._stack.pop()
                    else:
                        for i in range(len(self._stack) - 1, -1, -1):
                            if self._stack[i] is frame:
                                del self._stack[i]
                                break
                    if self._stack:
                        self._stack[-1][0] += elapsed
                    row = self.totals.setdefault(
                        _resolve_import_name(name, globals, level), [0.0, 0.0]
                    )
                    row[0] += max(0.0, elapsed - frame[0])
                    row[1] += elapsed
                except Exception:
                    pass

        self._wrapper = import_
        builtins.__import__ = import_

    def uninstall(self) -> None:
        if self._orig is not None and builtins.__import__ is self._wrapper:
            builtins.__import__ = self._orig


# Installed before SessionState exists (conftests load during config parsing,
# ahead of pytest_configure), so it lives at module level until configure
# claims its totals.
_IMPORT_TIMER: _ImportTimer | None = None


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config: Any, parser: Any, args: Any) -> Any:
    ns = getattr(early_config, "known_args_namespace", None)
    enabled = ns is not None and (
        getattr(ns, "perf_report", None) or getattr(ns, "perf_report_json", None)
    )
    global _IMPORT_TIMER
    if enabled and _IMPORT_TIMER is None and runtime.STATE is None:
        _IMPORT_TIMER = _ImportTimer()
        _IMPORT_TIMER.install()
    yield


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup("perf-report")
    group.addoption(
        "--perf-report",
        nargs="?",
        const="perf-report.html",
        default=None,
        metavar="PATH",
        help="Write an HTML performance report for this run (default: perf-report.html).",
    )
    group.addoption(
        "--perf-report-json",
        nargs="?",
        const="perf-report.json",
        default=None,
        metavar="PATH",
        help="Also write the merged report data as JSON (default: perf-report.json).",
    )
    group.addoption(
        "--perf-report-sql-origins",
        action="store_true",
        default=False,
        help="Record the project call-site of every DB query (per-query stack "
        "walk: meaningful overhead, profiling runs only).",
    )
    group.addoption(
        "--perf-report-baseline",
        default=None,
        metavar="PATH",
        help="A previous --perf-report-json file to diff this run against "
        "(adds a 'vs baseline' section and regression TODOs).",
    )
    group.addoption(
        "--perf-report-cpu-profile",
        action="store_true",
        default=False,
        help="cProfile every test and aggregate function self-times into a "
        "'Where the CPU went' section (meaningful overhead, profiling runs only).",
    )


def _validate_output_path(path: str, flag: str, suffixes: tuple[str, ...]) -> None:
    """Refuse to overwrite something that is clearly not a report.

    ``--perf-report`` takes an optional value, so ``pytest --perf-report
    tests/test_foo.py`` swallows the file argument as the report path —
    without this guard the session would end by overwriting that test file
    with HTML.
    """
    if os.path.isdir(path):
        raise pytest.UsageError(
            f"{flag}: {path!r} is a directory. Use {flag}=PATH to set an output file."
        )
    if os.path.exists(path) and not path.lower().endswith(suffixes):
        raise pytest.UsageError(
            f"{flag}: refusing to overwrite existing non-report file {path!r} "
            f"(did a test path get swallowed as the option value? Use {flag}=PATH)."
        )


def _proc_self_io() -> tuple[int, int]:
    """(read_bytes, write_bytes) for this process; (0, 0) off Linux.

    Uses os primitives rather than open() because the file probe patches
    builtins.open and we must not count our own bookkeeping.
    """
    try:
        fd = os.open("/proc/self/io", os.O_RDONLY)
    except OSError:
        return (0, 0)
    try:
        data = os.read(fd, 4096).decode()
    finally:
        os.close(fd)
    read_b = write_b = 0
    for line in data.splitlines():
        if line.startswith("read_bytes:"):
            read_b = int(line.split(":")[1])
        elif line.startswith("write_bytes:"):
            write_b = int(line.split(":")[1])
    return (read_b, write_b)


@pytest.hookimpl(trylast=True)
def pytest_configure(config: Any) -> None:
    # Claim the startup import timer first: by configure time (trylast, so
    # other plugins' configure-time imports are still counted) the startup
    # phase is over, and the wrapper must come off whether or not this
    # session ends up profiled.
    global _IMPORT_TIMER
    import_timer, _IMPORT_TIMER = _IMPORT_TIMER, None
    if import_timer is not None:
        import_timer.uninstall()

    html_path = config.getoption("--perf-report")
    json_path = config.getoption("--perf-report-json")
    if html_path is None and json_path is None:
        return
    if runtime.STATE is not None:
        # A nested pytest session (e.g. pytester) while a profiled session is
        # active: profiling the inner run would corrupt the outer one. The
        # per-test hooks below carry config-identity guards for the same
        # reason.
        return
    if html_path:
        _validate_output_path(html_path, "--perf-report", (".html", ".htm"))
    if json_path:
        _validate_output_path(json_path, "--perf-report-json", (".json",))
    baseline_path = config.getoption("--perf-report-baseline")
    if baseline_path and not os.path.isfile(baseline_path):
        raise pytest.UsageError(
            f"--perf-report-baseline: {baseline_path!r} does not exist "
            "(expected a previous --perf-report-json output)."
        )

    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:
        shard_dir = workerinput.get("perf_report_shard_dir") or tempfile.mkdtemp(
            prefix="pytest-perf-report-"
        )
        shard_id = workerinput.get("workerid", "gw?")
        is_worker = True
        owns = False
    else:
        shard_dir = tempfile.mkdtemp(prefix="pytest-perf-report-")
        shard_id = "main"
        is_worker = False
        owns = True

    state = SessionState(
        html_path=html_path,
        json_path=json_path,
        shard_dir=shard_dir,
        shard_id=shard_id,
        is_worker=is_worker,
    )
    state.config = config
    state.owns_shard_dir = owns
    state.sql_origins = bool(config.getoption("--perf-report-sql-origins"))
    state.cpu_profile = bool(config.getoption("--perf-report-cpu-profile"))
    state.origin_root = str(config.rootpath)
    state.baseline_path = baseline_path
    state.t_configure = time.perf_counter()
    state.preconfigure_cpu = time.process_time()
    state.epoch_configure = time.time()
    if import_timer is not None:
        state.startup_imports = import_timer.totals
    state.meta = {
        "project": os.path.basename(str(config.rootpath)),
        "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "pytest": pytest.__version__,
        "plugin": __version__,
        "args": "pytest " + " ".join(config.invocation_params.args),
        "invocation_dir": str(config.invocation_params.dir),
    }
    state.io_start = _proc_self_io()

    config._perf_report_state = state
    runtime.STATE = state

    global _PROBES, _REFRESH_PROBES
    _PROBES = build_probes()
    for probe in _PROBES:
        try:
            probe.install()
        except Exception:  # a broken probe must never take the suite down
            try:
                probe.uninstall()
            except Exception:
                pass
    _REFRESH_PROBES = [p for p in _PROBES if type(p).refresh is not Probe.refresh]


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    """xdist (controller side): hand each worker the shard directory."""
    state = getattr(node.config, "_perf_report_state", None)
    if state is not None:
        node.workerinput["perf_report_shard_dir"] = state.shard_dir
        state.xdist_active = True
        state.expected_workers += 1


def pytest_collection_finish(session: Any) -> None:
    state = runtime.STATE
    if state is None or state.config is not session.config:
        return
    if not state.t_collect_done:
        state.t_collect_done = time.perf_counter()
        state.epoch_collect_done = time.time()


# In-memory cap on distinct modules, a backstop for pathological suites; the
# shard caps what travels anyway (merge.MAX_COLLECT_MODULE_ROWS).
MAX_TRACKED_COLLECT_MODULES = 20_000


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: Any) -> Any:
    """Time each test module's collection — that is where the module import
    happens, so module-scope work (globs, file reads, parametrize builders)
    lands here and nowhere else."""
    state = runtime.STATE
    if (
        state is None
        or not isinstance(collector, pytest.Module)
        or state.config is not collector.config
    ):
        yield
        return
    t0 = time.perf_counter()
    yield
    key = collector.nodeid or str(getattr(collector, "path", collector))
    modules = state.collect_modules
    if key in modules or len(modules) < MAX_TRACKED_COLLECT_MODULES:
        modules[key] = modules.get(key, 0.0) + (time.perf_counter() - t0)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(session: Any, config: Any, items: Any) -> Any:
    """Bracket every other pytest_collection_modifyitems implementation —
    conftest hooks that walk all collected items (inspect.getsource per item
    and the like) are otherwise invisible inside the collection number."""
    state = runtime.STATE
    if state is None or state.config is not config:
        yield
        return
    t0 = time.perf_counter()
    yield
    state.collect_modifyitems_s += time.perf_counter() - t0


def _fold_cpu_profile(state: SessionState, profiler: Any) -> None:
    """Fold one test's cProfile entries into the session function totals."""
    root = state.origin_root.rstrip(os.sep) + os.sep
    sep_sp = os.sep + "site-packages" + os.sep
    funcs = state.cpu_profile_funcs
    for entry in profiler.getstats():
        code = entry.code
        if isinstance(code, str):  # builtin, e.g. "<built-in method ...>"
            key = code
        else:
            filename = code.co_filename
            if filename.startswith(root):
                filename = filename[len(root) :]
            else:
                idx = filename.find(sep_sp)
                if idx != -1:
                    filename = filename[idx + len(sep_sp) :]
            key = f"{filename}:{code.co_firstlineno}:{code.co_name}"
        row = funcs.get(key)
        if row is None:
            if len(funcs) >= 100_000:  # runaway-suite backstop
                continue
            funcs[key] = [entry.inlinetime, entry.callcount]
        else:
            row[0] += entry.inlinetime
            row[1] += entry.callcount


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: Any, nextitem: Any) -> Any:
    state = runtime.STATE
    if state is None or state.config is not item.config:
        yield
        return
    for probe in _REFRESH_PROBES:
        try:
            probe.refresh()
        except Exception:
            pass
    rec = TestRecord(nodeid=item.nodeid)
    state.current = rec
    if not state.epoch_first_test:
        state.epoch_first_test = time.time()
        state.first_test_nodeid = item.nodeid
    profiler = None
    if state.cpu_profile:
        import cProfile

        profiler = cProfile.Profile()
        try:
            profiler.enable()
        except Exception:  # another profiler already owns sys.setprofile
            profiler = None
    rss0 = _peak_rss_bytes()
    cpu0 = time.process_time()
    wall0 = time.perf_counter()
    try:
        yield
    finally:
        rec.wall_s = time.perf_counter() - wall0
        rec.cpu_s = time.process_time() - cpu0
        rec.rss_growth_bytes = max(0, _peak_rss_bytes() - rss0)
        if profiler is not None:
            try:
                profiler.disable()
                _fold_cpu_profile(state, profiler)
            except Exception:
                pass
        state.epoch_last_test = time.time()
        state.current = None
        state.per_test.append(rec)
        if len(state.per_test) == 1:
            state.first_test_setup_s = rec.setup_s
        flush_test_shapes(state, rec.nodeid)


def pytest_runtest_logreport(report: Any) -> None:
    state = runtime.STATE
    if state is None:
        return
    rec = state.current
    if rec is None or rec.nodeid != report.nodeid:
        return
    duration = getattr(report, "duration", 0.0) or 0.0
    if report.when == "setup":
        rec.setup_s = duration
    elif report.when == "call":
        rec.call_s = duration
    elif report.when == "teardown":
        rec.teardown_s = duration

    # Ask pytest (and whatever outcome-shaping plugins are installed: xfail,
    # rerunfailures, ...) how it categorizes this report instead of
    # re-deriving the semantics. A later phase never downgrades a recorded
    # failure (e.g. a teardown error must not relabel a call failure).
    try:
        category = state.config.hook.pytest_report_teststatus(
            report=report, config=state.config
        )[0]
    except Exception:
        category = report.outcome
    if category == "rerun":
        # A flake plugin is retrying; count it but let the final attempt
        # decide the outcome.
        rec.reruns += 1
    elif category and rec.outcome not in ("failed", "error"):
        rec.outcome = category
    if report.failed and rec.failure is None:
        try:
            rec.failure = report.longreprtext[:MAX_FAILURE_CHARS]
        except Exception:
            rec.failure = str(report.longrepr)[:MAX_FAILURE_CHARS]


def _fixture_autouse(fixturedef: Any, request: Any) -> bool:
    """Best-effort autouse detection across pytest versions."""
    # pytest <8.4 stores the decorator's marker on the fixture function.
    for obj in (getattr(fixturedef, "func", None), fixturedef):
        for attr in ("_pytestfixturefunction", "_fixture_function_marker"):
            marker = getattr(obj, attr, None)
            if marker is not None and hasattr(marker, "autouse"):
                return bool(marker.autouse)
    # Newer pytest unwraps the function on FixtureDef; fall back to the
    # manager's autouse registry (private but long-stable).
    try:
        by_node = request.session._fixturemanager._nodeid_autousenames
        return any(fixturedef.argname in names for names in by_node.values())
    except Exception:
        return False


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef: Any, request: Any) -> Any:
    state = runtime.STATE
    if state is None or state.config is not request.config:
        yield
        return
    # Static dependencies are set up before this hook fires, so the elapsed
    # time here is mostly this fixture's own body. The exception is dynamic
    # request.getfixturevalue() calls inside the body — the stack lets
    # self_s subtract those nested setups (and lets record_query attribute
    # setup-phase queries to the fixture running them).
    frame = {"name": fixturedef.argname, "child_s": 0.0}
    state.fixture_stack.append(frame)
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        if state.fixture_stack and state.fixture_stack[-1] is frame:
            state.fixture_stack.pop()
        if state.fixture_stack:
            state.fixture_stack[-1]["child_s"] += elapsed
        row = state.fixtures.setdefault(fixturedef.argname, new_fixture_row())
        row["total_s"] += elapsed
        row["self_s"] += max(0.0, elapsed - frame["child_s"])
        row["count"] += 1
        row["max_s"] = max(row["max_s"], elapsed)
        if "scope" not in row:
            row["scope"] = str(getattr(fixturedef, "scope", "?"))
            row["autouse"] = _fixture_autouse(fixturedef, request)


def pytest_warning_recorded(
    warning_message: Any, when: Any, nodeid: Any, location: Any
) -> None:
    state = runtime.STATE
    if state is None:
        return
    # xdist forwards worker warnings to the controller; each worker's shard
    # already counts them, so counting the forwarded copy would double every
    # warning in the merged report.
    if state.xdist_active and not state.is_worker:
        return
    try:
        category = warning_message.category.__name__
        message = str(warning_message.message)[:120]
    except Exception:
        return
    state.warnings[(category, message)] += 1


def _resolve(path: str, state: SessionState) -> str:
    if not os.path.isabs(path):
        path = os.path.join(state.meta.get("invocation_dir", "."), path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def _jsonable(
    merged: dict[str, Any], stats: dict[str, Any], todos: list[dict[str, str]]
) -> dict[str, Any]:
    out = {"schema": SCHEMA_VERSION, **merged}
    out["raw_http_hosts"] = sorted(merged["raw_http_hosts"])
    out["files"] = {
        **merged["files"],
        "top": merged["files"]["top"].most_common(200),
    }
    out["warnings"] = [[list(k), n] for k, n in merged["warnings"].most_common(200)]
    out["stats"] = {k: v for k, v in stats.items() if k not in ("decomposition",)}
    out["stats"]["decomposition"] = [[label, v] for label, v in stats["decomposition"]]
    out["todos"] = todos
    return out


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: Any, exitstatus: Any) -> None:
    state = runtime.STATE
    if (
        state is None
        or getattr(session.config, "_perf_report_state", None) is not state
    ):
        return
    runtime.STATE = None
    for probe in _PROBES:
        try:
            probe.uninstall()
        except Exception:
            pass
    _PROBES.clear()
    _REFRESH_PROBES.clear()

    state.t_finish = time.perf_counter()
    state.epoch_finish = time.time()
    state.peak_rss_bytes = _peak_rss_bytes()
    io_start = state.io_start
    io_end = _proc_self_io()
    state.io_read_bytes = max(0, io_end[0] - io_start[0])
    state.io_write_bytes = max(0, io_end[1] - io_start[1])

    if state.is_worker:
        # The controller reports a missing shard if this write fails.
        try:
            write_shard(state)
        except OSError:
            pass
        return

    # The controller's own data stays in memory; only worker shards round-trip
    # through disk.
    shards = load_shards(state.shard_dir) + [state_to_shard(state)]
    merged = merge_shards(shards)
    missing_workers = state.expected_workers - merged["suite"]["workers"]
    if missing_workers > 0:
        merged["meta"]["missing_workers"] = missing_workers

    # Size the most-reopened files so the report can talk about re-read
    # *volume*, not just open counts — a 5× reopen of a 1KB file is noise, of
    # a 500MB file a finding. One stat per file, controller only.
    sizes: dict[str, int] = {}
    invocation_dir = merged["meta"].get("invocation_dir", ".")
    for path, _count in merged["files"]["top"].most_common(20):
        if path.startswith("<"):
            continue
        full = path if os.path.isabs(path) else os.path.join(invocation_dir, path)
        try:
            sizes[path] = os.path.getsize(full)
        except OSError:
            continue
    merged["files"]["sizes"] = sizes

    stats = compute(merged)

    baseline_delta = None
    if state.baseline_path:
        try:
            with open(state.baseline_path) as f:
                baseline_delta = build_baseline_delta(json.load(f), merged, stats)
        except (OSError, ValueError):
            baseline_delta = None
    todos = build_todos(merged, stats, baseline_delta)

    json_abspath = None
    if state.json_path:
        out = _jsonable(merged, stats, todos)
        if baseline_delta is not None:
            out["baseline_delta"] = baseline_delta
        json_abspath = _resolve(state.json_path, state)
        with open(json_abspath, "w") as f:
            json.dump(out, f, indent=1)
    html_abspath = None
    if state.html_path:
        from pytest_perf_report.report import render

        html_abspath = _resolve(state.html_path, state)
        with open(html_abspath, "w") as f:
            f.write(render(merged, stats, todos, baseline_delta))

    if state.owns_shard_dir:
        shutil.rmtree(state.shard_dir, ignore_errors=True)

    state.summary_lines = _summary_lines(
        stats, todos, baseline_delta, missing_workers, html_abspath, json_abspath
    )


def _summary_lines(
    stats: dict[str, Any],
    todos: list[dict[str, str]],
    baseline_delta: dict[str, Any] | None,
    missing_workers: int,
    html_abspath: str | None,
    json_abspath: str | None,
) -> list[str]:
    """The compact summary pytest_terminal_summary prints."""
    outcomes = stats["outcomes"]
    failed = outcomes.get("failed", 0) + outcomes.get("error", 0)
    lines = [
        f"{stats['tests']:,} tests in {fmt_seconds(stats['suite_wall_s'])} wall"
        f" ({fmt_seconds(stats['agg_wall_s'])} aggregate, {fmt_seconds(stats['cpu_s'])} CPU)",
        f"DB {stats['db_queries']:,} queries ({fmt_seconds(stats['db_time_s'])},"
        f" {stats['db_share']:.0%} of test time)"
        f" · HTTP {stats['http_calls']:,} calls ({fmt_seconds(stats['http_time_s'])})"
        f" · sleep {fmt_seconds(stats['sleep_s'])}"
        f" · {stats['file_opens']:,} file opens",
    ]
    if baseline_delta is not None:
        by_key = {t["key"]: t for t in baseline_delta["totals"]}
        bits = []
        for key in ("tests", "db_queries", "agg_wall_s"):
            row = by_key[key]
            sign = "+" if row["delta"] >= 0 else "-"
            value = (
                fmt_seconds(abs(row["delta"]))
                if key.endswith("_s")
                else f"{abs(row['delta']):,.0f}"
            )
            pct = f" ({row['pct']:+.0%})" if row["pct"] is not None else ""
            bits.append(f"{row['label']} {sign}{value}{pct}")
        lines.append("vs baseline: " + " · ".join(bits))
        if baseline_delta["baseline_schema"] != SCHEMA_VERSION:
            lines.append(
                f"WARNING: baseline JSON is schema {baseline_delta['baseline_schema']}"
                f", this run writes schema {SCHEMA_VERSION} — the comparison may be off."
            )
    if missing_workers > 0:
        lines.append(
            f"WARNING: {missing_workers} xdist worker shard(s) missing "
            "(crashed worker or non-shared filesystem) — totals are incomplete."
        )
    if failed:
        lines.append(f"{failed} failing test(s) — fix those first")
    for todo in todos[:3]:
        lines.append(f"TODO [{todo['severity']}] {todo['title']}")
    if html_abspath:
        lines.append(f"report: file://{os.path.abspath(html_abspath)}")
    if json_abspath:
        lines.append(f"json:   {os.path.abspath(json_abspath)}")
    return lines


def pytest_terminal_summary(
    terminalreporter: Any, exitstatus: Any, config: Any
) -> None:
    state = getattr(config, "_perf_report_state", None)
    if state is None or state.is_worker or not state.summary_lines:
        return
    terminalreporter.write_sep("=", "perf report")
    for line in state.summary_lines:
        terminalreporter.write_line(line)


def pytest_unconfigure(config: Any) -> None:
    # Safety net if sessionfinish never ran (e.g. usage errors, crashes).
    global _IMPORT_TIMER
    if _IMPORT_TIMER is not None:  # configure never ran (usage error)
        _IMPORT_TIMER.uninstall()
        _IMPORT_TIMER = None
    state = getattr(config, "_perf_report_state", None)
    if state is not None and runtime.STATE is state:
        runtime.STATE = None
        for probe in _PROBES:
            try:
                probe.uninstall()
            except Exception:
                pass
        _PROBES.clear()
        _REFRESH_PROBES.clear()
        if state.owns_shard_dir:
            shutil.rmtree(state.shard_dir, ignore_errors=True)
