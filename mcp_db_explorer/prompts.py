from mcp.types import (
    Prompt,
    PromptArgument,
    GetPromptResult,
    PromptMessage,
    TextContent,
)

# List of supported MCP Prompts
PROMPTS = [
    Prompt(
        name="analyze-schema",
        description="Help analyze the current database schema, suggest insights, and propose analytical questions.",
        arguments=[
            PromptArgument(
                name="schema_info",
                description="Text description of the database schema.",
                required=True,
            )
        ],
    ),
    Prompt(
        name="suggest-sql",
        description="Translate a natural language question into a safe and optimized SQL query based on the schema.",
        arguments=[
            PromptArgument(
                name="question",
                description="The natural language question to solve.",
                required=True,
            ),
            PromptArgument(
                name="schema_info",
                description="Text description of the database schema.",
                required=True,
            ),
        ],
    ),
    Prompt(
        name="optimize-query",
        description="Analyze a SQL query and its EXPLAIN query plan to suggest optimizations and missing indexes.",
        arguments=[
            PromptArgument(
                name="sql", description="The SQL query to optimize.", required=True
            ),
            PromptArgument(
                name="query_plan",
                description="The output of EXPLAIN query plan analysis.",
                required=False,
            ),
            PromptArgument(
                name="schema_info",
                description="Relevant database schema details.",
                required=False,
            ),
        ],
    ),
]


def render_prompt(name: str, arguments: dict) -> GetPromptResult:
    """
    Renders the prompt text based on the name and arguments, and returns a GetPromptResult.
    """
    if name == "analyze-schema":
        schema_info = arguments.get("schema_info", "")
        text_content = (
            "You are a senior data analyst and database administrator.\n\n"
            "Analyze the database schema provided below and suggest:\n"
            "1. Key tables and relationship paths.\n"
            "2. 5 high-value business questions you can answer using this schema.\n"
            "3. Initial optimization or indexing recommendations based on keys and columns.\n\n"
            f"Here is the database schema:\n{schema_info}\n\n"
            "Format your answer as structured, readable Markdown."
        )
    elif name == "suggest-sql":
        question = arguments.get("question", "")
        schema_info = arguments.get("schema_info", "")
        text_content = (
            "You are a database engineer. Translate the following user request into a clean, optimized SQL query:\n"
            f'Request: "{question}"\n\n'
            f"Database Schema:\n{schema_info}\n\n"
            "Rules:\n"
            "1. Output ONLY the raw SQL query in a code block.\n"
            "2. Ensure the query is read-only (SELECT queries only).\n"
            "3. Quote identifiers where necessary (e.g., column names with spaces or keywords).\n"
            "4. Use JOINs instead of subqueries where performance is better."
        )
    elif name == "optimize-query":
        sql = arguments.get("sql", "")
        query_plan = arguments.get("query_plan", "Not provided")
        schema_info = arguments.get("schema_info", "Not provided")
        text_content = (
            "You are a database performance expert. Review the following SQL query and suggest optimizations:\n\n"
            f"SQL Query:\n```sql\n{sql}\n```\n\n"
            f"Database Schema:\n{schema_info}\n\n"
            f"Query Execution Plan (EXPLAIN):\n{query_plan}\n\n"
            "Please provide:\n"
            "1. An optimized version of the SQL query.\n"
            "2. A line-by-line explanation of why this version is better (e.g., avoiding table scans, leveraging indexes).\n"
            "3. Recommendations for indexes that should be created on the tables involved."
        )
    else:
        raise ValueError(f"Unknown prompt name: {name}")

    return GetPromptResult(
        messages=[
            PromptMessage(
                role="user", content=TextContent(type="text", text=text_content)
            )
        ]
    )
