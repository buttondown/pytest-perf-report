# Changelog

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
