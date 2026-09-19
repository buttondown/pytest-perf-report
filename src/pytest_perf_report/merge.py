"""Shard serialization and merging.

Every process that ran tests (the lone process, or each xdist worker) writes
one JSON shard at session finish; the non-worker process merges them into a
single picture. The shard format is versioned so out-of-tree tooling can
consume it.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any

from pytest_perf_report.runtime import (
    MAX_ORIGINS_PER_SHAPE,
    SessionState,
    new_fixture_row,
)

SCHEMA_VERSION = 3
MAX_FILE_ROWS = 500
MAX_WARNING_ROWS = 100
# Per shard, only the heaviest profiled functions travel; merging restores a
# stable global top because heavy functions are heavy everywhere.
MAX_CPU_PROFILE_ROWS = 400
MAX_COLLECT_MODULE_ROWS = 200
MAX_STARTUP_IMPORT_ROWS = 200


def state_to_shard(state: SessionState) -> dict[str, Any]:
    collect_s = (
        max(0.0, state.t_collect_done - state.t_configure)
        if state.t_collect_done
        else 0.0
    )
    return {
        "schema": SCHEMA_VERSION,
        "shard_id": state.shard_id,
        "is_worker": state.is_worker,
        "meta": state.meta,
        "timing": {
            "wall_s": round(state.t_finish - state.t_configure, 6),
            # Interpreter boot + plugin/conftest imports happen before
            # pytest_configure can start a wall clock; process CPU up to that
            # point is the closest portable measure of them.
            "startup_collect_s": round(state.preconfigure_cpu + collect_s, 6),
            "preconfigure_cpu_s": round(state.preconfigure_cpu, 6),
            "collect_s": round(collect_s, 6),
            "collect_modifyitems_s": round(state.collect_modifyitems_s, 6),
            "epoch_configure": state.epoch_configure,
            "epoch_collect_done": state.epoch_collect_done,
            "epoch_first_test": state.epoch_first_test,
            "epoch_last_test": state.epoch_last_test,
            "epoch_finish": state.epoch_finish,
            "first_test_setup_s": round(state.first_test_setup_s, 6),
            "first_test_nodeid": state.first_test_nodeid,
            "peak_rss_bytes": state.peak_rss_bytes,
        },
        "per_test": [rec.as_dict() for rec in state.per_test],
        "shapes": {
            shape: {
                **{k: v for k, v in row.items() if not k.startswith("_")},
                **(
                    {
                        "origins": dict(
                            row["_origins"].most_common(MAX_ORIGINS_PER_SHAPE)
                        )
                    }
                    if row.get("_origins")
                    else {}
                ),
            }
            for shape, row in state.shapes.items()
        },
        "db_vendors": dict(state.db_vendors),
        "db_outside": {
            "queries": state.db_queries_outside,
            "time_s": round(state.db_time_outside, 6),
        },
        "http_hosts": state.http_hosts,
        "raw_http_hosts": sorted(state.raw_http_hosts),
        "net_endpoints": state.net_endpoints,
        "files": {
            "top": state.file_opens.most_common(MAX_FILE_ROWS),
            "total_opens": sum(state.file_opens.values()) + state.file_opens_overflow,
            "distinct": len(state.file_opens),
        },
        "sleep_s": round(state.sleep_total_s, 6),
        "gc_s": round(state.gc_total_s, 6),
        "gc_collections": state.gc_collections,
        "fixtures": state.fixtures,
        "warnings": [
            [list(k), n] for k, n in state.warnings.most_common(MAX_WARNING_ROWS)
        ],
        "io": {"read_bytes": state.io_read_bytes, "write_bytes": state.io_write_bytes},
        "cpu_profile": sorted(
            (
                [key, round(row[0], 6), int(row[1])]
                for key, row in state.cpu_profile_funcs.items()
            ),
            key=lambda r: r[1],
            reverse=True,
        )[:MAX_CPU_PROFILE_ROWS],
        "collect_modules": sorted(
            ([path, round(secs, 6)] for path, secs in state.collect_modules.items()),
            key=lambda r: r[1],
            reverse=True,
        )[:MAX_COLLECT_MODULE_ROWS],
        "startup_imports": sorted(
            (
                [name, round(row[0], 6), round(row[1], 6)]
                for name, row in state.startup_imports.items()
            ),
            key=lambda r: r[1],
            reverse=True,
        )[:MAX_STARTUP_IMPORT_ROWS],
    }


def write_shard(state: SessionState) -> str:
    path = os.path.join(state.shard_dir, f"shard-{state.shard_id}.json")
    with open(path, "w") as f:
        json.dump(state_to_shard(state), f)
    return path


def load_shards(shard_dir: str) -> list[dict[str, Any]]:
    shards = []
    try:
        names = sorted(os.listdir(shard_dir))
    except OSError:
        return []
    for name in names:
        if not (name.startswith("shard-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(shard_dir, name)) as f:
                shards.append(json.load(f))
        except (OSError, ValueError):
            continue
    return shards


def merge_shards(shards: list[dict[str, Any]]) -> dict[str, Any]:
    per_test: list[dict[str, Any]] = []
    shapes: dict[str, dict[str, Any]] = {}
    db_vendors: Counter = Counter()
    db_outside = {"queries": 0, "time_s": 0.0}
    http_hosts: dict[str, dict[str, float]] = {}
    raw_http_hosts: set[str] = set()
    net_endpoints: dict[str, dict[str, Any]] = {}
    files_top: Counter = Counter()
    files_total = 0
    files_distinct = 0
    sleep_s = 0.0
    gc_s = 0.0
    gc_collections = 0
    fixtures: dict[str, dict[str, Any]] = {}
    warnings: Counter = Counter()
    cpu_profile: dict[str, list[float]] = {}
    io = {"read_bytes": 0, "write_bytes": 0}
    meta: dict[str, Any] = {}
    suite = {
        "wall_s": 0.0,
        "startup_collect_s": 0.0,
        "startup_preconfigure_s": 0.0,
        "startup_collection_s": 0.0,
        "collect_modifyitems_s": 0.0,
        "workers": 0,
    }
    collect_modules: dict[str, float] = {}
    startup_imports: dict[str, list[float]] = {}
    # One lane per process for the run timeline; controller first, then
    # workers by id.
    lanes: list[dict[str, Any]] = []

    for shard in shards:
        timing = shard.get("timing", {})
        lanes.append(
            {
                "shard_id": shard.get("shard_id", "?"),
                "is_worker": bool(shard.get("is_worker")),
                "tests": len(shard.get("per_test", [])),
                **timing,
            }
        )
        if shard.get("is_worker"):
            suite["workers"] += 1
        else:
            meta = shard.get("meta") or meta
            # Suite wall is the orchestrating process's clock; workers start
            # later and only ever see less.
            suite["wall_s"] = max(suite["wall_s"], timing.get("wall_s", 0.0))
        # Startup/collection cost is paid per process and is largest in
        # workers (they import the test modules; an xdist controller doesn't),
        # so take the max over every shard. The decomposition travels with the
        # winning shard so the split always describes one real process.
        if timing.get("startup_collect_s", 0.0) >= suite["startup_collect_s"]:
            suite["startup_collect_s"] = timing.get("startup_collect_s", 0.0)
            suite["startup_preconfigure_s"] = timing.get("preconfigure_cpu_s", 0.0)
            suite["startup_collection_s"] = timing.get("collect_s", 0.0)
            suite["collect_modifyitems_s"] = timing.get("collect_modifyitems_s", 0.0)
        if not meta:
            meta = shard.get("meta") or {}

        per_test.extend(shard.get("per_test", []))

        for shape, row in shard.get("shapes", {}).items():
            agg = shapes.get(shape)
            if agg is None:
                agg = dict(row)
                if "origins" in agg:
                    agg["origins"] = dict(agg["origins"])
                shapes[shape] = agg
            else:
                agg["count"] += row.get("count", 0)
                agg["db_time_s"] += row.get("db_time_s", 0.0)
                agg["tests"] += row.get("tests", 0)
                if row.get("max_in_one_test", 0) > agg["max_in_one_test"]:
                    agg["max_in_one_test"] = row["max_in_one_test"]
                    agg["max_test"] = row.get("max_test", "")
                for origin, n in row.get("origins", {}).items():
                    agg.setdefault("origins", {})
                    agg["origins"][origin] = agg["origins"].get(origin, 0) + n

        db_vendors.update(shard.get("db_vendors", {}))
        outside = shard.get("db_outside", {})
        db_outside["queries"] += outside.get("queries", 0)
        db_outside["time_s"] += outside.get("time_s", 0.0)

        for host, row in shard.get("http_hosts", {}).items():
            agg = http_hosts.setdefault(host, {"calls": 0, "time_s": 0.0})
            agg["calls"] += row.get("calls", 0)
            agg["time_s"] += row.get("time_s", 0.0)
        raw_http_hosts.update(shard.get("raw_http_hosts", []))

        for endpoint, row in shard.get("net_endpoints", {}).items():
            agg = net_endpoints.setdefault(
                endpoint,
                {"connects": 0, "time_s": 0.0, "external": row.get("external", False)},
            )
            agg["connects"] += row.get("connects", 0)
            agg["time_s"] += row.get("time_s", 0.0)

        shard_files = shard.get("files", {})
        for path, n in shard_files.get("top", []):
            files_top[path] += n
        files_total += shard_files.get("total_opens", 0)
        files_distinct = max(files_distinct, shard_files.get("distinct", 0))

        sleep_s += shard.get("sleep_s", 0.0)
        gc_s += shard.get("gc_s", 0.0)
        gc_collections += shard.get("gc_collections", 0)

        for name, row in shard.get("fixtures", {}).items():
            agg = fixtures.setdefault(name, new_fixture_row())
            agg["total_s"] += row.get("total_s", 0.0)
            agg["self_s"] += row.get("self_s", 0.0)
            agg["count"] += row.get("count", 0)
            agg["max_s"] = max(agg["max_s"], row.get("max_s", 0.0))
            agg["db_queries"] += row.get("db_queries", 0)
            agg["db_time_s"] += row.get("db_time_s", 0.0)
            agg["db_writes"] += row.get("db_writes", 0)
            if "scope" in row and "scope" not in agg:
                agg["scope"] = row["scope"]
                agg["autouse"] = row.get("autouse", False)

        for key, tottime, calls in shard.get("cpu_profile", []):
            agg_row = cpu_profile.get(key)
            if agg_row is None:
                cpu_profile[key] = [tottime, calls]
            else:
                agg_row[0] += tottime
                agg_row[1] += calls

        # Like startup: every process pays these, so max (not sum) is the
        # honest per-run figure.
        for path, secs in shard.get("collect_modules", []):
            if secs > collect_modules.get(path, 0.0):
                collect_modules[path] = secs
        for name, self_s, cum_s in shard.get("startup_imports", []):
            row = startup_imports.get(name)
            if row is None or self_s > row[0]:
                startup_imports[name] = [self_s, cum_s]

        for key, n in shard.get("warnings", []):
            warnings[tuple(key)] += n

        shard_io = shard.get("io", {})
        io["read_bytes"] += shard_io.get("read_bytes", 0)
        io["write_bytes"] += shard_io.get("write_bytes", 0)

    lanes.sort(key=lambda lane: (lane["is_worker"], lane["shard_id"]))
    return {
        "meta": meta,
        "suite": suite,
        "lanes": lanes,
        "per_test": per_test,
        "shapes": shapes,
        "db_vendors": dict(db_vendors),
        "db_outside": db_outside,
        "http_hosts": http_hosts,
        "raw_http_hosts": raw_http_hosts,
        "net_endpoints": net_endpoints,
        "files": {
            "top": files_top,
            "total_opens": files_total,
            "distinct": files_distinct,
            # Filled by the controller at session finish (one stat per file).
            "sizes": {},
        },
        "sleep_s": sleep_s,
        "gc_s": gc_s,
        "gc_collections": gc_collections,
        "fixtures": fixtures,
        "warnings": warnings,
        "cpu_profile": cpu_profile,
        "collect_modules": collect_modules,
        "startup_imports": startup_imports,
        "io": io,
    }
