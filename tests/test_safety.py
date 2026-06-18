from mcp_db_explorer.safety import is_safe_query


def test_safe_queries():
    # Simple SELECT
    safe, msg = is_safe_query("SELECT * FROM users")
    assert safe

    # EXPLAIN
    safe, msg = is_safe_query("EXPLAIN QUERY PLAN SELECT * FROM users")
    assert safe

    # CTE
    safe, msg = is_safe_query("WITH cte AS (SELECT 1) SELECT * FROM cte")
    assert safe

    # UNION
    safe, msg = is_safe_query("SELECT 1 UNION SELECT 2")
    assert safe

    # Parenthesized queries
    safe, msg = is_safe_query("(SELECT * FROM users)")
    assert safe, msg

    safe, msg = is_safe_query("((SELECT 1))")
    assert safe, msg


def test_unsafe_queries():
    # INSERT
    safe, msg = is_safe_query("INSERT INTO users (name) VALUES ('Alice')")
    assert not safe
    assert "Forbidden database modification" in msg or "statement type" in msg

    # DELETE
    safe, msg = is_safe_query("DELETE FROM users WHERE id = 1")
    assert not safe
    assert "Forbidden database modification" in msg or "statement type" in msg

    # DROP
    safe, msg = is_safe_query("DROP TABLE users")
    assert not safe
    assert "Forbidden database modification" in msg or "statement type" in msg

    # Multi-statement (semicolon separation)
    safe, msg = is_safe_query("SELECT 1; SELECT 2")
    assert not safe
    assert "Multiple SQL statements" in msg

    # Injection try
    safe, msg = is_safe_query("SELECT * FROM users; DROP TABLE users")
    assert not safe
    assert "Multiple SQL statements" in msg


def test_allow_write_override():
    # Write allowed
    safe, msg = is_safe_query(
        "INSERT INTO users (name) VALUES ('Alice')", allow_write=True
    )
    assert safe

    safe, msg = is_safe_query("DROP TABLE users", allow_write=True)
    assert safe


def test_semicolon_with_comments():
    # Semicolon followed by comments should be allowed
    safe, msg = is_safe_query("SELECT 1; -- trailing comment")
    assert safe, msg

    safe, msg = is_safe_query("SELECT * FROM users;    /* block comment */")
    assert safe, msg


def test_pg_explain_options():
    # Advanced EXPLAIN statements with Postgres options should be parsed and allowed
    safe, msg = is_safe_query("EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM users")
    assert safe, msg

    safe, msg = is_safe_query("EXPLAIN ANALYZE VERBOSE SELECT * FROM users")
    assert safe, msg

    # Destructive operations in EXPLAIN should still be blocked
    safe, msg = is_safe_query("EXPLAIN (ANALYZE) DELETE FROM users")
    assert not safe
    assert "Forbidden database modification" in msg or "statement type" in msg


def test_select_into_blocked():
    # SELECT INTO should be blocked as a write operation
    safe, msg = is_safe_query("SELECT * INTO new_table FROM users")
    assert not safe
    assert "Forbidden database modification" in msg


def test_other_blocked_dml_ddl():
    # Test TRUNCATE, GRANT, REVOKE, COPY, LOAD DATA
    safe, msg = is_safe_query("TRUNCATE TABLE users")
    assert not safe
    assert "statement type" in msg or "Forbidden database modification" in msg

    safe, msg = is_safe_query("GRANT SELECT ON users TO guest")
    assert not safe
    assert "statement type" in msg or "Forbidden database modification" in msg

    safe, msg = is_safe_query("REVOKE INSERT ON users FROM public")
    assert not safe
    assert "statement type" in msg or "Forbidden database modification" in msg

    safe, msg = is_safe_query("COPY users TO stdout")
    assert not safe
    assert "statement type" in msg or "Forbidden database modification" in msg

    safe, msg = is_safe_query("LOAD DATA INFILE 'data.txt' INTO TABLE users")
    assert not safe
    assert any(
        x in msg
        for x in ("statement type", "Forbidden database modification", "parsing error")
    )


def test_dialect_specific_parsing():
    # PostgreSQL dollar-quoted string syntax should be parsed correctly when postgres dialect is specified
    safe, msg = is_safe_query("SELECT $$hello$$", dialect="postgres")
    assert safe, msg

    # Destructive PL/pgSQL block (DO statement) should be blocked when postgres dialect is specified
    safe, msg = is_safe_query(
        "DO $$ BEGIN INSERT INTO users VALUES (1); END $$", dialect="postgres"
    )
    assert not safe
    assert "statement type" in msg or "Forbidden database modification" in msg
