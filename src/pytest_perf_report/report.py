"""Self-contained HTML report.

No external assets: every style and script the page needs is inlined, so the
file can be opened from disk, attached to a CI run, or emailed as-is.
"""

from __future__ import annotations

import datetime
import html
from typing import Any

from pytest_perf_report.analyze import N_PLUS_ONE_THRESHOLD, fmt_bytes, fmt_seconds

esc = html.escape

# A small component set (tokens, cards, pills, callouts, proportion bar,
# barchart, legend, trend, table), followed by a clearly-marked block of
# report-specific extensions (sortable headers, timeline lanes, failure
# disclosure, TODO list) built on the same tokens.
CSS = """
:root {
  /* Surfaces */
  --bg: #0f1115; --panel: #181b22; --panel2: #1f232c; --panel3: #20242e;
  --border: #2a2f3a; --chip: #262b35;
  /* Text */
  --fg: #e7eaf0; --muted: #9aa3b2; --faint: #6b7280;
  /* Semantic */
  --accent: #6ea8fe; --warn: #f0a35e; --good: #5fd08a; --bad: #f06e6e;
  /* Categorical chart palette */
  --series-1: #6ea8fe; --series-2: #5fd08a; --series-3: #f0a35e; --series-4: #f06e6e;
  --series-5: #a78bfa; --series-6: #22d3ee; --series-7: #f472b6; --series-8: #a3e635;
}
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1080px; padding: 32px; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
h1 { font-size: 24px; margin: 0 0 4px; font-weight: 650; }
h2 { font-size: 16px; margin: 36px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code { background: var(--chip); padding: 1.5px 6px; border-radius: 5px; font-size: 12.5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
.sub, .note, .footer { color: var(--muted); font-size: 12.5px; }
.subhead { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; margin: 20px 0 10px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.cards { display: grid; grid-template-columns: repeat(7, 1fr); gap: 12px; margin: 20px 0; }
@media (max-width: 920px) { .cards { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); } }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; cursor: help; }
.card .n { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; }
.card .l { color: var(--muted); font-size: 12px; margin-top: 2px; }
.trend { font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.trend.good { color: var(--good); }
.trend.bad { color: var(--bad); }
.trend.flat { color: var(--faint); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 24px; margin: 12px 0 0; }
.legend-item { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); white-space: nowrap; }
.legend-item .swatch { width: 10px; height: 10px; border-radius: 2px; flex: none; }
.legend-item .v { color: var(--fg); font-variant-numeric: tabular-nums; }
.barchart { display: flex; flex-direction: column; gap: 8px; }
.bar-row { display: grid; grid-template-columns: 100px 1fr 78px; align-items: center; gap: 12px; }
.bar-row .bl { color: var(--muted); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-track { height: 18px; background: var(--panel2); border: 1px solid var(--border); border-radius: 5px; overflow: hidden; }
.bar-fill { height: 100%; background: var(--accent); border-radius: 4px; }
.bar-row .bv { text-align: right; white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; font-variant-numeric: tabular-nums; }
.bar { height: 6px; background: var(--accent); border-radius: 3px; display: inline-block; vertical-align: middle; }
.proportion { display: flex; width: 100%; height: 18px; border: 1px solid var(--border);
  border-radius: 999px; overflow: hidden; background: var(--panel2); }
.proportion > span { height: 100%; }
.proportion > span + span { border-left: 1px solid var(--bg); }
table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 9px 14px; border-bottom: 1px solid var(--border); }
th { background: var(--panel2); color: var(--muted); font-weight: 600; font-size: 12px;
  text-transform: uppercase; letter-spacing: 0.03em; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--panel3); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.muted { color: var(--muted); }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11.5px;
  font-weight: 600; background: var(--chip); color: var(--fg); }
.pill.good { background: color-mix(in srgb, var(--good) 18%, transparent); color: var(--good); }
.pill.warn { background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); }
.pill.bad { background: color-mix(in srgb, var(--bad) 18%, transparent); color: var(--bad); }
.pill.neutral { background: var(--chip); color: var(--muted); }
.callout { background: color-mix(in srgb, var(--good) 7%, var(--panel));
  border: 1px solid color-mix(in srgb, var(--good) 38%, var(--border));
  border-radius: 8px; padding: 14px 16px; margin: 16px 0; }
.callout.warn { background: color-mix(in srgb, var(--warn) 7%, var(--panel));
  border-color: color-mix(in srgb, var(--warn) 38%, var(--border)); }
.callout.bad { background: color-mix(in srgb, var(--bad) 7%, var(--panel));
  border-color: color-mix(in srgb, var(--bad) 38%, var(--border)); }
.footer { margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--border); }

/* ---- Report-specific extensions on kit tokens ----------------------------- */
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: var(--fg); }
.tl-row { display: flex; align-items: center; gap: 10px; margin: 5px 0; }
.tl-label { width: 52px; color: var(--muted); font-size: 11px; text-align: right;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; flex-shrink: 0; }
.lane { position: relative; flex: 1; height: 18px; background: var(--panel2);
  border: 1px solid var(--border); border-radius: 5px; overflow: hidden; }
.lane .seg { position: absolute; top: 0; bottom: 0; }
.tl-axis { display: flex; justify-content: space-between; color: var(--faint);
  font-size: 11px; margin: 2px 0 0 62px; font-variant-numeric: tabular-nums; }
details { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; margin: 8px 0; }
details summary { cursor: pointer; padding: 9px 14px; }
details pre { margin: 0; padding: 12px 14px; overflow-x: auto; font-size: 12px; color: var(--bad);
  border-top: 1px solid var(--border); }
ol.todos { padding-left: 0; list-style: none; counter-reset: todo; }
ol.todos li { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px 14px 52px; margin: 10px 0; position: relative; counter-increment: todo; }
ol.todos li::before { content: counter(todo); position: absolute; left: 16px; top: 14px;
  width: 24px; height: 24px; border-radius: 50%; background: var(--panel2); color: var(--muted);
  font-weight: 650; display: flex; align-items: center; justify-content: center; font-size: 13px; }
ol.todos .t { font-weight: 650; margin-right: 8px; }
ol.todos .b { color: var(--muted); font-size: 13px; margin-top: 4px; }
.tabs { display: flex; gap: 4px; margin: 36px 0 0; border-bottom: 1px solid var(--border); }
.tabs button { background: none; border: none; border-bottom: 2px solid transparent; color: var(--muted);
  font: inherit; font-weight: 600; font-size: 13px; padding: 8px 14px; cursor: pointer; }
.tabs button:hover { color: var(--fg); }
.tabs button[aria-selected="true"] { color: var(--fg); border-bottom-color: var(--accent); }
.tab-panel > h2:first-child { margin-top: 20px; border-bottom: none; padding-bottom: 0; }
"""

TAB_JS = """
var tabs = document.querySelectorAll('.tabs button');
function selectTab(name) {
  tabs.forEach(function (b) { b.setAttribute('aria-selected', String(b.dataset.tab === name)); });
  document.querySelectorAll('.tab-panel').forEach(function (p) { p.hidden = p.dataset.tab !== name; });
}
tabs.forEach(function (b) { b.addEventListener('click', function () { selectTab(b.dataset.tab); }); });
if (tabs.length) selectTab(tabs[0].dataset.tab);
"""

SORT_JS = """
document.querySelectorAll('th.sortable').forEach(function (th) {
  th.addEventListener('click', function () {
    var table = th.closest('table'), tbody = table.tBodies[0];
    var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
    var rows = Array.prototype.slice.call(tbody.rows);
    var dir = th.dataset.dir === 'desc' ? 'asc' : 'desc';
    table.querySelectorAll('th.sortable').forEach(function (h) { delete h.dataset.dir; });
    th.dataset.dir = dir;
    rows.sort(function (a, b) {
      var av = parseFloat(a.cells[idx].dataset.v || a.cells[idx].textContent) || 0;
      var bv = parseFloat(b.cells[idx].dataset.v || b.cells[idx].textContent) || 0;
      return dir === 'desc' ? bv - av : av - bv;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  });
});
"""

STACK_COLORS = {
    "DB": "var(--series-1)",
    "HTTP (real)": "var(--series-5)",
    "sleep": "var(--series-4)",
    "GC": "var(--series-3)",
    "other CPU": "var(--series-2)",
    "other wait": "var(--faint)",
}

SEVERITY_PILL = {"high": "bad", "medium": "warn", "low": "neutral"}

# The tests table shows everything up to this cap (announced when it bites);
# a 10k-row sortable table is where browsers start to chug.
MAX_TEST_TABLE_ROWS = 5_000


def _legend(items: list[tuple[str, str, str]]) -> str:
    """Kit legend: (color, label, value) triples; value may be empty."""
    return (
        '<div class="legend">'
        + "".join(
            f'<span class="legend-item"><span class="swatch" style="background:{color}">'
            f"</span>{esc(label)}"
            + (f' <span class="v">{esc(value)}</span>' if value else "")
            + "</span>"
            for color, label, value in items
        )
        + "</div>"
    )


def _card(value: str, label: str, tip: str = "") -> str:
    title = f' title="{esc(tip)}"' if tip else ""
    return (
        f'<div class="card"{title}><div class="n">{value}</div>'
        f'<div class="l">{esc(label)}</div></div>'
    )


def _num(raw: Any, formatted: str) -> str:
    """Numeric cell; data-v is the sort key SORT_JS reads."""
    return f'<td class="num" data-v="{raw}">{formatted}</td>'


def _bar(fraction: float, max_px: int = 120) -> str:
    width = max(2, int(round(min(1.0, fraction) * max_px)))
    return f'<span class="bar" style="width:{width}px"></span>'


def _outcome_pill(outcome: str) -> str:
    cls = {
        "passed": "good",
        "failed": "bad",
        "error": "bad",
        "skipped": "neutral",
        "xfailed": "neutral",
        "xpassed": "warn",
    }.get(outcome, "neutral")
    return f'<span class="pill {cls}">{esc(outcome)}</span>'


def _stack_bar(decomposition: list[tuple[str, float]]) -> str:
    """Kit proportion bar — the decomposition is a pie unrolled into a line."""
    total = sum(v for _, v in decomposition) or 1.0
    segments = "".join(
        f'<span style="width:{v / total * 100:.2f}%;background:{STACK_COLORS[label]}" '
        f'title="{esc(label)}: {fmt_seconds(v)} ({v / total:.1%})"></span>'
        for label, v in decomposition
        if v > 0
    )
    legend = _legend(
        [
            (STACK_COLORS[label], label, f"{fmt_seconds(v)} · {v / total:.0%}")
            for label, v in decomposition
            if v > 0
        ]
    )
    return (
        f'<div class="proportion">{segments}</div>{legend}'
        '<div class="note" style="margin-top:8px">Approximate: DB, real-socket HTTP, '
        "and sleep are measured waits; mocked HTTP is CPU and counts under “other "
        "CPU”; GC is part of CPU; “other wait” is whatever wall time remains "
        "unattributed (subprocesses, locks, disk).</div>"
    )


TIMELINE_COLORS = {
    "startup (imports, est.)": "var(--series-5)",
    "collection": "var(--series-1)",
    "waiting / scheduling": "var(--chip)",
    "session setup (first test)": "var(--series-3)",
    "tests": "var(--series-2)",
    "teardown + reporting": "var(--faint)",
    "orchestrating workers": "var(--chip)",
}


def _timeline(lanes: list[dict[str, Any]]) -> str:
    """One lane per process: boot → collect → wait → session setup → tests → finish."""
    valid = [lane for lane in lanes if lane.get("epoch_configure")]
    if not valid:
        return ""
    start = min(
        lane["epoch_configure"] - lane.get("preconfigure_cpu_s", 0.0) for lane in valid
    )
    end = max(lane.get("epoch_finish") or lane["epoch_configure"] for lane in valid)
    total = max(end - start, 0.001)

    def seg(t0: float, t1: float, phase: str) -> str:
        if t1 <= t0:
            return ""
        left = (t0 - start) / total * 100
        width = max((t1 - t0) / total * 100, 0.15)
        return (
            f'<div class="seg" style="left:{left:.2f}%;width:{width:.2f}%;'
            f'background:{TIMELINE_COLORS[phase]}" '
            f'title="{esc(phase)}: {fmt_seconds(t1 - t0)}"></div>'
        )

    used_phases: dict[str, bool] = {}

    def lane_html(lane: dict[str, Any]) -> str:
        boot0 = lane["epoch_configure"] - lane.get("preconfigure_cpu_s", 0.0)
        collect_done = lane.get("epoch_collect_done") or lane["epoch_configure"]
        first = lane.get("epoch_first_test") or 0.0
        last = lane.get("epoch_last_test") or 0.0
        finish = lane.get("epoch_finish") or end
        parts = [
            ("startup (imports, est.)", boot0, lane["epoch_configure"]),
            ("collection", lane["epoch_configure"], collect_done),
        ]
        if first and last:
            setup_end = min(first + lane.get("first_test_setup_s", 0.0), last)
            parts += [
                ("waiting / scheduling", collect_done, first),
                ("session setup (first test)", first, setup_end),
                ("tests", setup_end, last),
                ("teardown + reporting", last, finish),
            ]
        else:
            parts.append(("orchestrating workers", collect_done, finish))
        segments = []
        for phase, t0, t1 in parts:
            html_seg = seg(t0, t1, phase)
            if html_seg:
                used_phases[phase] = True
                segments.append(html_seg)
        return (
            f'<div class="tl-row"><div class="tl-label">{esc(lane["shard_id"])}</div>'
            f'<div class="lane">{"".join(segments)}</div></div>'
        )

    rows = "".join(lane_html(lane) for lane in valid)
    legend = _legend(
        [
            (TIMELINE_COLORS[phase], phase, "")
            for phase in TIMELINE_COLORS
            if used_phases.get(phase)
        ]
    )
    return (
        f'{rows}<div class="tl-axis"><span>0s</span><span>{fmt_seconds(total)}</span></div>'
        f"{legend}"
        '<div class="note" style="margin-top:8px">One lane per process. The startup '
        "segment is estimated from pre-pytest CPU (interpreter boot and imports "
        "happen before any plugin clock exists); “session setup” is the first "
        "test's setup phase, which pytest charges with all session-scoped "
        "fixtures.</div>"
    )


def _histogram(walls: list[float]) -> str:
    if not walls:
        return ""
    # Log buckets from 1ms up; everything slower than the last edge pools there.
    edges = [0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0]
    labels = [
        "≤1ms",
        "≤3ms",
        "≤10ms",
        "≤30ms",
        "≤0.1s",
        "≤0.3s",
        "≤1s",
        "≤3s",
        "≤10s",
        ">10s",
    ]
    counts = [0] * len(labels)
    for w in walls:
        for i, edge in enumerate(edges):
            if w <= edge:
                counts[i] += 1
                break
        else:
            counts[len(edges)] += 1
    peak = max(counts) or 1
    # Kit horizontal bar chart, one row per duration bucket.
    rows = "".join(
        f'<div class="bar-row"><div class="bl">{esc(label)}</div>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{c / peak * 100:.1f}%"></div></div>'
        f'<div class="bv">{c:,}</div></div>'
        for c, label in zip(counts, labels)
    )
    return f'<div class="barchart">{rows}</div>'


def _table(headers: list[tuple[str, bool]], rows: list[str], note: str = "") -> str:
    head = "".join(
        f'<th class="num sortable">{esc(h)}</th>' if numeric else f"<th>{esc(h)}</th>"
        for h, numeric in headers
    )
    note_html = f'<div class="note" style="margin:6px 0 0">{note}</div>' if note else ""
    return (
        f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        + note_html
    )


def _delta_cell(delta: float, pct: float | None, is_time: bool) -> str:
    """Kit trend readout: ▲ more cost is bad, ▼ less is good, — flat."""
    if delta == 0:
        return '<td class="num trend flat" data-v="0">—</td>'
    arrow, cls = ("▲", "bad") if delta > 0 else ("▼", "good")
    value = fmt_seconds(abs(delta)) if is_time else f"{abs(delta):,.0f}"
    pct_txt = f" ({pct:+.0%})" if pct is not None else ""
    return f'<td class="num trend {cls}" data-v="{delta}">{arrow} {value}{esc(pct_txt)}</td>'


def _baseline_section(delta: dict[str, Any]) -> str:
    base_meta = delta.get("baseline_meta", {})
    base_label = base_meta.get("started") or "previous run"
    rows = "".join(
        f"<tr><td>{esc(row['label'])}</td>"
        f'<td class="num" data-v="{row["before"]}">{esc(row["before_fmt"])}</td>'
        f'<td class="num" data-v="{row["after"]}">{esc(row["after_fmt"])}</td>'
        + _delta_cell(row["delta"], row["pct"], row["key"].endswith("_s"))
        + "</tr>"
        for row in delta["totals"]
    )
    html = (
        f"<h2>Vs baseline ({esc(base_label)})</h2>"
        f'<div class="sub">{delta["common_tests"]:,} tests present in both runs; '
        "per-test comparisons use only those.</div>"
        + _table(
            [("metric", False), ("baseline", True), ("this run", True), ("Δ", True)],
            [rows],
        )
    )
    if delta["shape_changes"]:
        shape_rows = [
            f"<tr><td><code>{esc(c['shape'][:160])}</code>"
            + (
                ' <span class="pill warn">new</span>'
                if not c["before"]
                else (' <span class="pill good">gone</span>' if not c["after"] else "")
            )
            + "</td>"
            + _num(c["before"], f"{c['before']:,}")
            + _num(c["after"], f"{c['after']:,}")
            + _delta_cell(c["delta"], None, False)
            + "</tr>"
            for c in delta["shape_changes"]
        ]
        html += '<p class="subhead">Biggest query-shape changes</p>' + _table(
            [
                ("query shape", False),
                ("baseline", True),
                ("this run", True),
                ("Δ", True),
            ],
            shape_rows,
        )
    if delta["test_regressions"]:
        reg_rows = [
            f"<tr><td><code>{esc(r['nodeid'])}</code></td>"
            + _num(r["before"], fmt_seconds(r["before"]))
            + _num(r["after"], fmt_seconds(r["after"]))
            + _delta_cell(
                r["delta"], r["delta"] / r["before"] if r["before"] else None, True
            )
            + "</tr>"
            for r in delta["test_regressions"]
        ]
        html += '<p class="subhead">Slowest test regressions</p>' + _table(
            [("test", False), ("baseline", True), ("this run", True), ("Δ", True)],
            reg_rows,
        )
    return html


def render(
    merged: dict[str, Any],
    stats: dict[str, Any],
    todos: list[dict[str, str]],
    baseline_delta: dict[str, Any] | None = None,
) -> str:
    per_test = merged["per_test"]
    meta = merged["meta"]
    outcomes = stats["outcomes"]
    failed_n = outcomes.get("failed", 0) + outcomes.get("error", 0)

    # --- header / cards ---
    outcome_bits = [f"{outcomes.get('passed', 0):,} passed"]
    if failed_n:
        outcome_bits.append(f"{failed_n:,} failed")
    if outcomes.get("skipped"):
        outcome_bits.append(f"{outcomes['skipped']:,} skipped")
    if outcomes.get("xfailed"):
        outcome_bits.append(f"{outcomes['xfailed']:,} xfailed")
    if outcomes.get("xpassed"):
        outcome_bits.append(f"{outcomes['xpassed']:,} xpassed")

    workers = stats["workers"]
    runner = f"{workers} xdist workers" if workers else "single process"
    sub_parts = [
        meta.get("started", ""),
        f"{stats['tests']:,} tests",
        ", ".join(outcome_bits),
        runner,
        f"Python {meta.get('python', '?')}",
        f"pytest {meta.get('pytest', '?')}",
    ]
    subtitle = " · ".join(p for p in sub_parts if p)

    cards = [
        _card(
            fmt_seconds(stats["suite_wall_s"]),
            "suite wall time",
            "End-to-end clock for the whole run, including per-process startup "
            "and collection (paid once per xdist worker).",
        ),
        _card(
            fmt_seconds(stats["agg_wall_s"]),
            "aggregate test time",
            "Sum of per-test wall time across all workers — total work done, "
            "ignoring parallelism.",
        ),
        _card(
            fmt_seconds(stats["cpu_s"]),
            "CPU time",
            "Process CPU consumed inside tests; the rest of test time is waiting "
            "(DB, network, disk, locks).",
        ),
        _card(
            fmt_seconds(stats["p50"]),
            "median test",
            "Half of all tests finish within this.",
        ),
        _card(
            fmt_seconds(stats["p99"]),
            "p99 test",
            "99% of tests finish within this.",
        ),
        _card(
            fmt_seconds(stats["startup_collect_s"]),
            "startup + collect",
            "Interpreter boot, imports, and collection before the first test — "
            "paid on every run, even single-test -k runs."
            + (
                f" ≈{fmt_seconds(stats['startup_preconfigure_s'])} pre-pytest imports"
                f" + {fmt_seconds(stats['startup_collection_s'])} collection;"
                " see the Startup tab."
                if (stats["startup_preconfigure_s"] or stats["startup_collection_s"])
                else ""
            ),
        ),
    ]
    # With a single worker "parallel efficiency" would just restate startup
    # overhead in a confusing costume; only meaningful when work is fanned out.
    if stats["parallel_efficiency"] is not None and workers >= 2:
        cards.append(
            _card(
                f"{stats['parallel_efficiency']:.0%}",
                "parallel efficiency",
                "Aggregate test time ÷ (suite wall × workers): how busy the "
                "workers were, net of startup and scheduling gaps.",
            )
        )

    db_vendor = ", ".join(sorted(merged["db_vendors"])) or None
    cards += [
        _card(
            f"{stats['db_queries']:,}",
            f"DB queries ({db_vendor})" if db_vendor else "DB queries",
            "Statements observed by the ORM-level probes during tests "
            "(plus any outside-of-test work, noted in the DB tab).",
        ),
        _card(
            fmt_seconds(stats["db_time_s"]),
            "in-DB time",
            "Wall time spent waiting on the database inside tests.",
        ),
        _card(
            f"{stats['db_share']:.1%}",
            "DB share of test time",
            "In-DB time as a share of aggregate test time.",
        ),
        _card(
            f"{stats['http_calls']:,}",
            "HTTP calls",
            "HTTP client calls, including ones answered by transport-level "
            "mocks (responses, respx) that never hit the network.",
        ),
        _card(
            fmt_seconds(stats["http_time_s"]),
            "HTTP time",
            "Wall time inside HTTP client calls; mocked calls cost CPU, "
            "not network wait.",
        ),
        _card(
            fmt_seconds(stats["sleep_s"]),
            "time.sleep()",
            "Total time.sleep() during tests — pure dead time.",
        ),
        _card(
            f"{stats['file_opens']:,}",
            "file opens",
            "open()/io.open() calls during tests (module imports not included).",
        ),
    ]
    io = merged["io"]
    if io["read_bytes"] or io["write_bytes"]:
        cards.append(
            _card(
                f"{fmt_bytes(io['read_bytes'])} / {fmt_bytes(io['write_bytes'])}",
                "disk read / written",
                "Real disk bytes for this process, from /proc/self/io (Linux only).",
            )
        )
    if stats.get("peak_rss_bytes"):
        cards.append(
            _card(
                fmt_bytes(stats["peak_rss_bytes"]),
                "peak RSS / process",
                "Memory high-water mark; it never comes back down, so one hungry "
                "test sets the floor for the whole worker.",
            )
        )

    # --- callout ---
    external = [(e, r) for e, r in merged["net_endpoints"].items() if r["external"]]
    if failed_n:
        callout_cls, callout = (
            "bad",
            (
                f"<strong>{failed_n} test{'s' if failed_n != 1 else ''} failing.</strong> "
                "Performance notes below still apply, but green comes first."
            ),
        )
    elif external:
        callout_cls, callout = (
            "warn",
            (
                f"<strong>All tests passed, but {len(external)} external network endpoint"
                f"{'s were' if len(external) != 1 else ' was'} contacted.</strong> "
                "The suite is not hermetic — see the network section and TODO #1–2."
            ),
        )
    else:
        top_component = max(stats["decomposition"], key=lambda kv: kv[1])
        callout_cls, callout = (
            "",
            (
                f"<strong>All tests passed.</strong> Largest time component: "
                f"{esc(top_component[0])} at {fmt_seconds(top_component[1])} "
                f"({top_component[1] / (stats['agg_wall_s'] or 1):.0%} of test time)."
            ),
        )

    # --- failures ---
    failures_html = ""
    failures = [t for t in per_test if t["outcome"] in ("failed", "error")]
    if failures:
        items = "".join(
            f"<details><summary>{_outcome_pill(t['outcome'])} "
            f"<code>{esc(t['nodeid'])}</code> · {fmt_seconds(t['wall_s'])}</summary>"
            f"<pre>{esc(t.get('failure') or '(no captured output)')}</pre></details>"
            for t in failures[:50]
        )
        more = (
            f'<div class="note">…and {len(failures) - 50} more.</div>'
            if len(failures) > 50
            else ""
        )
        failures_html = f"<h2>Failures</h2>{items}{more}"

    # --- slowest / fastest ---
    by_wall_asc = sorted(per_test, key=lambda t: t["wall_s"])
    sorted_walls = [t["wall_s"] for t in by_wall_asc]
    max_wall = (sorted_walls[-1] if sorted_walls else 1) or 1
    # Each process's first test gets billed for all session-scoped fixture
    # setup; flag it so it doesn't read as a genuinely slow test.
    first_test_nodeids = {
        lane.get("first_test_nodeid")
        for lane in merged.get("lanes", [])
        if lane.get("first_test_nodeid")
    }

    def test_row(t: dict[str, Any]) -> str:
        session_pill = ""
        if t["nodeid"] in first_test_nodeids and t["setup_s"] > 0.5 * (
            t["wall_s"] or 1
        ):
            session_pill = (
                ' <span class="pill neutral" title="First test in its process: '
                "pytest charges all session-scoped fixture setup (DB creation, "
                "cache warming) to this test's setup phase.\">includes session setup</span>"
            )
        return (
            f"<tr><td>{_bar(t['wall_s'] / max_wall)} <code>{esc(t['nodeid'])}</code> "
            f"{_outcome_pill(t['outcome']) if t['outcome'] != 'passed' else ''}"
            f"{session_pill}</td>"
            + _num(t["wall_s"], fmt_seconds(t["wall_s"]))
            + _num(t["setup_s"], fmt_seconds(t["setup_s"]))
            + _num(t["call_s"], fmt_seconds(t["call_s"]))
            + _num(t["cpu_s"], fmt_seconds(t["cpu_s"]))
            + _num(t["db_queries"], f"{t['db_queries']:,}")
            + _num(t["db_time_s"], fmt_seconds(t["db_time_s"]))
            + _num(t["http_calls"], f"{t['http_calls']:,}")
            + "</tr>"
        )

    test_headers = [
        ("test", False),
        ("wall", True),
        ("setup", True),
        ("call", True),
        ("cpu", True),
        ("queries", True),
        ("db", True),
        ("http", True),
    ]
    # One table, every test, slowest first (sortable by any column). Beyond
    # the cap the page gets unwieldy; the cut is announced, never silent.
    all_tests = by_wall_asc[::-1][:MAX_TEST_TABLE_ROWS]
    tests_note = (
        "All tests, slowest first. The fastest tests put a floor under per-test "
        "overhead — anything a slow test spends beyond its own work shows up "
        "against that baseline."
    )
    if len(by_wall_asc) > MAX_TEST_TABLE_ROWS:
        tests_note = (
            f"Showing the {MAX_TEST_TABLE_ROWS:,} slowest of {len(by_wall_asc):,} "
            f"tests (the rest are in --perf-report-json). " + tests_note
        )
    tests_table = _table(
        test_headers, [test_row(t) for t in all_tests], note=tests_note
    )

    # --- fixtures ---
    def fixture_label(name: str, row: dict[str, Any]) -> str:
        pills = ""
        if row.get("autouse"):
            pills += ' <span class="pill warn" title="Runs for every test in scope">autouse</span>'
        scope = row.get("scope")
        if scope and scope != "function":
            pills += f' <span class="pill neutral">{esc(scope)}</span>'
        return f"<code>{esc(name)}</code>{pills}"

    def fixture_queries_cell(row: dict[str, Any]) -> str:
        queries = int(row.get("db_queries", 0))
        writes = int(row.get("db_writes", 0))
        text = f"{queries:,}" + (f" ({writes:,}w)" if writes else "")
        return _num(queries, text if queries else "—")

    fixtures_html = ""
    fixtures = sorted(
        merged["fixtures"].items(), key=lambda kv: kv[1]["total_s"], reverse=True
    )[:20]
    if fixtures:
        max_total = fixtures[0][1]["total_s"] or 1
        rows = [
            f"<tr><td>{_bar(row['total_s'] / max_total)} {fixture_label(name, row)}</td>"
            + _num(row["total_s"], fmt_seconds(row["total_s"]))
            + _num(row.get("self_s", 0.0), fmt_seconds(row.get("self_s", 0.0)))
            + _num(row["count"], f"{int(row['count']):,}")
            + _num(
                row["total_s"] / (row["count"] or 1),
                fmt_seconds(row["total_s"] / (row["count"] or 1)),
            )
            + _num(row["max_s"], fmt_seconds(row["max_s"]))
            + fixture_queries_cell(row)
            + "</tr>"
            for name, row in fixtures
        ]
        fixtures_html = "<h2>Costliest fixtures (setup time)</h2>" + _table(
            [
                ("fixture", False),
                ("total", True),
                ("self", True),
                ("setups", True),
                ("mean", True),
                ("max", True),
                ("queries", True),
            ],
            rows,
            note="“Total” includes fixtures set up on demand from inside the body "
            "(request.getfixturevalue); “self” subtracts them — read self as where "
            "the time actually lives. “Queries” counts DB statements issued during "
            "the fixture's own setup (w = writes).",
        )

    # --- per-test taxes: autouse function-scoped fixtures ---
    taxes_html = ""
    taxes = sorted(
        (
            (name, row)
            for name, row in merged["fixtures"].items()
            if row.get("autouse") and row.get("scope") == "function"
        ),
        key=lambda kv: kv[1].get("self_s", 0.0),
        reverse=True,
    )[:15]
    if taxes:
        tax_total = sum(row.get("self_s", 0.0) for _, row in taxes)
        rows = [
            f"<tr><td><code>{esc(name)}</code></td>"
            + _num(row.get("self_s", 0.0), fmt_seconds(row.get("self_s", 0.0)))
            + _num(row["count"], f"{int(row['count']):,}")
            + _num(
                row.get("self_s", 0.0) / (row["count"] or 1) * 1000,
                f"{row.get('self_s', 0.0) / (row['count'] or 1) * 1000:.2f}ms",
            )
            + fixture_queries_cell(row)
            + "</tr>"
            for name, row in taxes
        ]
        taxes_html = (
            "<h2>Per-test taxes (autouse fixtures)</h2>"
            f'<div class="sub">Function-scoped autouse fixtures run for every test '
            f"in their scope whether needed or not — {fmt_seconds(tax_total)} of "
            "self time in this run.</div>"
            + _table(
                [
                    ("fixture", False),
                    ("self total", True),
                    ("runs", True),
                    ("per run", True),
                    ("queries", True),
                ],
                rows,
            )
        )

    # --- query shapes ---
    db_html = ""
    if merged["shapes"]:
        total_q = sum(r["count"] for r in merged["shapes"].values()) or 1

        def shape_origin(row: dict[str, Any]) -> str:
            origins = row.get("origins")
            if origins:
                top = max(origins.items(), key=lambda kv: kv[1])
                note = f"↳ {esc(top[0])} ({top[1]:,}×)"
            elif row.get("origin_first") and row["origin_first"] != "?":
                note = f"↳ first issued from {esc(row['origin_first'])}"
            else:
                note = ""
            # Name the test where this shape repeated the most — the place to
            # open when chasing an N+1.
            if row.get("max_test") and row.get("max_in_one_test", 0) >= 10:
                if note:
                    note += " · "
                note += (
                    f"peak {row['max_in_one_test']:,}× in "
                    f"<code>{esc(row['max_test'])}</code>"
                )
            return f'<div class="note">{note}</div>' if note else ""

        def shape_rows(items: list[tuple[str, dict[str, Any]]]) -> list[str]:
            return [
                # The title tooltip carries a concrete example statement.
                f'<tr><td><code title="{esc(row.get("example", ""))}">{esc(shape[:160])}</code>'
                f"{shape_origin(row)}</td>"
                + _num(row["count"], f"{row['count']:,}")
                + _num(row["count"] / total_q, f"{row['count'] / total_q:.1%}")
                + _num(row["db_time_s"], fmt_seconds(row["db_time_s"]))
                + _num(row["tests"], f"{row['tests']:,}")
                + _num(row["max_in_one_test"], f"{row['max_in_one_test']:,}")
                + "</tr>"
                for shape, row in items
            ]

        shape_headers = [
            ("query shape", False),
            ("count", True),
            ("% of queries", True),
            ("db time", True),
            ("tests", True),
            ("max / test", True),
        ]
        by_count = sorted(
            merged["shapes"].items(), key=lambda kv: kv[1]["count"], reverse=True
        )[:20]
        by_time = sorted(
            merged["shapes"].items(), key=lambda kv: kv[1]["db_time_s"], reverse=True
        )[:20]
        n_plus_one = sorted(
            (
                (s, r)
                for s, r in merged["shapes"].items()
                if r.get("max_in_one_test", 0) >= N_PLUS_ONE_THRESHOLD
            ),
            key=lambda kv: kv[1]["max_in_one_test"],
            reverse=True,
        )[:15]
        db_html = (
            "<h2>Top query shapes by count</h2>"
            + f'<div class="sub">{len(merged["shapes"]):,} distinct shapes · '
            + f"{stats['db_queries']:,} queries total"
            + (
                f" · {merged['db_outside']['queries']:,} ran outside tests "
                f"(migrations/fixtures/teardown, {fmt_seconds(stats['db_time_outside_s'])})"
                if merged["db_outside"]["queries"]
                else ""
            )
            + "</div>"
            + _table(shape_headers, shape_rows(by_count))
            + "<h2>Top query shapes by total DB time</h2>"
            + _table(shape_headers, shape_rows(by_time))
        )
        if n_plus_one:
            db_html += (
                f"<h2>N+1 suspects (same shape ≥{N_PLUS_ONE_THRESHOLD}× within one test)</h2>"
                + _table(shape_headers, shape_rows(n_plus_one))
            )

    # --- http / network ---
    http_html = ""
    if merged["http_hosts"] or merged["net_endpoints"]:
        real_pill = '<span class="pill warn">real socket</span>'
        mocked_pill = '<span class="pill good">mocked / in-process</span>'
        rows = [
            f"<tr><td><code>{esc(host)}</code></td>"
            + _num(row["calls"], f"{int(row['calls']):,}")
            + _num(row["time_s"], fmt_seconds(row["time_s"]))
            + _num(
                row["time_s"] / (row["calls"] or 1),
                fmt_seconds(row["time_s"] / (row["calls"] or 1)),
            )
            + f"<td>{real_pill if host in merged['raw_http_hosts'] else mocked_pill}</td></tr>"
            for host, row in sorted(
                merged["http_hosts"].items(),
                key=lambda kv: kv[1]["time_s"],
                reverse=True,
            )[:25]
        ]
        http_html = "<h2>HTTP calls by host</h2>" + (
            _table(
                [
                    ("host", False),
                    ("calls", True),
                    ("time", True),
                    ("mean", True),
                    ("transport", False),
                ],
                rows,
                note="“Mocked / in-process” means no request from this host ever reached "
                "the raw HTTP layer — it was answered by a transport-level mock.",
            )
            if rows
            else '<div class="note">No HTTP client calls observed.</div>'
        )
        external_pill = '<span class="pill bad">external</span>'
        loopback_pill = '<span class="pill good">loopback</span>'
        net_rows = [
            f"<tr><td><code>{esc(endpoint)}</code></td>"
            + _num(row["connects"], f"{int(row['connects']):,}")
            + _num(row["time_s"], fmt_seconds(row["time_s"]))
            + f"<td>{external_pill if row['external'] else loopback_pill}</td></tr>"
            for endpoint, row in sorted(
                merged["net_endpoints"].items(),
                key=lambda kv: (not kv[1]["external"], -kv[1]["connects"]),
            )[:25]
        ]
        if net_rows:
            http_html += "<h2>Socket connections</h2>" + _table(
                [
                    ("endpoint", False),
                    ("connects", True),
                    ("connect time", True),
                    ("kind", False),
                ],
                net_rows,
                note="Python-level sockets only; database drivers that connect in C "
                "(e.g. psycopg) don't appear here — their queries are measured above.",
            )

    # --- cpu profile (opt-in via --perf-report-cpu-profile) ---
    cpu_html = ""
    cpu_profile = merged.get("cpu_profile") or {}
    if cpu_profile:
        top_funcs = sorted(cpu_profile.items(), key=lambda kv: kv[1][0], reverse=True)[
            :25
        ]
        total_profiled = sum(row[0] for row in cpu_profile.values()) or 1.0
        rows = [
            f"<tr><td><code>{esc(key)}</code></td>"
            + _num(row[0], fmt_seconds(row[0]))
            + _num(row[0] / total_profiled, f"{row[0] / total_profiled:.1%}")
            + _num(row[1], f"{int(row[1]):,}")
            + "</tr>"
            for key, row in top_funcs
        ]
        cpu_html = "<h2>Where the CPU went (cProfile)</h2>" + _table(
            [
                ("function", False),
                ("self time", True),
                ("% of profiled", True),
                ("calls", True),
            ],
            rows,
            note="Function self-time aggregated across every test "
            "(--perf-report-cpu-profile). Profiling overhead inflates absolute "
            "times; the ranking and shares are what to trust.",
        )

    # --- files ---
    files_html = ""
    top_files = merged["files"]["top"].most_common(15)
    if top_files:
        max_n = top_files[0][1] or 1
        file_sizes = merged["files"].get("sizes", {})

        def reread_cell(path: str, n: int) -> str:
            size = file_sizes.get(path)
            if not size:
                return '<td class="num" data-v="0">—</td>'
            return _num(n * size, fmt_bytes(n * size))

        rows = [
            f"<tr><td>{_bar(n / max_n)} <code>{esc(path)}</code></td>"
            + _num(n, f"{n:,}")
            + reread_cell(path, n)
            + "</tr>"
            for path, n in top_files
        ]
        files_html = "<h2>Most-opened files</h2>" + _table(
            [("path", False), ("opens", True), ("≈ re-read", True)],
            rows,
            note=f"{merged['files']['total_opens']:,} opens of "
            f"{merged['files']['distinct']:,} distinct files via open()/io.open() "
            "(module imports not included). “≈ re-read” is opens × current file "
            "size — a volume estimate for whether caching is worth it.",
        )

    # --- startup & collection ---
    startup_html = ""
    pre_s = stats.get("startup_preconfigure_s", 0.0)
    collection_s = stats.get("startup_collection_s", 0.0)
    if pre_s or collection_s:
        startup_html += (
            f'<div class="sub">{fmt_seconds(stats["startup_collect_s"])} before the '
            f"first test: ≈{fmt_seconds(pre_s)} of pre-pytest work (interpreter "
            "boot, plugin and conftest imports, estimated from process CPU) + "
            f"{fmt_seconds(collection_s)} of collection. Paid per process — every "
            "xdist worker re-pays it.</div>"
        )
    collect_modules = sorted(
        merged.get("collect_modules", {}).items(), key=lambda kv: kv[1], reverse=True
    )[:15]
    if collect_modules:
        max_collect = collect_modules[0][1] or 1
        rows = [
            f"<tr><td>{_bar(secs / max_collect)} <code>{esc(path)}</code></td>"
            + _num(secs, fmt_seconds(secs))
            + "</tr>"
            for path, secs in collect_modules
        ]
        startup_html += "<h2>Slowest test modules to collect</h2>" + _table(
            [("module", False), ("collect time", True)],
            rows,
            note="Collecting a module imports it, so module-scope work (globs, "
            "file reads, building parametrize lists) lands here — and runs on "
            "every collection, even -k runs that select none of its tests.",
        )
    modifyitems_s = stats.get("collect_modifyitems_s", 0.0)
    if modifyitems_s >= 0.05:
        startup_html += (
            "<h2>Collection hooks</h2>"
            f'<div class="sub">pytest_collection_modifyitems implementations '
            f"(all plugins and conftests together) took "
            f"{fmt_seconds(modifyitems_s)}. Hooks that inspect every collected "
            "item — source reads, marker walks — hide in this number.</div>"
        )
    startup_imports = sorted(
        merged.get("startup_imports", {}).items(),
        key=lambda kv: kv[1][0],
        reverse=True,
    )[:15]
    if startup_imports:
        max_self = startup_imports[0][1][0] or 1
        rows = [
            f"<tr><td>{_bar(row[0] / max_self)} <code>{esc(name)}</code></td>"
            + _num(row[0], fmt_seconds(row[0]))
            + _num(row[1], fmt_seconds(row[1]))
            + "</tr>"
            for name, row in startup_imports
        ]
        startup_html += "<h2>Heaviest startup imports</h2>" + _table(
            [("module", False), ("self", True), ("cumulative", True)],
            rows,
            note="Imports executed while pytest loaded plugins and conftests "
            "(before collection) — settings modules, app registries, and "
            "everything they pull in. “Cumulative” includes nested imports; "
            "“self” is the module's own body.",
        )

    # --- warnings ---
    warnings_html = ""
    top_warnings = merged["warnings"].most_common(15)
    if top_warnings:
        rows = [
            f"<tr><td><code>{esc(cat)}</code> {esc(msg)}</td>"
            + _num(n, f"{n:,}")
            + "</tr>"
            for (cat, msg), n in top_warnings
        ]
        warnings_html = "<h2>Warnings</h2>" + _table(
            [(f"warning ({stats['warnings_total']:,} total)", False), ("count", True)],
            rows,
        )

    # --- todos ---
    todo_items = "".join(
        f'<li><span class="pill {SEVERITY_PILL[t["severity"]]}">{t["severity"]}</span> '
        f'<span class="t">{esc(t["title"])}</span>'
        f'<div class="b">{esc(t["body"])}</div></li>'
        for t in todos
    )

    timeline = _timeline(merged.get("lanes", []))
    timeline_html = f"<h2>Run timeline</h2>\n{timeline}" if timeline else ""
    baseline_html = _baseline_section(baseline_delta) if baseline_delta else ""

    missing_html = ""
    if meta.get("missing_workers"):
        missing_html = (
            f'<div class="callout bad"><strong>{meta["missing_workers"]} xdist worker '
            "shard(s) missing.</strong> A worker crashed or doesn't share a filesystem "
            "with the controller — every number below under-reports.</div>"
        )

    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    command = meta.get("args", "")

    tests_panel = f"""
<h2>Test duration distribution</h2>
{_histogram(sorted_walls)}
<div class="note" style="margin-top:14px">p50 {fmt_seconds(stats["p50"])} · p90 {fmt_seconds(stats["p90"])} · p99 {fmt_seconds(stats["p99"])} · mean {fmt_seconds(stats["mean"])}</div>
{failures_html}
<h2>Tests</h2>
{tests_table}
"""
    # (label, key, panel html) — a tab only exists when it has content.
    panels = [
        ("Tests", "tests", tests_panel),
        ("Startup", "startup", startup_html),
        ("Fixtures", "fixtures", fixtures_html + taxes_html),
        ("DB", "db", db_html),
        ("CPU", "cpu", cpu_html),
        ("HTTP", "http", http_html),
        ("Files", "files", files_html),
        ("Warnings", "warnings", warnings_html),
    ]
    panels = [(label, key, body) for label, key, body in panels if body.strip()]
    tab_buttons = "".join(
        f'<button data-tab="{key}" aria-selected="false">{esc(label)}</button>'
        for label, key, _ in panels
    )
    tab_panels = "".join(
        f'<section class="tab-panel" data-tab="{key}" hidden>{body}</section>'
        for _, key, body in panels
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test suite performance report — {esc(meta.get("project", ""))}</title>
<style>{CSS}</style></head><body>
<h1>Test suite performance report</h1>
<div class="sub">{esc(meta.get("project", ""))} · {subtitle}</div>

{missing_html}
<div class="callout {callout_cls}">{callout}</div>

<div class="cards">{"".join(cards)}</div>

{timeline_html}

<h2>Where the time went</h2>
{_stack_bar(stats["decomposition"])}

{baseline_html}

<div class="tabs" role="tablist">{tab_buttons}</div>
{tab_panels}

<h2>What to do about it</h2>
<ol class="todos">{todo_items}</ol>

<div class="footer">Generated {generated} by pytest-perf-report ·
command: <code>{esc(command)}</code> · numeric column headers are click-to-sort
· hover a headline card for what it means.</div>
<script>{SORT_JS}</script>
<script>{TAB_JS}</script>
</body></html>"""
