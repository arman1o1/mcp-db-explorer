import re
from sqlglot import parse, exp


def is_safe_query(
    sql: str, allow_write: bool = False, dialect: str | None = None
) -> tuple[bool, str]:
    """
    Checks if a SQL query is safe to execute based on the allow_write flag.
    In read-only mode, only SELECT, EXPLAIN, SHOW, and DESCRIBE queries are permitted.
    Semicolon-separated multi-statement queries are blocked to prevent injection.

    Returns (is_safe, error_message).
    """
    if allow_write:
        return True, ""

    # Map database type/dialect names to sqlglot dialects
    sqlglot_dialect = None
    if dialect:
        dialect_lower = dialect.lower().strip()
        if dialect_lower in ("postgres", "postgresql"):
            sqlglot_dialect = "postgres"
        elif dialect_lower == "sqlite":
            sqlglot_dialect = "sqlite"

    try:
        # Normalize and strip query
        sql_stripped = sql.strip()
        if not sql_stripped:
            return False, "Empty query."

        # Parse statements, filtering out trailing/empty semicolons
        statements = [
            s
            for s in parse(sql_stripped, read=sqlglot_dialect)
            if s and not isinstance(s, exp.Semicolon)
        ]
        if not statements:
            return False, "No valid SQL statement detected."

        # Block multi-statements in read-only mode to prevent query chaining
        if len(statements) > 1:
            return False, "Multiple SQL statements are not allowed in read-only mode."

        stmt = statements[0]

        # Handle EXPLAIN queries which sqlglot parses as exp.Command
        if isinstance(stmt, exp.Command) and stmt.this.upper() == "EXPLAIN":
            if not stmt.expression or not hasattr(stmt.expression, "this"):
                return False, "Invalid EXPLAIN statement."
            inner_sql = stmt.expression.this.strip()

            # Recursively strip EXPLAIN options/modifiers from the inner query
            while True:
                upper_inner = inner_sql.upper().strip()
                if upper_inner.startswith("QUERY PLAN "):
                    inner_sql = inner_sql.strip()[11:]
                elif upper_inner.startswith("QUERY PLAN"):
                    inner_sql = inner_sql.strip()[10:]
                elif upper_inner.startswith("ANALYZE "):
                    inner_sql = inner_sql.strip()[8:]
                elif upper_inner.startswith("ANALYZE"):
                    inner_sql = inner_sql.strip()[7:]
                elif upper_inner.startswith("VERBOSE "):
                    inner_sql = inner_sql.strip()[8:]
                elif upper_inner.startswith("VERBOSE"):
                    inner_sql = inner_sql.strip()[7:]
                elif upper_inner.startswith("("):
                    match = re.match(r"^\s*\([^)]*\)\s*", inner_sql)
                    if match:
                        inner_sql = inner_sql[match.end() :]
                    else:
                        break
                else:
                    break
            return is_safe_query(inner_sql, allow_write, dialect)

        # Whitelist of allowed root statements (no exp.Explain)
        allowed_roots = (
            exp.Select,
            exp.Show,
            exp.Describe,
            exp.Union,
            exp.Subquery,
        )

        if not isinstance(stmt, allowed_roots):
            return (
                False,
                f"SQL statement type '{type(stmt).__name__}' is not allowed in read-only mode.",
            )

        # Walk the entire AST to ensure no nested DML/DDL operations (e.g., in subqueries or CTEs)
        forbidden_nodes = (
            exp.Insert,
            exp.Update,
            exp.Delete,
            exp.Drop,
            exp.Create,
            exp.Alter,
            exp.Merge,
            exp.Command,
            exp.Into,
            exp.Copy,
            exp.Grant,
            exp.Revoke,
            exp.LoadData,
            exp.TruncateTable,
        )

        for node in stmt.walk():
            # Skip checking the root statement itself
            if node is stmt:
                continue
            if isinstance(node, forbidden_nodes):
                return (
                    False,
                    f"Forbidden database modification operation '{type(node).__name__}' detected.",
                )

        return True, ""
    except Exception as e:
        return False, f"SQL parsing error: {str(e)}"
