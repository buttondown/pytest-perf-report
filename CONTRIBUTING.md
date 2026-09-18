# Contributing

Thanks for taking a look. Bug reports and pull requests are welcome; for
anything larger than a fix, please open an issue first so we can agree on the
shape before you spend time on it.

## Setup

The project uses [uv](https://docs.astral.sh/uv/). Python 3.10 or newer.

```bash
uv sync                # creates .venv with pytest, Django, SQLAlchemy, ruff, etc.
uv run pytest          # the plugin's own suite
uv run ruff check .    # lint
uv run ruff format .   # format
```

Try your change against a real suite by pointing another project at your
checkout:

```bash
cd ../some-project
uv pip install -e ../pytest-perf-report
pytest --perf-report --perf-report-json=perf.json
```

## What a good change looks like

- Every probe measures through the narrowest seam available: a stdlib
  function when one exists (`http.client`, `socket`, `time.sleep`, `open`),
  otherwise a library's own extension API (Django's `execute_wrapper`,
  SQLAlchemy's cursor events). Please don't add hard dependencies; optional
  libraries are detected at install time and skipped when absent.
- Everything must be zero-overhead when `--perf-report` is not passed. The
  plugin registers its options and otherwise never touches a clock.
- Tests live in `tests/` and drive the plugin end to end through `pytester`;
  a change to what the report measures should come with a test that runs a
  tiny suite and asserts on the JSON output.
- Keep the HTML report dependency-free. No external scripts, styles, or fonts.
- The plugin's own suite cannot be run with `--perf-report`: its tests start
  nested pytest sessions through `pytester`, and the plugin deliberately
  leaves a nested session unprofiled while an outer profiled one is active.
  Profile a real project instead (see above).
- Lists longer than five items are alphabetized.

## Releasing

1. Bump `version` in `pyproject.toml` and `__version__` in
   `src/pytest_perf_report/__init__.py`.
2. Add a section to `CHANGELOG.md`.
3. Tag the commit `vX.Y.Z` and push the tag. The release workflow builds the
   wheel and publishes it to PyPI through trusted publishing; it refuses a tag
   that does not match the version in `pyproject.toml`.

## License

By contributing you agree that your contributions are licensed under the MIT
License in `LICENSE`.
