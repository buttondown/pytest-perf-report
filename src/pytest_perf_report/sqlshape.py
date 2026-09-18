"""SQL "shape" normalization: collapse a concrete statement into a template.

The same query issued with different bind values (or different IN-list
lengths) aggregates to one row in the report. Statements arrive either fully
parameterized (``%s`` / ``?`` / ``$1`` placeholders) or with inline literals
(raw SQL), so we defensively strip both. Database-agnostic by construction —
it never parses the SQL, only pattern-collapses it.
"""

import functools
import re

_PYFORMAT_RE = re.compile(r"%\(\w+\)s|%s")
_DOLLAR_RE = re.compile(r"\$\d+")
_NAMED_RE = re.compile(r"(?<![:\w]):\w+")
# Skips doubled-quote ('') and backslash (\') escapes; the latter matters for
# MySQL, whose default mode backslash-escapes quotes — without it a statement
# splits at the embedded quote and every literal value becomes its own shape.
_STRING_RE = re.compile(r"'(?:[^'\\]|''|\\.)*'")
_NUMBER_RE = re.compile(r"\b\d+\.?\d*\b")
_PAREN_LIST_RE = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")
_TUPLE_LIST_RE = re.compile(r"\(\?\)(?:\s*,\s*\(\?\))+")
_WS_RE = re.compile(r"\s+")
_TARGET_RE = re.compile(r'(?:FROM|JOIN|INTO|UPDATE)\s+("?[\w.]+"?)', re.IGNORECASE)

# Parameterized SQL repeats verbatim, so an LRU memo turns the regex pipeline
# into a cache hit on the hot path. Statements past this length (giant bulk
# INSERTs) are almost never verbatim repeats; skipping the cache for them
# avoids hashing megabytes per query and pinning them in memory.
_MEMO_MAX_SQL_LEN = 2_048


def normalize_sql(sql: str) -> str:
    """Reduce a concrete statement to its shape (placeholders + collapsed lists)."""
    if len(sql) > _MEMO_MAX_SQL_LEN:
        return _normalize(sql)
    return _normalize_cached(sql)


def _normalize(sql: str) -> str:
    s = _PYFORMAT_RE.sub("?", sql)
    s = _DOLLAR_RE.sub("?", s)
    s = _NAMED_RE.sub("?", s)
    s = _STRING_RE.sub("?", s)
    s = _NUMBER_RE.sub("?", s)
    s = _PAREN_LIST_RE.sub("(?)", s)  # (?, ?, ?) -> (?)
    s = _TUPLE_LIST_RE.sub("(?)", s)  # (?), (?), (?) -> (?)  (bulk INSERT VALUES)
    return _WS_RE.sub(" ", s).strip()


_normalize_cached = functools.lru_cache(maxsize=10_000)(_normalize)


def classify(sql: str) -> tuple[str, str]:
    """Return (operation, target_table) for coarse grouping, best-effort."""
    head = sql.strip().split(None, 1)
    op = head[0].upper() if head else "?"
    m = _TARGET_RE.search(sql)
    target = m.group(1).strip('"') if m else "?"
    return op, target
