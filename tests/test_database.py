import pytest
from sqlalchemy import text
from mcp_db_explorer.database import DatabaseManager


@pytest.fixture
def db_mgr():
    mgr = DatabaseManager()
    # Connect with readonly=False so we can create test tables
    success, msg = mgr.connect("sqlite", ":memory:", readonly=False)
    assert success

    # Create test tables and insert sample data
    with mgr.engine.connect() as conn:
        conn.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, age INTEGER)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, user_id INTEGER, FOREIGN KEY(user_id) REFERENCES users(id))"
            )
        )
        conn.execute(
            text(
                'CREATE TABLE "special-table" ("item-id" INTEGER PRIMARY KEY, details TEXT)'
            )
        )
        conn.execute(
            text(
                "CREATE VIEW active_users AS SELECT * FROM users WHERE age IS NOT NULL"
            )
        )
        conn.execute(text("INSERT INTO users (name, age) VALUES ('Alice', 30)"))
        conn.execute(text("INSERT INTO users (name, age) VALUES ('Bob', 25)"))
        conn.execute(text("INSERT INTO users (name, age) VALUES ('Charlie', NULL)"))
        conn.execute(
            text("INSERT INTO posts (title, user_id) VALUES ('Hello World', 1)")
        )
        conn.execute(text("INSERT INTO posts (title, user_id) VALUES ('Test Post', 1)"))
        conn.execute(
            text(
                'INSERT INTO "special-table" ("item-id", details) VALUES (1, \'Widget\')'
            )
        )
        conn.commit()

    return mgr


def test_list_tables(db_mgr):
    tables = db_mgr.list_tables()
    table_names = [t["table_name"] for t in tables]
    assert "users" in table_names
    assert "posts" in table_names


def test_describe_table(db_mgr):
    desc = db_mgr.describe_table("users")
    assert desc["table_name"] == "users"
    assert len(desc["columns"]) == 3

    col_names = [c["name"] for c in desc["columns"]]
    assert "id" in col_names
    assert "name" in col_names
    assert "age" in col_names

    # Check primary key
    id_col = next(c for c in desc["columns"] if c["name"] == "id")
    assert id_col["primary_key"] is True

    # Sample rows verification
    assert len(desc["sample_rows"]) == 3
    assert desc["sample_rows"][0]["name"] == "Alice"


def test_run_query(db_mgr):
    res = db_mgr.run_query("SELECT name FROM users WHERE age > 26")
    assert len(res) == 1
    assert res[0]["name"] == "Alice"


def test_explain_query_plan(db_mgr):
    plan = db_mgr.explain_query_plan("SELECT * FROM users WHERE id = 1")
    assert len(plan) > 0
    # SQLite explain output columns typically contain detail or similar
    headers = plan[0].keys()
    assert any(h in headers for h in ("detail", "opcode", "selectid"))


def test_get_table_stats(db_mgr):
    stats = db_mgr.get_table_stats("users")
    assert stats["total_rows"] == 3

    age_stats = stats["columns"]["age"]
    assert age_stats["null_count"] == 1
    assert age_stats["null_rate"] == round(1 / 3, 4)
    assert age_stats["distinct_count"] == 2
    assert age_stats["min"] == 25
    assert age_stats["max"] == 30
    assert age_stats["avg"] == 27.5


def test_generate_erd(db_mgr):
    erd = db_mgr.generate_erd()
    assert "erDiagram" in erd
    assert "users" in erd
    assert "posts" in erd
    assert "users ||--o{ posts" in erd
    # Verify primary key sanitization bug fix
    assert "specialtable" in erd
    assert "itemid PK" in erd


def test_schema_qualified_table(db_mgr):
    # Test describe_table with schema-qualified table name "main.users"
    desc = db_mgr.describe_table("main.users")
    assert desc["table_name"] == "main.users"
    assert len(desc["columns"]) == 3
    col_names = [c["name"] for c in desc["columns"]]
    assert "id" in col_names
    assert "name" in col_names
    assert "age" in col_names

    # Test get_table_stats with schema-qualified table name "main.users"
    stats = db_mgr.get_table_stats("main.users")
    assert stats["total_rows"] == 3
    assert stats["columns"]["age"]["min"] == 25


def test_sqlite_path_resolution(tmp_path):
    import os

    mgr = DatabaseManager()

    # Save current working directory
    old_cwd = os.getcwd()
    os.chdir(tmp_path)

    try:
        # Connect with relative path "relative_test.db"
        success, msg = mgr.connect("sqlite", "relative_test.db", readonly=False)
        assert success

        # Verify it resolves to the absolute path
        expected_path = os.path.abspath("relative_test.db").replace("\\", "/")
        assert expected_path in mgr.engine.url.database.replace("\\", "/")
    finally:
        os.chdir(old_cwd)


def test_view_support(db_mgr):
    # Test listing views
    tables = db_mgr.list_tables()
    table_names = [t["table_name"] for t in tables]
    assert "active_users" in table_names

    # Test describing view
    desc = db_mgr.describe_table("active_users")
    assert desc["table_name"] == "active_users"
    col_names = [c["name"] for c in desc["columns"]]
    assert "name" in col_names
    assert "age" in col_names

    # Test profiling view
    stats = db_mgr.get_table_stats("active_users")
    assert stats["total_rows"] == 2  # Charlie filtered out


def test_sqlite_readonly_bypass_prevention(tmp_path):
    from sqlalchemy import text

    db_file = tmp_path / "bypass_test.db"

    # Create the DB and a table
    mgr = DatabaseManager()
    success, msg = mgr.connect("sqlite", str(db_file), readonly=False)
    assert success
    with mgr.engine.connect() as conn:
        conn.execute(text("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO test (name) VALUES ('original')"))
        conn.commit()
    mgr.engine.dispose()

    # Try connection string with URI parameters to bypass read-only mode
    malicious_conn = f"file:///{db_file.as_posix()}?mode=rw&uri=true"
    mgr_readonly = DatabaseManager()
    success, msg = mgr_readonly.connect("sqlite", malicious_conn, readonly=True)
    assert success

    # Verify that attempting a write query fails because read-only mode was successfully enforced
    with pytest.raises(Exception):
        with mgr_readonly.engine.connect() as conn:
            conn.execute(text("INSERT INTO test (name) VALUES ('bypassed')"))
            conn.commit()
    mgr_readonly.engine.dispose()


def test_password_masking_in_errors():
    from mcp_db_explorer.database import mask_connection_string

    # 1. Direct test of the mask function
    url_with_pass = "postgresql://myuser:secretpassword123@invalidhost:5432/mydb"
    masked = mask_connection_string(url_with_pass)
    assert "secretpassword123" not in masked
    assert "postgresql://myuser:***@invalidhost:5432/mydb" == masked

    # 2. Test that connect() masks passwords in its output error message
    mgr = DatabaseManager()
    success, msg = mgr.connect("postgres", url_with_pass)
    assert not success
    assert "secretpassword123" not in msg


def test_sqlite_query_timeout():
    mgr = DatabaseManager()
    # Set timeout threshold to very low (0.1s) before connect
    mgr.sqlite_timeout = 0.1

    # Connect to in-memory sqlite database
    success, msg = mgr.connect("sqlite", ":memory:", readonly=False)
    assert success

    # Run a slow query (large recursive CTE)
    slow_query = """
    WITH RECURSIVE r(i) AS (
      VALUES(0)
      UNION ALL
      SELECT i+1 FROM r LIMIT 10000000
    )
    SELECT COUNT(*) FROM r
    """

    # Run query and verify it gets interrupted/aborted
    with pytest.raises(Exception) as excinfo:
        mgr.run_query(slow_query)

    # sqlite3 interrupted query raises OperationalError containing 'interrupted' or 'timeout'
    assert (
        "interrupted" in str(excinfo.value).lower()
        or "timeout" in str(excinfo.value).lower()
    )
    mgr.engine.dispose()


def test_get_table_stats_unified_fallback(db_mgr, monkeypatch):
    # We want to force the unified query to raise an exception,
    # making sure it falls back to column-by-column queries,
    # which should still succeed and return correct stats.
    with db_mgr.engine.connect() as conn:
        conn_class = conn.__class__
        original_execute = conn_class.execute

    def mock_execute(self, statement, *args, **kwargs):
        stmt_str = str(statement)
        # Identify unified query (has multiple COUNT, MIN, MAX or alias indicators)
        if "c_0_null_count" in stmt_str:
            raise RuntimeError("Forced unified query failure")
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(conn_class, "execute", mock_execute)

    stats = db_mgr.get_table_stats("users")
    assert stats["total_rows"] == 3

    age_stats = stats["columns"]["age"]
    assert age_stats["null_count"] == 1
    assert age_stats["null_rate"] == round(1 / 3, 4)
    assert age_stats["distinct_count"] == 2


def test_password_masking_edge_cases():
    from mcp_db_explorer.database import mask_connection_string

    # Empty username
    assert (
        mask_connection_string("postgresql://:secretpass@host:5432/mydb")
        == "postgresql://:***@host:5432/mydb"
    )

    # No colon (password only)
    assert (
        mask_connection_string("postgresql://secretpass@host:5432/mydb")
        == "postgresql://***@host:5432/mydb"
    )

    # Multiple URLs in text
    text_val = "Error on postgresql://usr:pwd@host/db and postgresql://:pwd@host/db"
    expected = "Error on postgresql://usr:***@host/db and postgresql://:***@host/db"
    assert mask_connection_string(text_val) == expected


def test_sqlite_path_restriction(tmp_path):
    mgr = DatabaseManager()
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    outside = tmp_path / "outside.db"
    inside = allowed / "inside.db"

    # Test connecting outside allowed directory fails
    success, msg = mgr.connect("sqlite", str(outside), allowed_dir=str(allowed))
    assert not success
    assert "Access denied" in msg

    # Test connecting inside allowed directory succeeds
    success, msg = mgr.connect(
        "sqlite", str(inside), readonly=False, allowed_dir=str(allowed)
    )
    assert success, msg
    mgr.engine.dispose()

    # Test connecting to in-memory succeeds
    success, msg = mgr.connect("sqlite", ":memory:", allowed_dir=str(allowed))
    assert success
    mgr.engine.dispose()
