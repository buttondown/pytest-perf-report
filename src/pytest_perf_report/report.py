"""Self-contained HTML report.

No external assets: every style and script the page needs is inlined, so the
file can be opened from disk, attached to a CI run, or emailed as-is. The page
skeleton, stylesheet, and scripts live in ``templates/``; this module renders
each section to HTML and substitutes them into the skeleton.
"""

from __future__ import annotations

import datetime
import functools
import html
import string
from importlib import resources
from typing import Any

from pytest_perf_report.analyze import (
    N_PLUS_ONE_THRESHOLD,
    fmt_bytes,
    fmt_seconds,
    split_nodeid,
)
from pytest_perf_report.merge import SCHEMA_VERSION

esc = html.escape


@functools.cache
def _template(name: str) -> str:
    return (resources.files(__package__) / "templates" / name).read_text("utf-8")


STACK_COLORS = {
    "DB": "var(--series-1)",
    "HTTP (real)": "var(--series-5)",
    "sleep": "var(--series-4)",
    "GC": "var(--series-3)",
    "other CPU": "var(--series-2)",
    "other wait": "var(--faint)",
}

PHASE_COLORS = {
    "startup, imports": "var(--series-5)",
    "collection": "var(--series-1)",
    "session fixtures": "var(--series-3)",
    "test bodies": "var(--series-2)",
    "orchestration": "var(--chip)",
}

TIMELINE_COLORS = {
    "startup (imports, est.)": "var(--series-5)",
    "collection": "var(--series-1)",
    "waiting / scheduling": "var(--chip)",
    "session setup (first test)": "var(--series-3)",
    "tests": "var(--series-2)",
    "teardown + reporting": "var(--faint)",
    "orchestrating workers": "var(--chip)",
}

SEVERITY_PILL = {"high": "bad", "medium": "warn", "low": "neutral"}

# The tests table shows everything up to this cap (announced when it bites);
# a 10k-row sortable table is where browsers start to chug.
MAX_TEST_TABLE_ROWS = 5_000

# How much of the path a label keeps before it starts dropping directories.
PATH_LIMIT = 30

# Every tests-table row carries the same columns, so a family row and a case
# row stay comparable when either view is sorted. (key, is_time)
TEST_COLUMNS = [
    ("wall_s", True),
    ("setup_s", True),
    ("call_s", True),
    ("cpu_s", True),
    ("db_queries", False),
    ("db_time_s", True),
    ("http_calls", False),
]

TEST_HEADERS = [
    ("test", False),
    ("wall", True),
    ("setup", True),
    ("call", True),
    ("cpu", True),
    ("queries", True),
    ("db", True),
    ("http", True),
]

SHAPE_HEADERS = [
    ("query shape", False),
    ("count", True),
    ("% of queries", True),
    ("db time", True),
    ("tests", True),
    ("max / test", True),
]


# ---- kit components --------------------------------------------------------


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


def _strip(items: list[tuple[str, str]]) -> str:
    """The metrics that are worth a number but not worth a card."""
    return (
        '<div class="strip">'
        + "".join(
            f'<span>{esc(label)} <span class="v">{esc(value)}</span></span>'
            for label, value in items
        )
        + "</div>"
    )


def _num(raw: Any, formatted: str) -> str:
    """Numeric cell; data-v is the sort key the table script reads."""
    return f'<td class="num" data-v="{raw}">{formatted}</td>'


def _bar(fraction: float, max_px: int = 120) -> str:
    width = max(2, int(round(min(1.0, fraction) * max_px)))
    return f'<span class="bar" style="width:{width}px"></span>'


def _table(
    headers: list[tuple[str, bool]],
    rows: list[str],
    note: str = "",
    attrs: str = "",
) -> str:
    head = "".join(
        f'<th class="num sortable">{esc(h)}</th>' if numeric else f"<th>{esc(h)}</th>"
        for h, numeric in headers
    )
    note_html = f'<div class="note" style="margin:6px 0 0">{note}</div>' if note else ""
    return (
        f"<table{attrs}><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>" + note_html
    )


def _delta_cell(delta: float, pct: float | None, is_time: bool) -> str:
    """Kit trend readout: ▲ more cost is bad, ▼ less is good, — flat."""
    if delta == 0:
        return '<td class="num trend flat" data-v="0">—</td>'
    arrow, cls = ("▲", "bad") if delta > 0 else ("▼", "good")
    value = fmt_seconds(abs(delta)) if is_time else f"{abs(delta):,.0f}"
    pct_txt = f" ({pct:+.0%})" if pct is not None else ""
    return f'<td class="num trend {cls}" data-v="{delta}">{arrow} {value}{esc(pct_txt)}</td>'


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


def _elide_path(prefix: str) -> str:
    """Drop leading directories until the path fits, so a label stays on one
    line. The file (and class) survive; the whole nodeid is in the tooltip."""
    if len(prefix) <= PATH_LIMIT:
        return prefix
    parts = prefix.split("/")
    kept = parts[-1]
    for part in reversed(parts[:-1]):
        if len(part) + len(kept) + 1 > PATH_LIMIT:
            break
        kept = f"{part}/{kept}"
    return "…/" + kept


def _test_label(nodeid: str) -> str:
    """A nodeid as a dim path, the test name, then the parametrize id in its
    own chip — so names line up and a long id never buries the name."""
    prefix, name, param = split_nodeid(nodeid)
    label = (
        f'<code class="tid" title="{esc(nodeid)}">'
        f'<span class="tid-path">{esc(_elide_path(prefix))}</span>'
        f'<span class="tid-name">{esc(name)}</span></code>'
    )
    if param:
        label += f'<span class="param" title="{esc(param)}">{esc(param)}</span>'
    return label


# ---- charts ----------------------------------------------------------------


def _phase_bar(phases: list[tuple[str, float]]) -> str:
    """The run as one bar: every phase of the wall clock, end to end. The
    legend above names the segments, so the segments carry no labels."""
    total = sum(v for _, v in phases) or 1.0
    segments = "".join(
        f'<span class="seg" style="width:{value / total * 100:.2f}%;'
        f'background:{PHASE_COLORS[label]}" '
        f'title="{esc(label)}: {fmt_seconds(value)} ({value / total:.1%})"></span>'
        for label, value in phases
        if value > 0
    )
    legend = _legend(
        [
            (PHASE_COLORS[label], label, fmt_seconds(value))
            for label, value in phases
            if value > 0
        ]
    )
    return f'{legend}<div class="phasebar">{segments}</div>'


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
    return f'<div class="proportion">{segments}</div>{legend}'


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


# ---- page header -----------------------------------------------------------


def _subtitle(meta: dict[str, Any], stats: dict[str, Any]) -> str:
    outcomes = stats["outcomes"]
    failed_n = outcomes.get("failed", 0) + outcomes.get("error", 0)
    outcome_bits = [f"{outcomes.get('passed', 0):,} passed"]
    if failed_n:
        outcome_bits.append(f"{failed_n:,} failed")
    for outcome in ("skipped", "xfailed", "xpassed"):
        if outcomes.get(outcome):
            outcome_bits.append(f"{outcomes[outcome]:,} {outcome}")
    workers = stats["workers"]
    runner = f"{workers} xdist workers" if workers else "single process"
    parts = [
        meta.get("started", ""),
        f"{stats['tests']:,} tests",
        ", ".join(outcome_bits),
        runner,
        f"Python {meta.get('python', '?')}",
        f"pytest {meta.get('pytest', '?')}",
    ]
    return " · ".join(p for p in parts if p)


def _cards(merged: dict[str, Any], stats: dict[str, Any]) -> str:
    """Four headline cards: the numbers that earn poster treatment."""
    db_vendor = ", ".join(sorted(merged["db_vendors"])) or None
    cards = [
        _card(
            fmt_seconds(stats["agg_wall_s"]),
            "aggregate test time",
            "Sum of per-test wall time across all workers — total work done, "
            "ignoring parallelism.",
        ),
        _card(
            fmt_seconds(stats["p50"]),
            "median test",
            "Half of all tests finish within this.",
        ),
        _card(
            f"{stats['db_queries']:,}",
            f"DB queries ({db_vendor})" if db_vendor else "DB queries",
            "Statements observed by the ORM-level probes during tests "
            "(plus any outside-of-test work, noted in the DB tab).",
        ),
    ]
    if stats.get("peak_rss_bytes"):
        cards.append(
            _card(
                fmt_bytes(stats["peak_rss_bytes"]),
                "peak RSS / process",
                "Memory high-water mark; it never comes back down, so one hungry "
                "test sets the floor for the whole worker.",
            )
        )
    else:
        cards.append(
            _card(
                f"{stats['tests']:,}",
                "tests",
                "Tests that reported a result in this run.",
            )
        )
    return "".join(cards)


def _strip_html(merged: dict[str, Any], stats: dict[str, Any]) -> str:
    decomp = dict(stats["decomposition"])
    items = [
        ("GC", fmt_seconds(stats["gc_s"])),
        ("other CPU", fmt_seconds(decomp["other CPU"])),
        ("other wait", fmt_seconds(decomp["other wait"])),
        ("HTTP", f"{stats['http_calls']:,} · {fmt_seconds(stats['http_time_s'])}"),
        ("file opens", f"{stats['file_opens']:,}"),
        ("time.sleep()", fmt_seconds(stats["sleep_s"])),
    ]
    # With a single worker "parallel efficiency" would just restate startup
    # overhead in a confusing costume; only meaningful when work is fanned out.
    if stats["parallel_efficiency"] is not None and stats["workers"] >= 2:
        items.append(("parallel efficiency", f"{stats['parallel_efficiency']:.0%}"))
    io = merged["io"]
    if io["read_bytes"] or io["write_bytes"]:
        items.append(
            (
                "disk read / written",
                f"{fmt_bytes(io['read_bytes'])} / {fmt_bytes(io['write_bytes'])}",
            )
        )
    return _strip(items)


def _callout(merged: dict[str, Any], stats: dict[str, Any]) -> str:
    """One sentence under the headline when something outranks performance."""
    outcomes = stats["outcomes"]
    failed_n = outcomes.get("failed", 0) + outcomes.get("error", 0)
    external = [e for e, r in merged["net_endpoints"].items() if r["external"]]
    if failed_n:
        return (
            '<div class="callout bad">'
            f"<strong>{failed_n} test{'s' if failed_n != 1 else ''} failing.</strong> "
            "Performance notes below still apply, but green comes first.</div>"
        )
    if external:
        return (
            '<div class="callout warn">'
            f"<strong>All tests passed, but {len(external)} external network endpoint"
            f"{'s were' if len(external) != 1 else ' was'} contacted.</strong> "
            "The suite is not hermetic — see the network section and TODO #1–2.</div>"
        )
    return ""


def _missing_workers(meta: dict[str, Any]) -> str:
    if not meta.get("missing_workers"):
        return ""
    return (
        f'<div class="callout bad"><strong>{meta["missing_workers"]} xdist worker '
        "shard(s) missing.</strong> A worker crashed or doesn't share a filesystem "
        "with the controller — every number below under-reports.</div>"
    )


# ---- tab panels ------------------------------------------------------------


def _overview_panel(
    merged: dict[str, Any],
    stats: dict[str, Any],
    baseline_delta: dict[str, Any] | None,
) -> str:
    timeline = _timeline(merged.get("lanes", []))
    timeline_html = f"<h2>Run timeline</h2>\n{timeline}" if timeline else ""
    baseline_html = _baseline_section(baseline_delta) if baseline_delta else ""
    return f"""
{timeline_html}
<h2>Where the time went</h2>
{_stack_bar(stats["decomposition"])}
{baseline_html}
"""


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
    schema_note = ""
    if delta.get("baseline_schema") != SCHEMA_VERSION:
        schema_note = (
            f' <span class="pill warn" title="The baseline was written as schema '
            f"{esc(str(delta.get('baseline_schema')))}; this run writes schema "
            f'{SCHEMA_VERSION}.">schema mismatch</span>'
        )
    html = (
        f"<h2>Vs baseline ({esc(base_label)})</h2>"
        f'<div class="sub">{delta["common_tests"]:,} tests present in both runs.'
        f"{schema_note}</div>"
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
            f'<tr><td class="test">{_test_label(r["nodeid"])}</td>'
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


def _failures(per_test: list[dict[str, Any]]) -> str:
    failures = [t for t in per_test if t["outcome"] in ("failed", "error")]
    if not failures:
        return ""
    items = "".join(
        f"<details><summary>{_outcome_pill(t['outcome'])} "
        f"{_test_label(t['nodeid'])} · {fmt_seconds(t['wall_s'])}</summary>"
        f"<pre>{esc(t.get('failure') or '(no captured output)')}</pre></details>"
        for t in failures[:50]
    )
    more = (
        f'<div class="note">…and {len(failures) - 50} more.</div>'
        if len(failures) > 50
        else ""
    )
    return f"<h2>Failures</h2>{items}{more}"


def _test_cells(row: dict[str, Any]) -> str:
    return "".join(
        _num(row[key], fmt_seconds(row[key]) if is_time else f"{row[key]:,}")
        for key, is_time in TEST_COLUMNS
    )


def _test_row(
    t: dict[str, Any], first_test_nodeids: set[str], family: int | None = None
) -> str:
    session_pill = ""
    if t["nodeid"] in first_test_nodeids and t["setup_s"] > 0.5 * (t["wall_s"] or 1):
        session_pill = (
            ' <span class="pill neutral" title="First test in its process: '
            "pytest charges all session-scoped fixture setup (DB creation, "
            "cache warming) to this test's setup phase.\">includes session setup</span>"
        )
    attrs = f' class="case" data-fam="{family}"' if family is not None else ""
    return (
        f'<tr{attrs}><td class="test">'
        f"{_test_label(t['nodeid'])} "
        f"{_outcome_pill(t['outcome']) if t['outcome'] != 'passed' else ''}"
        f"{session_pill}</td>" + _test_cells(t) + "</tr>"
    )


def _family_row(family: int, nodeid: str, cases: list[dict[str, Any]]) -> str:
    totals = {key: sum(c[key] for c in cases) for key, _ in TEST_COLUMNS}
    failed = sum(1 for c in cases if c["outcome"] in ("failed", "error"))
    pills = f' <span class="pill neutral">{len(cases)} cases</span>' + (
        f' <span class="pill bad">{failed} failed</span>' if failed else ""
    )
    return (
        f'<tr class="fam" data-fam="{family}"><td class="test">'
        f'<span class="caret">▸</span> {_test_label(nodeid)}{pills}</td>'
        + _test_cells(totals)
        + "</tr>"
    )


def _tests_table(merged: dict[str, Any]) -> tuple[str, str]:
    """(table html, heading toggle html). One table, every test, slowest first,
    sortable by any column. Beyond the cap the page gets unwieldy; the cut is
    announced, never silent."""
    per_test = merged["per_test"]
    # Each process's first test gets billed for all session-scoped fixture
    # setup; flag it so it doesn't read as a genuinely slow test.
    first_test_nodeids = {
        lane.get("first_test_nodeid")
        for lane in merged.get("lanes", [])
        if lane.get("first_test_nodeid")
    }
    all_tests = sorted(per_test, key=lambda t: t["wall_s"])[::-1][:MAX_TEST_TABLE_ROWS]
    # The cases of one parametrized test are one entry: a whole parametrized
    # test is what a reader recognises, and 40 cases of it otherwise bury
    # everything else in the table.
    families: dict[str, list[dict[str, Any]]] = {}
    for t in all_tests:
        prefix, name, param = split_nodeid(t["nodeid"])
        families.setdefault(prefix + name if param else t["nodeid"], []).append(t)
    grouped = sorted(
        families.items(),
        key=lambda kv: sum(t["wall_s"] for t in kv[1]),
        reverse=True,
    )
    rows = []
    for family, (nodeid, cases) in enumerate(grouped):
        if len(cases) == 1:
            rows.append(_test_row(cases[0], first_test_nodeids))
            continue
        rows.append(_family_row(family, nodeid, cases))
        rows.extend(
            _test_row(t, first_test_nodeids, family)
            for t in sorted(cases, key=lambda t: t["wall_s"], reverse=True)
        )
    parametrized = sum(1 for _, cases in grouped if len(cases) > 1)
    note = ""
    if len(per_test) > MAX_TEST_TABLE_ROWS:
        note = (
            f"Showing the {MAX_TEST_TABLE_ROWS:,} slowest of {len(per_test):,} "
            "tests (the rest are in --perf-report-json)."
        )
    toggle = (
        '<button class="rowtoggle" id="group-toggle" aria-pressed="true">'
        "Group parametrized cases</button>"
        if parametrized
        else ""
    )
    table = _table(TEST_HEADERS, rows, note=note, attrs=' id="tests-table"')
    return table, toggle


def _tests_panel(merged: dict[str, Any], stats: dict[str, Any]) -> str:
    per_test = merged["per_test"]
    walls = sorted(t["wall_s"] for t in per_test)
    table, toggle = _tests_table(merged)
    return f"""
<h2>Test duration distribution</h2>
{_histogram(walls)}
<div class="note" style="margin-top:14px">p50 {fmt_seconds(stats["p50"])} · p90 {fmt_seconds(stats["p90"])} · p99 {fmt_seconds(stats["p99"])} · mean {fmt_seconds(stats["mean"])}</div>
{_failures(per_test)}
<h2 class="row">Tests{toggle}</h2>
{table}
"""


def _fixture_queries_cell(row: dict[str, Any]) -> str:
    queries = int(row.get("db_queries", 0))
    writes = int(row.get("db_writes", 0))
    text = f"{queries:,}" + (f" ({writes:,}w)" if writes else "")
    return _num(queries, text if queries else "—")


def _fixtures_panel(merged: dict[str, Any]) -> str:
    def fixture_label(name: str, row: dict[str, Any]) -> str:
        pills = ""
        if row.get("autouse"):
            pills += ' <span class="pill warn" title="Runs for every test in scope">autouse</span>'
        scope = row.get("scope")
        if scope and scope != "function":
            pills += f' <span class="pill neutral">{esc(scope)}</span>'
        return f"<code>{esc(name)}</code>{pills}"

    html = ""
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
            + _fixture_queries_cell(row)
            + "</tr>"
            for name, row in fixtures
        ]
        html += "<h2>Costliest fixtures (setup time)</h2>" + _table(
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
        )

    # Per-test taxes: autouse function-scoped fixtures run for every test.
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
        rows = [
            f"<tr><td><code>{esc(name)}</code></td>"
            + _num(row.get("self_s", 0.0), fmt_seconds(row.get("self_s", 0.0)))
            + _num(row["count"], f"{int(row['count']):,}")
            + _num(
                row.get("self_s", 0.0) / (row["count"] or 1) * 1000,
                f"{row.get('self_s', 0.0) / (row['count'] or 1) * 1000:.2f}ms",
            )
            + _fixture_queries_cell(row)
            + "</tr>"
            for name, row in taxes
        ]
        html += "<h2>Per-test taxes (autouse fixtures)</h2>" + _table(
            [
                ("fixture", False),
                ("self total", True),
                ("runs", True),
                ("per run", True),
                ("queries", True),
            ],
            rows,
        )
    return html


def _shape_origin(row: dict[str, Any]) -> str:
    origins = row.get("origins")
    if origins:
        top = max(origins.items(), key=lambda kv: kv[1])
        note = f"↳ {esc(top[0])} ({top[1]:,}×)"
    elif row.get("origin_first") and row["origin_first"] != "?":
        note = f"↳ first issued from {esc(row['origin_first'])}"
    else:
        note = ""
    # Name the test where this shape repeated the most — the place to open
    # when chasing an N+1.
    if row.get("max_test") and row.get("max_in_one_test", 0) >= 10:
        if note:
            note += " · "
        note += (
            f"peak {row['max_in_one_test']:,}× in <code>{esc(row['max_test'])}</code>"
        )
    return f'<div class="note">{note}</div>' if note else ""


def _shape_rows(items: list[tuple[str, dict[str, Any]]], total_q: int) -> list[str]:
    return [
        # The title tooltip carries a concrete example statement.
        f'<tr><td><code title="{esc(row.get("example", ""))}">{esc(shape[:160])}</code>'
        f"{_shape_origin(row)}</td>"
        + _num(row["count"], f"{row['count']:,}")
        + _num(row["count"] / total_q, f"{row['count'] / total_q:.1%}")
        + _num(row["db_time_s"], fmt_seconds(row["db_time_s"]))
        + _num(row["tests"], f"{row['tests']:,}")
        + _num(row["max_in_one_test"], f"{row['max_in_one_test']:,}")
        + "</tr>"
        for shape, row in items
    ]


def _db_panel(merged: dict[str, Any], stats: dict[str, Any]) -> str:
    shapes = merged["shapes"]
    if not shapes:
        return ""
    total_q = sum(r["count"] for r in shapes.values()) or 1
    by_count = sorted(shapes.items(), key=lambda kv: kv[1]["count"], reverse=True)[:20]
    by_time = sorted(shapes.items(), key=lambda kv: kv[1]["db_time_s"], reverse=True)[
        :20
    ]
    n_plus_one = sorted(
        (
            (s, r)
            for s, r in shapes.items()
            if r.get("max_in_one_test", 0) >= N_PLUS_ONE_THRESHOLD
        ),
        key=lambda kv: kv[1]["max_in_one_test"],
        reverse=True,
    )[:15]
    outside = merged["db_outside"]
    html = (
        "<h2>Top query shapes by count</h2>"
        + f'<div class="sub">{len(shapes):,} distinct shapes · '
        + f"{stats['db_queries']:,} queries total"
        + (
            f" · {outside['queries']:,} ran outside tests "
            f"(migrations/fixtures/teardown, {fmt_seconds(stats['db_time_outside_s'])})"
            if outside["queries"]
            else ""
        )
        + "</div>"
        + _table(SHAPE_HEADERS, _shape_rows(by_count, total_q))
        + "<h2>Top query shapes by total DB time</h2>"
        + _table(SHAPE_HEADERS, _shape_rows(by_time, total_q))
    )
    if n_plus_one:
        html += (
            f"<h2>N+1 suspects (same shape ≥{N_PLUS_ONE_THRESHOLD}× within one test)</h2>"
            + _table(SHAPE_HEADERS, _shape_rows(n_plus_one, total_q))
        )
    return html


def _http_panel(merged: dict[str, Any]) -> str:
    if not (merged["http_hosts"] or merged["net_endpoints"]):
        return ""
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
    html = "<h2>HTTP calls by host</h2>" + (
        _table(
            [
                ("host", False),
                ("calls", True),
                ("time", True),
                ("mean", True),
                ("transport", False),
            ],
            rows,
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
        html += "<h2>Socket connections</h2>" + _table(
            [
                ("endpoint", False),
                ("connects", True),
                ("connect time", True),
                ("kind", False),
            ],
            net_rows,
        )
    return html


def _cpu_panel(merged: dict[str, Any]) -> str:
    """Opt-in via --perf-report-cpu-profile."""
    cpu_profile = merged.get("cpu_profile") or {}
    if not cpu_profile:
        return ""
    top_funcs = sorted(cpu_profile.items(), key=lambda kv: kv[1][0], reverse=True)[:25]
    total_profiled = sum(row[0] for row in cpu_profile.values()) or 1.0
    rows = [
        f"<tr><td><code>{esc(key)}</code></td>"
        + _num(row[0], fmt_seconds(row[0]))
        + _num(row[0] / total_profiled, f"{row[0] / total_profiled:.1%}")
        + _num(row[1], f"{int(row[1]):,}")
        + "</tr>"
        for key, row in top_funcs
    ]
    return "<h2>Where the CPU went (cProfile)</h2>" + _table(
        [
            ("function", False),
            ("self time", True),
            ("% of profiled", True),
            ("calls", True),
        ],
        rows,
    )


def _files_panel(merged: dict[str, Any]) -> str:
    top_files = merged["files"]["top"].most_common(15)
    if not top_files:
        return ""
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
    return "<h2>Most-opened files</h2>" + _table(
        [("path", False), ("opens", True), ("≈ re-read", True)],
        rows,
    )


def _startup_panel(merged: dict[str, Any]) -> str:
    html = ""
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
        html += "<h2>Slowest test modules to collect</h2>" + _table(
            [("module", False), ("collect time", True)],
            rows,
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
        html += "<h2>Heaviest startup imports</h2>" + _table(
            [("module", False), ("self", True), ("cumulative", True)],
            rows,
        )
    return html


def _warnings_panel(merged: dict[str, Any], stats: dict[str, Any]) -> str:
    top_warnings = merged["warnings"].most_common(15)
    if not top_warnings:
        return ""
    rows = [
        f"<tr><td><code>{esc(cat)}</code> {esc(msg)}</td>" + _num(n, f"{n:,}") + "</tr>"
        for (cat, msg), n in top_warnings
    ]
    return "<h2>Warnings</h2>" + _table(
        [(f"warning ({stats['warnings_total']:,} total)", False), ("count", True)],
        rows,
    )


def _todo_items(todos: list[dict[str, str]]) -> str:
    return "".join(
        f'<li><span class="pill {SEVERITY_PILL[t["severity"]]}">{t["severity"]}</span> '
        f'<span class="t">{esc(t["title"])}</span>'
        f'<div class="b">{esc(t["body"])}</div></li>'
        for t in todos
    )


# ---- page ------------------------------------------------------------------


def render(
    merged: dict[str, Any],
    stats: dict[str, Any],
    todos: list[dict[str, str]],
    baseline_delta: dict[str, Any] | None = None,
) -> str:
    meta = merged["meta"]

    # (label, key, panel html, badge) — a tab only exists when it has content.
    panels = [
        ("Overview", "overview", _overview_panel(merged, stats, baseline_delta), ""),
        ("Tests", "tests", _tests_panel(merged, stats), f"{stats['tests']:,}"),
        ("Startup", "startup", _startup_panel(merged), ""),
        ("Fixtures", "fixtures", _fixtures_panel(merged), ""),
        ("DB", "db", _db_panel(merged, stats), f"{stats['db_queries']:,}"),
        ("CPU", "cpu", _cpu_panel(merged), ""),
        ("HTTP", "http", _http_panel(merged), f"{stats['http_calls']:,}"),
        ("Files", "files", _files_panel(merged), f"{stats['file_opens']:,}"),
        (
            "Warnings",
            "warnings",
            _warnings_panel(merged, stats),
            f"{stats['warnings_total']:,}",
        ),
    ]
    panels = [row for row in panels if row[2].strip()]
    tab_buttons = "".join(
        f'<button data-tab="{key}" aria-selected="false">{esc(label)}'
        + (f'<span class="badge">{esc(badge)}</span>' if badge else "")
        + "</button>"
        for label, key, _, badge in panels
    )
    tab_panels = "".join(
        f'<section class="tab-panel" data-tab="{key}" hidden>{body}</section>'
        for _, key, body, _ in panels
    )

    return string.Template(_template("report.html")).substitute(
        css=_template("report.css"),
        js=_template("report.js"),
        project=esc(meta.get("project", "")),
        subtitle=_subtitle(meta, stats),
        missing_html=_missing_workers(meta),
        total_wall=fmt_seconds(stats["total_wall_s"]),
        phase_bar=_phase_bar(stats["phases"]),
        cards=_cards(merged, stats),
        strip=_strip_html(merged, stats),
        callout_html=_callout(merged, stats),
        tab_buttons=tab_buttons,
        tab_panels=tab_panels,
        todo_items=_todo_items(todos),
        generated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        command=esc(meta.get("args", "")),
    )
