# Changelog

## 0.4.0

- A `pytest-perf-report` console script runs pytest with the report enabled,
  so `uvx pytest-perf-report` and `uv run --with pytest-perf-report
  pytest-perf-report` work without installing the plugin.
- Parametrized cases are grouped into one family row in the tests table, with
  a toggle to flatten the table.
- `pathlib` file opens are counted on Python 3.10.
- The `--perf-report-json` output carries a top-level `schema` key. A baseline
  written with a different schema is flagged in the report and the terminal
  summary.
- The report's HTML skeleton, stylesheet, and scripts moved into
  `templates/`; the generated page is unchanged.

## 0.3.0

First public release. Earlier versions lived inside Buttondown's monorepo and
were never published.

- One flag (`--perf-report`) writes a single self-contained HTML report:
  headline numbers, startup and collection cost, a decomposition of where
  in-test time went, database query shapes and N+1 suspects, HTTP and socket
  activity, file opens and disk IO, fixture cost, memory high-water marks,
  flaky tests, warnings, and a prioritized TODO list.
- `--perf-report-json` emits the same data as versioned JSON;
  `--perf-report-baseline` diffs a run against a previous JSON file.
- `--perf-report-sql-origins` attributes every query to its call-site;
  `--perf-report-cpu-profile` adds a per-function CPU table.
- Probes for Django (`execute_wrapper`, any backend), SQLAlchemy (cursor
  events), `requests`, `httpx`, `http.client`, sockets, `time.sleep`,
  `open()`, GC pauses, and `/proc/self/io`. Optional-library probes no-op
  when the library is absent.
- Works unchanged under pytest-xdist and is pytest-rerunfailures aware.
