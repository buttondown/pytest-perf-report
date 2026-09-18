from pytest_perf_report.sqlshape import classify, normalize_sql


def test_pyformat_placeholders_collapse():
    assert (
        normalize_sql('SELECT * FROM "users" WHERE id = %s AND name = %(name)s')
        == 'SELECT * FROM "users" WHERE id = ? AND name = ?'
    )


def test_inline_literals_collapse():
    assert (
        normalize_sql("SELECT * FROM users WHERE name = 'bob' AND age > 21")
        == "SELECT * FROM users WHERE name = ? AND age > ?"
    )


def test_in_lists_collapse_to_one_placeholder():
    a = normalize_sql("SELECT 1 FROM t WHERE id IN (%s, %s, %s)")
    b = normalize_sql("SELECT 1 FROM t WHERE id IN (%s)")
    assert a == b == "SELECT ? FROM t WHERE id IN (?)"


def test_bulk_insert_tuples_collapse():
    a = normalize_sql("INSERT INTO t (a) VALUES (%s), (%s), (%s)")
    b = normalize_sql("INSERT INTO t (a) VALUES (%s)")
    assert a == b


def test_dollar_and_named_placeholders():
    assert normalize_sql("SELECT * FROM t WHERE a = $1 AND b = $2") == (
        "SELECT * FROM t WHERE a = ? AND b = ?"
    )
    assert normalize_sql("SELECT * FROM t WHERE a = :name") == (
        "SELECT * FROM t WHERE a = ?"
    )


def test_postgres_cast_survives():
    assert "::" in normalize_sql("SELECT a::text FROM t WHERE b = %s")


def test_classify():
    assert classify('SELECT * FROM "emails_newsletter" WHERE x = 1') == (
        "SELECT",
        "emails_newsletter",
    )
    assert classify("UPDATE users SET a = 1") == ("UPDATE", "users")
    assert classify("BEGIN") == ("BEGIN", "?")
