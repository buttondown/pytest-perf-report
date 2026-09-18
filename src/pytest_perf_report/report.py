"""Self-contained HTML report.

No external assets: every style and script the page needs is inlined, so the
file can be opened from disk, attached to a CI run, or emailed as-is.
"""

from __future__ import annotations

import datetime
import html
from typing import Any

from pytest_perf_report.analyze import (
    N_PLUS_ONE_THRESHOLD,
    fmt_bytes,
    fmt_seconds,
    split_nodeid,
)

esc = html.escape

# A small component set (tokens, cards, pills, callouts, proportion bar,
# barchart, legend, trend, table), followed by a clearly-marked block of
# report-specific extensions (sortable headers, timeline lanes, failure
# disclosure, TODO list) built on the same tokens.
CSS = """
:root {
  /* Surfaces: Apple's neutral dark greys over true black, not a tinted dark. */
  --bg: #000000; --panel: #1c1c1e; --panel2: #2c2c2e; --panel3: #3a3a3c;
  --chip: rgba(255, 255, 255, 0.08); --border: rgba(255, 255, 255, 0.10);
  /* Text: label, secondary label, tertiary label. */
  --fg: #f5f5f7; --muted: rgba(235, 235, 245, 0.60); --faint: rgba(235, 235, 245, 0.32);
  /* Semantic: the dark-mode system colours. */
  --accent: #0a84ff; --warn: #ff9f0a; --good: #30d158; --bad: #ff453a;
  /* Categorical chart palette: the same system colours, in chart order. */
  --series-1: #0a84ff; --series-2: #30d158; --series-3: #ff9f0a; --series-4: #ff453a;
  --series-5: #bf5af2; --series-6: #40c8e0; --series-7: #ff375f; --series-8: #ffd60a;
  /* Three radii and nothing else: chip, panel, capsule. */
  --r-sm: 6px; --r-md: 12px; --r-full: 980px;
}
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1080px; padding: 32px; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased; }
h1 { font-size: 24px; margin: 0 0 4px; font-weight: 600; }
/* Header: the run's identity on one dim line, then the one number that
   answers "how long did this take?" at poster size. The hero is set in the
   system font, not a monospace: only the figures are tabular. */
.head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; color: var(--muted);
  font-size: 13px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
.head .proj { color: var(--fg); font-size: 13px; font-weight: 600; }
h1.hero { font-size: 64px; line-height: 1.05; font-weight: 600; letter-spacing: -0.035em;
  margin: 24px 0 16px; font-variant-numeric: tabular-nums; }
/* The whole run as one bar. The segments carry no labels — the legend above
   already names them, and a bar that repeats it just gets louder. */
.phasebar { display: flex; width: 100%; height: 34px; margin-top: 14px;
  background: var(--panel2); border-radius: var(--r-md); overflow: hidden; }
.phasebar .seg + .seg { border-left: 1px solid var(--bg); }
/* The leftovers: real numbers, but none of them is the story on its own. */
.strip { display: flex; flex-wrap: wrap; gap: 8px 26px; margin-top: 18px;
  color: var(--muted); font-size: 13px; }
.strip .v { color: var(--fg); font-weight: 600; font-variant-numeric: tabular-nums; }
h2 { font-size: 16px; margin: 36px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--border);
  font-weight: 600; letter-spacing: -0.01em; }
h2.row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code { background: var(--chip); padding: 1.5px 6px; border-radius: var(--r-sm); font-size: 13px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
/* Test labels never wrap. Under pressure the dim path gives way first — the
   test name is never clipped — and the tooltip holds the whole nodeid. */
td.test { white-space: nowrap; }
code.tid { display: inline-flex; max-width: 52ch; vertical-align: bottom; white-space: nowrap; }
.rowtoggle { background: var(--chip); border: none; border-radius: var(--r-full); color: var(--muted);
  font: inherit; font-size: 12px; font-weight: 400; padding: 4px 12px; cursor: pointer; }
.rowtoggle:hover { color: var(--fg); }
.rowtoggle[aria-pressed="true"] { color: var(--fg); background: var(--panel3); }
tr.fam { cursor: pointer; }
tr.fam .caret { display: inline-block; width: 10px; color: var(--faint); }
tr.case > td:first-child { padding-left: 38px; }
table:not(.ungrouped) tr.case .tid { display: none; }
code.tid .tid-path { color: var(--faint); overflow: hidden; text-overflow: ellipsis; min-width: 0; }
code.tid .tid-name { flex: none; }
.param { display: inline-block; max-width: 30ch; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; vertical-align: bottom; color: var(--muted); background: var(--chip);
  padding: 1.5px 6px; border-radius: var(--r-sm); font-size: 12px; margin-left: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.sub, .note, .footer { color: var(--muted); font-size: 13px; }
.subhead { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; margin: 20px 0 10px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
/* Cards sit on a lighter fill, with no outline: the fill is the separation. */
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 24px 0 0; }
@media (max-width: 920px) { .cards { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); } }
.card { background: var(--panel); border-radius: var(--r-md); padding: 20px; cursor: help; }
/* The fill lifts on hover, so the tooltip announces itself without four
   permanent underlines competing with the numbers. */
.card[title]:hover { background: var(--panel2); }
.card .n { font-size: 32px; font-weight: 600; letter-spacing: -0.025em; font-variant-numeric: tabular-nums; }
.card .l { color: color-mix(in srgb, var(--fg) 75%, var(--muted)); font-size: 13px; margin-top: 6px; }
.trend { font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.trend.good { color: var(--good); }
.trend.bad { color: var(--bad); }
.trend.flat { color: var(--faint); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 24px; margin: 12px 0 0; }
.legend-item { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); white-space: nowrap; }
.legend-item .swatch { width: 9px; height: 9px; border-radius: var(--r-full); flex: none; }
.legend-item .v { color: var(--fg); font-weight: 600; font-variant-numeric: tabular-nums; }
.barchart { display: flex; flex-direction: column; gap: 8px; }
.bar-row { display: grid; grid-template-columns: 100px 1fr 78px; align-items: center; gap: 12px; }
.bar-row .bl { color: var(--muted); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-track { height: 18px; background: var(--panel2); border-radius: var(--r-full); overflow: hidden; }
.bar-fill { height: 100%; background: var(--accent); border-radius: var(--r-full); }
.bar-row .bv { text-align: right; white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; font-variant-numeric: tabular-nums; }
.bar { height: 6px; background: var(--accent); border-radius: var(--r-full); display: inline-block; vertical-align: middle; }
.proportion { display: flex; width: 100%; height: 18px; border-radius: var(--r-full);
  overflow: hidden; background: var(--panel2); }
.proportion > span { height: 100%; }
.proportion > span + span { border-left: 1px solid var(--bg); }
/* Tables keep their row hairlines and lose the box: 5,000 rows still need the
   horizontal rules, but the outline around them adds nothing. */
table { width: 100%; border-collapse: collapse; background: var(--panel);
  border-radius: var(--r-md); overflow: hidden; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border); }
th { background: var(--panel2); color: var(--muted); font-weight: 600; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.04em; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--panel2); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.muted { color: var(--muted); }
.pill { display: inline-block; padding: 1px 8px; border-radius: var(--r-full); font-size: 11px;
  font-weight: 600; background: var(--chip); color: var(--fg); }
.pill.good { background: color-mix(in srgb, var(--good) 22%, transparent); color: var(--good); }
.pill.warn { background: color-mix(in srgb, var(--warn) 22%, transparent); color: var(--warn); }
.pill.bad { background: color-mix(in srgb, var(--bad) 22%, transparent); color: var(--bad); }
.pill.neutral { background: var(--chip); color: var(--muted); }
/* A callout is a tinted panel, not an outlined one, so the tint carries more
   of the signal than it did behind a border. */
.callout { background: color-mix(in srgb, var(--good) 14%, var(--panel));
  border-radius: var(--r-md); padding: 16px 18px; margin: 18px 0; }
.callout.warn { background: color-mix(in srgb, var(--warn) 14%, var(--panel)); }
.callout.bad { background: color-mix(in srgb, var(--bad) 14%, var(--panel)); }
.footer { margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--border); }

/* ---- Report-specific extensions on kit tokens ----------------------------- */
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: var(--fg); }
.tl-row { display: flex; align-items: center; gap: 10px; margin: 5px 0; }
.tl-label { width: 52px; color: var(--muted); font-size: 11px; text-align: right;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; flex-shrink: 0; }
.lane { position: relative; flex: 1; height: 18px; background: var(--panel2);
  border-radius: var(--r-full); overflow: hidden; }
.lane .seg { position: absolute; top: 0; bottom: 0; }
.tl-axis { display: flex; justify-content: space-between; color: var(--faint);
  font-size: 11px; margin: 2px 0 0 62px; font-variant-numeric: tabular-nums; }
details { background: var(--panel); border-radius: var(--r-md); margin: 8px 0; }
details summary { cursor: pointer; padding: 10px 14px; }
details pre { margin: 0; padding: 12px 14px; overflow-x: auto; font-size: 12px; color: var(--bad);
  border-top: 1px solid var(--border); }
ol.todos { padding-left: 0; list-style: none; counter-reset: todo; }
ol.todos li { background: var(--panel); border-radius: var(--r-md);
  padding: 16px 18px 16px 52px; margin: 10px 0; position: relative; counter-increment: todo; }
ol.todos li::before { content: counter(todo); position: absolute; left: 18px; top: 16px;
  width: 24px; height: 24px; border-radius: var(--r-full); background: var(--chip); color: var(--muted);
  font-weight: 600; display: flex; align-items: center; justify-content: center; font-size: 13px; }
ol.todos .t { font-weight: 600; margin-right: 8px; }
ol.todos .b { color: var(--muted); font-size: 13px; margin-top: 4px; }
/* A segmented control, not an underlined tab strip. */
.tabs { display: inline-flex; flex-wrap: wrap; gap: 2px; margin: 32px 0 0; padding: 2px;
  background: var(--chip); border-radius: var(--r-full); }
.tabs button { background: none; border: none; border-radius: var(--r-full); color: var(--muted);
  font: inherit; font-weight: 600; font-size: 13px; padding: 6px 14px; cursor: pointer; }
.tabs button:hover { color: var(--fg); }
.tabs button[aria-selected="true"] { color: var(--fg); background: var(--panel3);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.32); }
.tabs .badge { margin-left: 7px; padding: 1px 7px; border-radius: var(--r-full); background: var(--chip);
  color: var(--muted); font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tabs button[aria-selected="true"] .badge { color: var(--fg); }
.tab-panel > h2:first-child { margin-top: 24px; border-bottom: none; padding-bottom: 0; }
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
function sortTable(table, idx, dir) {
  var tbody = table.tBodies[0];
  var rows = Array.prototype.slice.call(tbody.rows);
  function value(row) {
    var cell = row.cells[idx];
    return (cell && parseFloat(cell.dataset.v || cell.textContent)) || 0;
  }
  function cmp(a, b) { return dir === 'desc' ? value(b) - value(a) : value(a) - value(b); }
  table.dataset.sortIdx = idx;
  table.dataset.sortDir = dir;
  // Grouped: order the family/solo rows, then re-attach each family's cases
  // under it. Ungrouped: one flat order over the cases, families parked last.
  var grouped = !table.classList.contains('ungrouped');
  var heads = rows.filter(function (r) {
    return grouped ? !r.classList.contains('case') : !r.classList.contains('fam');
  });
  var cases = {};
  rows.forEach(function (r) {
    if (grouped && r.classList.contains('case')) {
      (cases[r.dataset.fam] = cases[r.dataset.fam] || []).push(r);
    }
  });
  heads.sort(cmp);
  heads.forEach(function (head) {
    tbody.appendChild(head);
    var kids = cases[head.dataset.fam];
    if (kids) { kids.sort(cmp); kids.forEach(function (k) { tbody.appendChild(k); }); }
  });
  if (!grouped) {
    rows.forEach(function (r) { if (r.classList.contains('fam')) tbody.appendChild(r); });
  }
}
document.querySelectorAll('th.sortable').forEach(function (th) {
  th.addEventListener('click', function () {
    var table = th.closest('table');
    var dir = th.dataset.dir === 'desc' ? 'asc' : 'desc';
    table.querySelectorAll('th.sortable').forEach(function (h) { delete h.dataset.dir; });
    th.dataset.dir = dir;
    sortTable(table, Array.prototype.indexOf.call(th.parentNode.children, th), dir);
  });
});
"""

# Parametrized cases collapse into their family row by default; the toggle
# flips the whole table between the two views, a family row opens just itself.
GROUP_JS = """
(function () {
  var table = document.getElementById('tests-table');
  if (!table) return;
  var toggle = document.getElementById('group-toggle');
  var rows = Array.prototype.slice.call(table.tBodies[0].rows);
  var families = {};
  rows.forEach(function (r) { if (r.classList.contains('fam')) families[r.dataset.fam] = r; });
  function apply() {
    var grouped = !table.classList.contains('ungrouped');
    rows.forEach(function (r) {
      if (r.classList.contains('fam')) {
        r.hidden = !grouped;
      } else if (r.classList.contains('case')) {
        var family = families[r.dataset.fam];
        r.hidden = grouped && !family.classList.contains('open');
      }
    });
  }
  Object.keys(families).forEach(function (key) {
    var family = families[key];
    family.addEventListener('click', function () {
      family.classList.toggle('open');
      family.querySelector('.caret').textContent =
        family.classList.contains('open') ? '▾' : '▸';
      apply();
    });
  });
  toggle.addEventListener('click', function () {
    var grouped = !table.classList.toggle('ungrouped');
    toggle.setAttribute('aria-pressed', String(grouped));
    sortTable(table, Number(table.dataset.sortIdx || 1), table.dataset.sortDir || 'desc');
    apply();
  });
  apply();
})();
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


PHASE_COLORS = {
    "startup, imports": "var(--series-5)",
    "collection": "var(--series-1)",
    "session fixtures": "var(--series-3)",
    "test bodies": "var(--series-2)",
    "orchestration": "var(--chip)",
}


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


def _num(raw: Any, formatted: str) -> str:
    """Numeric cell; data-v is the sort key SORT_JS reads."""
    return f'<td class="num" data-v="{raw}">{formatted}</td>'


def _bar(fraction: float, max_px: int = 120) -> str:
    width = max(2, int(round(min(1.0, fraction) * max_px)))
    return f'<span class="bar" style="width:{width}px"></span>'


# How much of the path a label keeps before it starts dropping directories.
PATH_LIMIT = 30


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
    return f'<div class="proportion">{segments}</div>{legend}'


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
        f'<div class="sub">{delta["common_tests"]:,} tests present in both runs.</div>'
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

    # --- headline: four cards, then the numbers that don't earn one ---
    decomp = dict(stats["decomposition"])
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

    strip = [
        ("GC", fmt_seconds(stats["gc_s"])),
        ("other CPU", fmt_seconds(decomp["other CPU"])),
        ("other wait", fmt_seconds(decomp["other wait"])),
        ("HTTP", f"{stats['http_calls']:,} · {fmt_seconds(stats['http_time_s'])}"),
        ("file opens", f"{stats['file_opens']:,}"),
        ("time.sleep()", fmt_seconds(stats["sleep_s"])),
    ]
    # With a single worker "parallel efficiency" would just restate startup
    # overhead in a confusing costume; only meaningful when work is fanned out.
    if stats["parallel_efficiency"] is not None and workers >= 2:
        strip.append(("parallel efficiency", f"{stats['parallel_efficiency']:.0%}"))
    io = merged["io"]
    if io["read_bytes"] or io["write_bytes"]:
        strip.append(
            (
                "disk read / written",
                f"{fmt_bytes(io['read_bytes'])} / {fmt_bytes(io['write_bytes'])}",
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
        callout_cls, callout = "", ""

    # --- failures ---
    failures_html = ""
    failures = [t for t in per_test if t["outcome"] in ("failed", "error")]
    if failures:
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
        failures_html = f"<h2>Failures</h2>{items}{more}"

    # --- slowest / fastest ---
    by_wall_asc = sorted(per_test, key=lambda t: t["wall_s"])
    sorted_walls = [t["wall_s"] for t in by_wall_asc]
    # Each process's first test gets billed for all session-scoped fixture
    # setup; flag it so it doesn't read as a genuinely slow test.
    first_test_nodeids = {
        lane.get("first_test_nodeid")
        for lane in merged.get("lanes", [])
        if lane.get("first_test_nodeid")
    }

    # Every row carries the same columns, so a family row and a case row stay
    # comparable when either view is sorted.
    TEST_COLUMNS = [
        ("wall_s", True),
        ("setup_s", True),
        ("call_s", True),
        ("cpu_s", True),
        ("db_queries", False),
        ("db_time_s", True),
        ("http_calls", False),
    ]

    def test_cells(row: dict[str, Any]) -> str:
        return "".join(
            _num(row[key], fmt_seconds(row[key]) if is_time else f"{row[key]:,}")
            for key, is_time in TEST_COLUMNS
        )

    def test_row(t: dict[str, Any], family: int | None = None) -> str:
        session_pill = ""
        if t["nodeid"] in first_test_nodeids and t["setup_s"] > 0.5 * (
            t["wall_s"] or 1
        ):
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
            f"{session_pill}</td>" + test_cells(t) + "</tr>"
        )

    def family_row(family: int, nodeid: str, cases: list[dict[str, Any]]) -> str:
        totals = {key: sum(c[key] for c in cases) for key, _ in TEST_COLUMNS}
        failed = sum(1 for c in cases if c["outcome"] in ("failed", "error"))
        pills = f' <span class="pill neutral">{len(cases)} cases</span>' + (
            f' <span class="pill bad">{failed} failed</span>' if failed else ""
        )
        return (
            f'<tr class="fam" data-fam="{family}"><td class="test">'
            f'<span class="caret">▸</span> {_test_label(nodeid)}{pills}</td>'
            + test_cells(totals)
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
    test_rows = []
    for family, (nodeid, cases) in enumerate(grouped):
        if len(cases) == 1:
            test_rows.append(test_row(cases[0]))
            continue
        test_rows.append(family_row(family, nodeid, cases))
        test_rows.extend(
            test_row(t, family)
            for t in sorted(cases, key=lambda t: t["wall_s"], reverse=True)
        )
    parametrized = sum(1 for _, cases in grouped if len(cases) > 1)
    tests_note = ""
    if len(by_wall_asc) > MAX_TEST_TABLE_ROWS:
        tests_note = (
            f"Showing the {MAX_TEST_TABLE_ROWS:,} slowest of {len(by_wall_asc):,} "
            "tests (the rest are in --perf-report-json)."
        )
    tests_toggle = (
        '<button class="rowtoggle" id="group-toggle" aria-pressed="true">'
        "Group parametrized cases</button>"
        if parametrized
        else ""
    )
    tests_table = _table(
        test_headers, test_rows, note=tests_note, attrs=' id="tests-table"'
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
        taxes_html = "<h2>Per-test taxes (autouse fixtures)</h2>" + _table(
            [
                ("fixture", False),
                ("self total", True),
                ("runs", True),
                ("per run", True),
                ("queries", True),
            ],
            rows,
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
        )

    # --- startup & collection ---
    startup_html = ""
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

    callout_html = (
        f'<div class="callout {callout_cls}">{callout}</div>' if callout else ""
    )

    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    command = meta.get("args", "")

    tests_panel = f"""
<h2>Test duration distribution</h2>
{_histogram(sorted_walls)}
<div class="note" style="margin-top:14px">p50 {fmt_seconds(stats["p50"])} · p90 {fmt_seconds(stats["p90"])} · p99 {fmt_seconds(stats["p99"])} · mean {fmt_seconds(stats["mean"])}</div>
{failures_html}
<h2 class="row">Tests{tests_toggle}</h2>
{tests_table}
"""
    overview_panel = f"""
{timeline_html}
<h2>Where the time went</h2>
{_stack_bar(stats["decomposition"])}
{baseline_html}
"""

    # (label, key, panel html, badge) — a tab only exists when it has content.
    panels = [
        ("Overview", "overview", overview_panel, ""),
        ("Tests", "tests", tests_panel, f"{stats['tests']:,}"),
        ("Startup", "startup", startup_html, ""),
        ("Fixtures", "fixtures", fixtures_html + taxes_html, ""),
        ("DB", "db", db_html, f"{stats['db_queries']:,}"),
        ("CPU", "cpu", cpu_html, ""),
        ("HTTP", "http", http_html, f"{stats['http_calls']:,}"),
        ("Files", "files", files_html, f"{stats['file_opens']:,}"),
        ("Warnings", "warnings", warnings_html, f"{stats['warnings_total']:,}"),
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

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test suite performance report — {esc(meta.get("project", ""))}</title>
<style>{CSS}</style></head><body>
<div class="head"><span class="proj">{esc(meta.get("project", ""))}</span>
<span>test suite performance · {subtitle}</span></div>

{missing_html}
<h1 class="hero" title="End-to-end clock for the whole run, including per-process startup and collection (paid once per xdist worker).">{fmt_seconds(stats["total_wall_s"])}</h1>
{_phase_bar(stats["phases"])}

<div class="cards">{"".join(cards)}</div>
{_strip(strip)}

{callout_html}

<div class="tabs" role="tablist">{tab_buttons}</div>
{tab_panels}

<h2>What to do about it</h2>
<ol class="todos">{todo_items}</ol>

<div class="footer">Generated {generated} by pytest-perf-report ·
command: <code>{esc(command)}</code></div>
<script>{SORT_JS}</script>
<script>{GROUP_JS}</script>
<script>{TAB_JS}</script>
</body></html>"""
