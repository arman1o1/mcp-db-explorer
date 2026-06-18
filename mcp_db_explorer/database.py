import os
import pathlib
import re
import time
from typing import Any, Dict, List, Tuple, Optional
from sqlalchemy import create_engine, inspect, text, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


def mask_connection_string(s: str) -> str:
    """Masks passwords in connection strings (e.g., postgresql://user:***@host/db)."""

    def replace_match(match):
        prefix = match.group(1)
        userinfo = match.group(2)
        suffix = match.group(3)
        if ":" in userinfo:
            parts = userinfo.split(":", 1)
            return f"{prefix}{parts[0]}:***{suffix}"
        else:
            return f"{prefix}***{suffix}"

    return re.sub(r"([a-zA-Z0-9+.-]+://)([^@/]+)(@)", replace_match, s)


class DatabaseManager:
    def __init__(self):
        self.engine: Optional[Engine] = None
        self.db_type: Optional[str] = None
        self.connection_string: Optional[str] = None
        self.readonly: bool = True
        self.sqlite_timeout: float = 30.0

    def connect(
        self,
        db_type: str,
        connection_string: str,
        readonly: bool = True,
        allowed_dir: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Establishes a connection to the database.
        Supports 'sqlite' and 'postgres'/'postgresql'.
        """
        self.readonly = readonly
        db_type = db_type.lower().strip()

        # Normalize connection string and db_type
        if db_type == "sqlite":
            # Strip sqlite: and file: prefixes and any leading slashes cleanly
            clean_conn = re.sub(
                r"^sqlite:/{0,4}", "", connection_string, flags=re.IGNORECASE
            )
            clean_conn = re.sub(r"^file:/{0,4}", "", clean_conn, flags=re.IGNORECASE)
            # Strip any query parameters to prevent parameter injection
            clean_conn = clean_conn.split("?")[0]

            # If in-memory
            if clean_conn == ":memory:":
                conn_url = "sqlite:///:memory:"
            else:
                # Resolve relative path to absolute posix path
                resolved_path = pathlib.Path(os.path.abspath(clean_conn))
                clean_conn = resolved_path.as_posix()

                if allowed_dir:
                    allowed_path = pathlib.Path(os.path.abspath(allowed_dir))
                    try:
                        resolved_path.relative_to(allowed_path)
                    except ValueError:
                        return (
                            False,
                            f"Access denied: SQLite database path '{clean_conn}' is outside the allowed directory '{allowed_path.as_posix()}'.",
                        )

                # If read-only SQLite, use mode=ro URI format
                if readonly:
                    conn_url = f"sqlite:///file:{clean_conn}?mode=ro&uri=true"
                else:
                    conn_url = f"sqlite:///{clean_conn}"
        elif db_type in ("postgres", "postgresql"):
            db_type = "postgresql"
            conn_url = connection_string
            if not conn_url.startswith("postgresql://") and not conn_url.startswith(
                "postgresql+psycopg2://"
            ):
                # Ensure correct driver prefix
                if conn_url.startswith("postgres://"):
                    conn_url = conn_url.replace("postgres://", "postgresql://", 1)
                else:
                    return (
                        False,
                        "Invalid PostgreSQL connection string. Must start with postgres:// or postgresql://",
                    )
        else:
            return (
                False,
                f"Unsupported database type '{db_type}'. Supported: sqlite, postgres",
            )

        try:
            # Create engine
            if db_type == "sqlite" and ":memory:" in conn_url:
                from sqlalchemy.pool import StaticPool

                new_engine = create_engine(
                    conn_url,
                    poolclass=StaticPool,
                    connect_args={"check_same_thread": False, "timeout": 30.0},
                )
            elif db_type == "sqlite":
                new_engine = create_engine(
                    conn_url,
                    connect_args={"timeout": 30.0},
                )
            else:
                new_engine = create_engine(conn_url)

            # Enforce read-only and statement timeout at database level for Postgres
            if db_type == "postgresql":

                @event.listens_for(new_engine, "connect")
                def set_postgres_options(dbapi_connection, connection_record):
                    if readonly:
                        dbapi_connection.readonly = True
                    cursor = dbapi_connection.cursor()
                    try:
                        cursor.execute("SET statement_timeout = 30000")
                    finally:
                        cursor.close()

            # Register SQLite event listeners for timeouts (DoS protection)
            if db_type == "sqlite":

                @event.listens_for(new_engine, "before_execute")
                def set_query_start_time(
                    conn, clauseelement, multiparams, params, execution_options
                ):
                    if hasattr(conn.connection, "info"):
                        conn.connection.info["query_start_time"] = time.time()

                @event.listens_for(new_engine, "connect")
                def set_sqlite_timeout(dbapi_connection, connection_record):
                    def progress_handler():
                        query_start = connection_record.info.get("query_start_time")
                        if query_start is not None:
                            if time.time() - query_start > self.sqlite_timeout:
                                raise RuntimeError(
                                    f"Query timed out (limit {self.sqlite_timeout}s)"
                                )
                        return 0

                    dbapi_connection.set_progress_handler(progress_handler, 1000)

            # Test connection
            with new_engine.connect() as conn:
                conn.execute(text("SELECT 1"))

            # Save state on success
            if self.engine:
                self.engine.dispose()
            self.engine = new_engine
            self.db_type = db_type
            self.connection_string = connection_string
            return True, "Successfully connected to the database."

        except Exception as e:
            # Mask sensitive URI credentials in error messages
            err_msg = mask_connection_string(str(e))
            return False, f"Connection failed: {err_msg}"

    def check_connection(self) -> None:
        if not self.engine:
            raise RuntimeError("Database is not connected. Use connect_database first.")

    def _parse_table_name(self, table_name: str) -> Tuple[Optional[str], str]:
        if "." in table_name:
            parts = table_name.split(".", 1)
            return parts[0].strip(), parts[1].strip()
        return None, table_name.strip()

    def list_tables(self) -> List[Dict[str, Any]]:
        """
        Lists all tables and views in the database along with their comments.
        """
        self.check_connection()
        inspector = inspect(self.engine)
        tables = []

        for table_name in inspector.get_table_names():
            # Get table comment/description if supported
            comment = None
            try:
                comment_dict = inspector.get_table_comment(table_name)
                comment = comment_dict.get("text")
            except Exception:
                pass

            tables.append({"table_name": table_name, "comment": comment})

        for view_name in inspector.get_view_names():
            # Get view comment/description if supported
            comment = None
            try:
                comment_dict = inspector.get_table_comment(view_name)
                comment = comment_dict.get("text")
            except Exception:
                pass

            tables.append({"table_name": view_name, "comment": comment})

        return tables

    def describe_table(self, table_name: str) -> Dict[str, Any]:
        """
        Returns full schema information for a table and 5 sample rows.
        """
        self.check_connection()
        schema, local_table_name = self._parse_table_name(table_name)
        inspector = inspect(self.engine)

        # Verify table or view exists
        all_objects = inspector.get_table_names(
            schema=schema
        ) + inspector.get_view_names(schema=schema)
        if local_table_name not in all_objects:
            raise ValueError(f"Table or View '{table_name}' does not exist.")

        columns = inspector.get_columns(local_table_name, schema=schema)
        pk = inspector.get_pk_constraint(local_table_name, schema=schema)
        fkeys = inspector.get_foreign_keys(local_table_name, schema=schema)
        indexes = inspector.get_indexes(local_table_name, schema=schema)

        # Quote table name using dialect preparer
        preparer = self.engine.dialect.identifier_preparer
        if schema:
            quoted_name = f"{preparer.quote(schema)}.{preparer.quote(local_table_name)}"
        else:
            quoted_name = preparer.quote(local_table_name)

        # Retrieve 5 sample rows
        sample_rows = []
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(f"SELECT * FROM {quoted_name} LIMIT 5"))
                # Convert rows to dicts
                keys = result.keys()
                for row in result:
                    sample_rows.append(dict(zip(keys, row)))
        except Exception as e:
            sample_rows = [f"Failed to fetch sample rows: {str(e)}"]

        # Format column details
        cols_info = []
        for col in columns:
            cols_info.append(
                {
                    "name": col["name"],
                    "type": str(col["type"]),
                    "nullable": col["nullable"],
                    "default": str(col["default"])
                    if col.get("default") is not None
                    else None,
                    "primary_key": col["name"] in pk.get("constrained_columns", []),
                }
            )

        return {
            "table_name": table_name,
            "columns": cols_info,
            "primary_key": pk.get("constrained_columns", []),
            "foreign_keys": fkeys,
            "indexes": [
                {
                    "name": idx["name"],
                    "columns": idx["column_names"],
                    "unique": idx["unique"],
                }
                for idx in indexes
            ],
            "sample_rows": sample_rows,
        }

    def run_query(self, sql: str, max_rows: int = 500) -> List[Dict[str, Any]]:
        """
        Executes a SQL query and returns results as a list of dicts.
        """
        self.check_connection()

        try:
            with self.engine.connect() as conn:
                # Set execution timeout if postgres
                if self.db_type == "postgresql":
                    conn.execute(text("SET statement_timeout = 30000"))  # 30s timeout

                result = conn.execute(text(sql))

                # Check if query returns rows (SELECT/EXPLAIN etc.)
                if result.returns_rows:
                    rows = []
                    keys = result.keys()
                    for i, row in enumerate(result):
                        if i >= max_rows:
                            break
                        # Convert values to string or JSON-serializable types
                        row_dict = {}
                        for key, val in zip(keys, row):
                            # Convert bytes or complex types to string representation
                            if isinstance(val, bytes):
                                row_dict[key] = val.hex()
                            else:
                                row_dict[key] = val
                        rows.append(row_dict)
                    return rows
                else:
                    return [{"row_count_affected": result.rowcount}]
        except SQLAlchemyError as e:
            raise RuntimeError(f"Database query failed: {str(e)}")

    def explain_query_plan(self, sql: str) -> List[Dict[str, Any]]:
        """
        Runs EXPLAIN on the SQL query and returns the plan.
        """
        self.check_connection()
        dialect = self.engine.dialect.name

        if dialect == "sqlite":
            explain_sql = f"EXPLAIN QUERY PLAN {sql}"
        elif dialect == "postgresql":
            explain_sql = f"EXPLAIN {sql}"
        else:
            explain_sql = f"EXPLAIN {sql}"

        try:
            return self.run_query(explain_sql)
        except Exception as e:
            raise RuntimeError(f"Failed to explain query plan: {str(e)}")

    def get_table_stats(self, table_name: str) -> Dict[str, Any]:
        """
        Profiles a table to return statistics:
        Row count, null rates, cardinality, and numeric ranges.
        """
        self.check_connection()
        schema, local_table_name = self._parse_table_name(table_name)
        inspector = inspect(self.engine)

        all_objects = inspector.get_table_names(
            schema=schema
        ) + inspector.get_view_names(schema=schema)
        if local_table_name not in all_objects:
            raise ValueError(f"Table or View '{table_name}' does not exist.")

        columns = inspector.get_columns(local_table_name, schema=schema)
        preparer = self.engine.dialect.identifier_preparer
        if schema:
            quoted_table = (
                f"{preparer.quote(schema)}.{preparer.quote(local_table_name)}"
            )
        else:
            quoted_table = preparer.quote(local_table_name)

        # 1. Get total row count
        try:
            with self.engine.connect() as conn:
                count_res = conn.execute(
                    text(f"SELECT COUNT(*) FROM {quoted_table}")
                ).scalar()
                total_rows = count_res if count_res is not None else 0
        except Exception as e:
            raise RuntimeError(f"Failed to get table row count: {str(e)}")

        if total_rows == 0:
            return {
                "table_name": table_name,
                "total_rows": 0,
                "columns": {
                    col["name"]: {
                        "null_count": 0,
                        "null_rate": 0.0,
                        "distinct_count": 0,
                        "cardinality_ratio": 0.0,
                    }
                    for col in columns
                },
            }

        # 2. Build stats query dynamically
        stats = {}
        unified_success = False
        select_items = []
        col_mappings = {}

        for idx, col in enumerate(columns):
            col_name = col["name"]
            col_type = col["type"]
            quoted_col = preparer.quote(col_name)

            # Check if column type is numeric
            is_numeric = False
            type_str = str(col_type).lower()
            if any(
                t in type_str
                for t in ("int", "float", "double", "decimal", "numeric", "real")
            ):
                is_numeric = True

            aliases = {
                "null_count": f"c_{idx}_null_count",
                "distinct_count": f"c_{idx}_distinct_count",
            }
            select_items.append(
                f"COUNT(*) - COUNT({quoted_col}) AS {aliases['null_count']}"
            )
            select_items.append(
                f"COUNT(DISTINCT {quoted_col}) AS {aliases['distinct_count']}"
            )

            if is_numeric:
                aliases["min_val"] = f"c_{idx}_min_val"
                aliases["max_val"] = f"c_{idx}_max_val"
                aliases["avg_val"] = f"c_{idx}_avg_val"
                select_items.append(f"MIN({quoted_col}) AS {aliases['min_val']}")
                select_items.append(f"MAX({quoted_col}) AS {aliases['max_val']}")
                select_items.append(f"AVG({quoted_col}) AS {aliases['avg_val']}")

            col_mappings[col_name] = {
                "aliases": aliases,
                "is_numeric": is_numeric,
            }

        if select_items:
            unified_query = f"SELECT {', '.join(select_items)} FROM {quoted_table}"
            try:
                with self.engine.connect() as conn:
                    res = conn.execute(text(unified_query)).mappings().first()
                    if res:
                        for col_name, mapping in col_mappings.items():
                            aliases = mapping["aliases"]
                            is_numeric = mapping["is_numeric"]

                            null_cnt = res[aliases["null_count"]]
                            dist_cnt = res[aliases["distinct_count"]]
                            col_stats = {
                                "null_count": null_cnt,
                                "null_rate": round(null_cnt / total_rows, 4),
                                "distinct_count": dist_cnt,
                                "cardinality_ratio": round(dist_cnt / total_rows, 4),
                            }
                            if is_numeric:
                                col_stats.update(
                                    {
                                        "min": res[aliases["min_val"]],
                                        "max": res[aliases["max_val"]],
                                        "avg": round(res[aliases["avg_val"]], 4)
                                        if res[aliases["avg_val"]] is not None
                                        else None,
                                    }
                                )
                            stats[col_name] = col_stats
                        unified_success = True
            except Exception:
                # Fall back to column-by-column if unified query fails (e.g. custom types)
                pass

        if not unified_success:
            stats = {}
            for col in columns:
                col_name = col["name"]
                col_type = col["type"]
                quoted_col = preparer.quote(col_name)

                # Check if column type is numeric
                is_numeric = False
                type_str = str(col_type).lower()
                if any(
                    t in type_str
                    for t in ("int", "float", "double", "decimal", "numeric", "real")
                ):
                    is_numeric = True

                # Build queries
                select_items = [
                    f"COUNT(*) - COUNT({quoted_col}) AS null_count",
                    f"COUNT(DISTINCT {quoted_col}) AS distinct_count",
                ]

                if is_numeric:
                    select_items.extend(
                        [
                            f"MIN({quoted_col}) AS min_val",
                            f"MAX({quoted_col}) AS max_val",
                            f"AVG({quoted_col}) AS avg_val",
                        ]
                    )

                stats_query = f"SELECT {', '.join(select_items)} FROM {quoted_table}"

                try:
                    with self.engine.connect() as conn:
                        res = conn.execute(text(stats_query)).mappings().first()
                        if res:
                            null_cnt = res["null_count"]
                            dist_cnt = res["distinct_count"]
                            col_stats = {
                                "null_count": null_cnt,
                                "null_rate": round(null_cnt / total_rows, 4),
                                "distinct_count": dist_cnt,
                                "cardinality_ratio": round(dist_cnt / total_rows, 4),
                            }
                            if is_numeric:
                                col_stats.update(
                                    {
                                        "min": res["min_val"],
                                        "max": res["max_val"],
                                        "avg": round(res["avg_val"], 4)
                                        if res["avg_val"] is not None
                                        else None,
                                    }
                                )
                            stats[col_name] = col_stats
                except Exception as e:
                    # Fallback for complex column types or query errors
                    stats[col_name] = {"error": f"Failed to compute stats: {str(e)}"}

        return {"table_name": table_name, "total_rows": total_rows, "columns": stats}

    def generate_erd(self) -> str:
        """
        Generates a Mermaid.js Entity Relationship diagram.
        """
        self.check_connection()
        inspector = inspect(self.engine)
        tables = inspector.get_table_names()

        erd_lines = ["erDiagram"]

        # Add tables and their attributes
        for table in tables:
            t_clean = re.sub(r"[^a-zA-Z0-9_]", "", table)
            erd_lines.append(f"    {t_clean} {{")
            columns = inspector.get_columns(table)
            pk = inspector.get_pk_constraint(table)
            pk_cols = pk.get("constrained_columns", [])

            for col in columns:
                orig_col_name = col["name"]
                key_type = "PK" if orig_col_name in pk_cols else ""

                # Simplify type representation for Mermaid
                type_str = str(col["type"]).split("(")[0].replace(" ", "_")

                # Clean characters for Mermaid
                type_str = re.sub(r"[^a-zA-Z0-9_]", "", type_str)
                col_name = re.sub(r"[^a-zA-Z0-9_]", "", orig_col_name)

                erd_lines.append(f"        {type_str} {col_name} {key_type}")
            erd_lines.append("    }")

        # Add relationships
        for table in tables:
            fkeys = inspector.get_foreign_keys(table)
            for fk in fkeys:
                referred_table = fk["referred_table"]
                constrained_cols = fk["constrained_columns"]
                referred_cols = fk["referred_columns"]

                col_map = "_".join(
                    f"{c}_{r}" for c, r in zip(constrained_cols, referred_cols)
                )
                # Sanitize table names for Mermaid
                t1 = re.sub(r"[^a-zA-Z0-9_]", "", referred_table)
                t2 = re.sub(r"[^a-zA-Z0-9_]", "", table)
                col_map = re.sub(r"[^a-zA-Z0-9_]", "", col_map)

                # referred_table ||--o{ table : col_map
                erd_lines.append(f"    {t1} ||--o{{ {t2} : {col_map}")

        return "\n".join(erd_lines)
