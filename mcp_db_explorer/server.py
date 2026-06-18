import argparse
import asyncio
import logging
import os
import sys
import mcp.types as types
from mcp.server import Server, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.server.models import InitializationOptions

from mcp_db_explorer.database import DatabaseManager, mask_connection_string
from mcp_db_explorer.safety import is_safe_query
from mcp_db_explorer.prompts import PROMPTS, render_prompt

logger = logging.getLogger("mcp-db-explorer")
server = Server("mcp-db-explorer")

# Global instances
db_manager = DatabaseManager()
allow_writes_globally = False
allowed_database_dir = None


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available database exploration and analysis tools."""
    return [
        types.Tool(
            name="connect_database",
            description=(
                "Connect to a SQLite or PostgreSQL database. "
                "For SQLite, connection_string can be a file path (e.g. 'dev.db') or ':memory:'. "
                "For Postgres, connection_string must be a valid PostgreSQL connection URI. "
                "Resolves relative paths for SQLite databases against the current working directory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "db_type": {
                        "type": "string",
                        "enum": ["sqlite", "postgres", "postgresql"],
                        "description": "The type of database engine.",
                    },
                    "connection_string": {
                        "type": "string",
                        "description": "Connection URI or file path to connect to.",
                    },
                },
                "required": ["db_type", "connection_string"],
            },
        ),
        types.Tool(
            name="list_tables",
            description="List all table names and their descriptions in the connected database.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="describe_table",
            description="Retrieve table columns, data types, primary key status, foreign keys, indexes, and a sample of 5 rows.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "The name of the table to describe.",
                    }
                },
                "required": ["table_name"],
            },
        ),
        types.Tool(
            name="run_query",
            description="Execute a SQL query against the database. Checked for safety. Read-only SELECT by default.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SQL query string to run.",
                    }
                },
                "required": ["sql"],
            },
        ),
        types.Tool(
            name="explain_query_plan",
            description="Run EXPLAIN on a SQL query to inspect the database execution path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SQL query string to analyze.",
                    }
                },
                "required": ["sql"],
            },
        ),
        types.Tool(
            name="get_table_stats",
            description="Profile a table to compute row count, null rates, cardinality, and numeric ranges (min/max/average).",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "The name of the table to profile.",
                    }
                },
                "required": ["table_name"],
            },
        ),
        types.Tool(
            name="generate_erd",
            description="Generate a Mermaid.js Entity Relationship Diagram (ERD) based on tables and foreign keys.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent]:
    """Routes tool execution requests to the database layer with safety checks."""
    if not arguments:
        arguments = {}

    try:
        if name == "connect_database":
            db_type = arguments["db_type"]
            connection_string = arguments["connection_string"]
            success, msg = await asyncio.to_thread(
                db_manager.connect,
                db_type,
                connection_string,
                readonly=not allow_writes_globally,
                allowed_dir=allowed_database_dir,
            )
            return [types.TextContent(type="text", text=msg)]

        elif name == "list_tables":
            tables = await asyncio.to_thread(db_manager.list_tables)
            if not tables:
                return [
                    types.TextContent(
                        type="text", text="No tables found in the database."
                    )
                ]

            md = ["### Database Tables\n"]
            for t in tables:
                comment_str = f" - *{t['comment']}*" if t["comment"] else ""
                md.append(f"- `{t['table_name']}`{comment_str}")
            return [types.TextContent(type="text", text="\n".join(md))]

        elif name == "describe_table":
            table_name = arguments["table_name"]
            info = await asyncio.to_thread(db_manager.describe_table, table_name)

            md = [f"### Table Schema: `{table_name}`\n"]
            md.append("| Column | Type | Primary Key | Nullable | Default |")
            md.append("|---|---|---|---|---|")
            for col in info["columns"]:
                pk = "✅" if col["primary_key"] else ""
                null_ok = "✅" if col["nullable"] else "❌"
                default_str = f"`{col['default']}`" if col["default"] else "-"
                md.append(
                    f"| `{col['name']}` | `{col['type']}` | {pk} | {null_ok} | {default_str} |"
                )

            if info["foreign_keys"]:
                md.append("\n#### Foreign Keys")
                for fk in info["foreign_keys"]:
                    c_cols = ", ".join(fk["constrained_columns"])
                    r_cols = ", ".join(fk["referred_columns"])
                    md.append(f"- `{c_cols}` -> `{fk['referred_table']}({r_cols})`")

            if info["indexes"]:
                md.append("\n#### Indexes")
                for idx in info["indexes"]:
                    u_str = " (UNIQUE)" if idx["unique"] else ""
                    md.append(
                        f"- `{idx['name']}`: on columns `{', '.join(idx['columns'])}`{u_str}"
                    )

            if info["sample_rows"]:
                md.append("\n#### Sample Rows (up to 5)")
                if isinstance(info["sample_rows"][0], str):
                    md.append(info["sample_rows"][0])
                else:
                    headers = list(info["sample_rows"][0].keys())
                    md.append("| " + " | ".join(headers) + " |")
                    md.append("| " + " | ".join("---" for _ in headers) + " |")
                    for row in info["sample_rows"]:
                        vals = [
                            str(row.get(h)) if row.get(h) is not None else "NULL"
                            for h in headers
                        ]
                        md.append("| " + " | ".join(vals) + " |")
            return [types.TextContent(type="text", text="\n".join(md))]

        elif name == "run_query":
            sql = arguments["sql"]
            # Enforce safety
            is_safe, error_msg = is_safe_query(
                sql, allow_write=allow_writes_globally, dialect=db_manager.db_type
            )
            if not is_safe:
                return [
                    types.TextContent(type="text", text=f"Query rejected: {error_msg}")
                ]

            results = await asyncio.to_thread(db_manager.run_query, sql)
            if not results:
                return [
                    types.TextContent(
                        type="text",
                        text="Query executed successfully but returned no results.",
                    )
                ]

            # If query was a write query (in allow_write mode) and just returns row count
            if "row_count_affected" in results[0] and len(results) == 1:
                return [
                    types.TextContent(
                        type="text",
                        text=f"Query executed successfully. Rows affected: {results[0]['row_count_affected']}",
                    )
                ]

            # Render tabular results in markdown
            headers = list(results[0].keys())
            md = []
            md.append("| " + " | ".join(headers) + " |")
            md.append("| " + " | ".join("---" for _ in headers) + " |")
            for row in results:
                vals = [
                    str(row.get(h)) if row.get(h) is not None else "NULL"
                    for h in headers
                ]
                md.append("| " + " | ".join(vals) + " |")
            return [types.TextContent(type="text", text="\n".join(md))]

        elif name == "explain_query_plan":
            sql = arguments["sql"]
            is_safe, error_msg = is_safe_query(
                sql, allow_write=allow_writes_globally, dialect=db_manager.db_type
            )
            if not is_safe:
                return [
                    types.TextContent(
                        type="text", text=f"Explain rejected: {error_msg}"
                    )
                ]

            plan = await asyncio.to_thread(db_manager.explain_query_plan, sql)
            if not plan:
                return [types.TextContent(type="text", text="No query plan generated.")]

            headers = list(plan[0].keys())
            md = ["### Query Execution Plan\n"]
            md.append("| " + " | ".join(headers) + " |")
            md.append("| " + " | ".join("---" for _ in headers) + " |")
            for row in plan:
                vals = [str(row.get(h, "")) for h in headers]
                md.append("| " + " | ".join(vals) + " |")
            return [types.TextContent(type="text", text="\n".join(md))]

        elif name == "get_table_stats":
            table_name = arguments["table_name"]
            stats = await asyncio.to_thread(db_manager.get_table_stats, table_name)

            md = [f"### Table Profiling Stats: `{table_name}`\n"]
            md.append(f"**Total Rows**: {stats['total_rows']}\n")
            md.append(
                "| Column | Null Count | Null Rate | Distinct Count | Card. Ratio | Numeric Stats (Min / Max / Avg) |"
            )
            md.append("|---|---|---|---|---|---|")
            for col_name, col_stats in stats["columns"].items():
                if "error" in col_stats:
                    md.append(
                        f"| `{col_name}` | *Error* | - | - | - | *{col_stats['error']}* |"
                    )
                    continue
                null_cnt = col_stats["null_count"]
                null_rt = f"{col_stats['null_rate'] * 100:.2f}%"
                dist_cnt = col_stats["distinct_count"]
                card_ratio = f"{col_stats['cardinality_ratio'] * 100:.2f}%"
                num_stats = "-"
                if "min" in col_stats:
                    num_stats = f"Min: {col_stats['min']} | Max: {col_stats['max']} | Avg: {col_stats['avg']}"
                md.append(
                    f"| `{col_name}` | {null_cnt} | {null_rt} | {dist_cnt} | {card_ratio} | {num_stats} |"
                )
            return [types.TextContent(type="text", text="\n".join(md))]

        elif name == "generate_erd":
            erd = await asyncio.to_thread(db_manager.generate_erd)
            return [types.TextContent(type="text", text=f"```mermaid\n{erd}\n```")]

        else:
            raise ValueError(f"Unknown tool name: {name}")
    except Exception as e:
        err_msg = mask_connection_string(str(e))
        logger.error(f"Error executing tool {name}: {err_msg}")
        return [
            types.TextContent(
                type="text", text=f"Error executing tool {name}: {err_msg}"
            )
        ]


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    """Exposes current database schema info as a browseable resource."""
    if not db_manager.engine:
        return []
    return [
        types.Resource(
            uri="schema://info",
            name="Current Database Schema Info",
            mimeType="text/plain",
            description="Exposes current schema details including tables, columns, and foreign keys.",
        )
    ]


@server.read_resource()
async def handle_read_resource(uri: str) -> list[types.TextResourceContents]:
    """Reads the current database schema structure."""
    if uri != "schema://info":
        raise ValueError(f"Unknown resource URI: {uri}")

    try:
        db_manager.check_connection()
        tables = await asyncio.to_thread(db_manager.list_tables)

        lines = ["Current Database Schema:\n"]
        for t in tables:
            t_name = t["table_name"]
            comment_str = f" ({t['comment']})" if t["comment"] else ""
            lines.append(f"Table: {t_name}{comment_str}")
            desc = await asyncio.to_thread(db_manager.describe_table, t_name)
            for col in desc["columns"]:
                pk_str = " (PK)" if col["primary_key"] else ""
                nullable_str = " (NULL)" if col["nullable"] else " (NOT NULL)"
                lines.append(f"  - {col['name']} ({col['type']}){pk_str}{nullable_str}")
            for fk in desc["foreign_keys"]:
                lines.append(
                    f"  - FK: {fk['constrained_columns']} -> {fk['referred_table']}({fk['referred_columns']})"
                )
            lines.append("")

        return [
            types.TextResourceContents(
                uri=uri, mimeType="text/plain", text="\n".join(lines)
            )
        ]
    except Exception as e:
        raise RuntimeError(mask_connection_string(str(e)))


@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    """List pre-built analytical and sql generation prompts."""
    return PROMPTS


@server.get_prompt()
async def handle_get_prompt(name: str, arguments: dict | None) -> types.GetPromptResult:
    """Renders the requested prompt with arguments."""
    if not arguments:
        arguments = {}
    return render_prompt(name, arguments)


async def run_server():
    """Starts the standard stdio server loop."""
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="mcp-db-explorer",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        if db_manager.engine:
            db_manager.engine.dispose()
            logger.info("Database engine connections disposed.")


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="AI-Powered Database Explorer MCP Server"
    )
    parser.add_argument(
        "--db-type",
        choices=["sqlite", "postgres", "postgresql"],
        default=os.environ.get("DB_TYPE") or os.environ.get("MCP_DB_TYPE"),
        help="Default database engine type.",
    )
    parser.add_argument(
        "--connection-string",
        default=os.environ.get("DATABASE_URL")
        or os.environ.get("MCP_DB_CONNECTION_STRING"),
        help="Default database connection string (file path or URI).",
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        default=os.environ.get("ALLOW_WRITES", "").lower() in ("true", "1", "yes"),
        help="Enable write operations (DML/DDL queries).",
    )
    parser.add_argument(
        "--allowed-dir",
        default=os.environ.get("ALLOWED_DATABASE_DIR"),
        help="Restricts SQLite database paths to be inside this directory.",
    )
    args = parser.parse_args()

    global allow_writes_globally, allowed_database_dir
    allow_writes_globally = args.allow_writes
    allowed_database_dir = args.allowed_dir

    # Configure logging strictly to stderr so stdout remains clean for MCP communication
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    # Pre-connect if arguments are passed
    if args.db_type and args.connection_string:
        success, msg = db_manager.connect(
            args.db_type,
            args.connection_string,
            readonly=not allow_writes_globally,
            allowed_dir=allowed_database_dir,
        )
        logger.info(f"Default connection setup: {msg}")

    asyncio.run(run_server())


if __name__ == "__main__":
    main()
