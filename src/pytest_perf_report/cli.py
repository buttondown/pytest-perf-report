"""``pytest-perf-report`` console script: run pytest with the report enabled.

Exists so ``uvx pytest-perf-report`` (or ``uv run --with pytest-perf-report
pytest-perf-report`` inside a project) works without installing the plugin.
Every argument is passed through to pytest; a later ``--perf-report=PATH``
overrides the default output path.
"""

from __future__ import annotations

import sys

import pytest

# Importing this module imports the package, so pytest cannot assert-rewrite
# the plugin and says so. The plugin has no asserts; the warning is noise.
_REWRITE_WARNING_FILTER = (
    "ignore:Module already imported so cannot be rewritten; pytest_perf_report"
    ":pytest.PytestAssertRewriteWarning"
)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return int(pytest.main(["-W", _REWRITE_WARNING_FILTER, "--perf-report", *args]))


if __name__ == "__main__":
    sys.exit(main())
