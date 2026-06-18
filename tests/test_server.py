import pytest
from sqlalchemy import text
from mcp_db_explorer.server import (
    handle_list_tools,
    handle_call_tool,
    handle_list_resources,
    handle_read_resource,
    handle_list_prompts,
    handle_get_prompt,
    db_manager,
)


@pytest.mark.asyncio
async def test_list_tools():
    tools = await handle_list_tools()
    assert len(tools) >= 6
    tool_names = [t.name for t in tools]
    assert "connect_database" in tool_names
    assert "list_tables" in tool_names
    assert "describe_table" in tool_names
    assert "run_query" in tool_names
    assert "get_table_stats" in tool_names
    assert "generate_erd" in tool_names


@pytest.mark.asyncio
async def test_server_tool_flow():
    # 1. Connect database
    res = await handle_call_tool(
        "connect_database", {"db_type": "sqlite", "connection_string": ":memory:"}
    )
    assert "Successfully connected" in res[0].text

    # Create test tables using raw DB engine
    with db_manager.engine.connect() as conn:
        conn.execute(
            text("CREATE TABLE products (id INT PRIMARY KEY, name TEXT, price REAL)")
        )
        conn.execute(text("INSERT INTO products VALUES (1, 'Laptop', 999.99)"))
        conn.commit()

    # 2. List tables tool
    res = await handle_call_tool("list_tables", {})
    assert "products" in res[0].text

    # 3. Describe table tool
    res = await handle_call_tool("describe_table", {"table_name": "products"})
    assert "products" in res[0].text
    assert "Laptop" in res[0].text

    # 4. Run query tool
    res = await handle_call_tool(
        "run_query", {"sql": "SELECT name, price FROM products"}
    )
    assert "Laptop" in res[0].text
    assert "999.99" in res[0].text

    # 5. Explain query tool
    res = await handle_call_tool(
        "explain_query_plan", {"sql": "SELECT * FROM products WHERE id = 1"}
    )
    assert len(res) > 0

    # 6. Table stats tool
    res = await handle_call_tool("get_table_stats", {"table_name": "products"})
    assert "Total Rows" in res[0].text

    # 7. Generate ERD tool
    res = await handle_call_tool("generate_erd", {})
    assert "products" in res[0].text


@pytest.mark.asyncio
async def test_resources_flow():
    # Test listing and reading schema resources
    res_list = await handle_list_resources()
    assert len(res_list) == 1
    assert str(res_list[0].uri) == "schema://info"

    res_contents = await handle_read_resource("schema://info")
    assert len(res_contents) == 1
    assert "products" in res_contents[0].text
    assert "price (REAL)" in res_contents[0].text


@pytest.mark.asyncio
async def test_prompts_flow():
    # List prompts
    prompts = await handle_list_prompts()
    prompt_names = [p.name for p in prompts]
    assert "analyze-schema" in prompt_names
    assert "suggest-sql" in prompt_names
    assert "optimize-query" in prompt_names

    # Get prompt
    prompt_res = await handle_get_prompt(
        "suggest-sql",
        {
            "question": "What is the cheapest product?",
            "schema_info": "products (id, name, price)",
        },
    )
    assert len(prompt_res.messages) == 1
    assert "cheapest product" in prompt_res.messages[0].content.text


@pytest.mark.asyncio
async def test_null_value_formatting():
    # Connect database
    res = await handle_call_tool(
        "connect_database", {"db_type": "sqlite", "connection_string": ":memory:"}
    )
    assert "Successfully connected" in res[0].text

    # Create test table and insert a row with NULL
    with db_manager.engine.connect() as conn:
        conn.execute(text("CREATE TABLE null_test (id INT, name TEXT, val REAL)"))
        conn.execute(text("INSERT INTO null_test VALUES (1, NULL, 9.99)"))
        conn.execute(text("INSERT INTO null_test VALUES (2, 'Bob', NULL)"))
        conn.commit()

    # Query the table
    res = await handle_call_tool(
        "run_query", {"sql": "SELECT name, val FROM null_test"}
    )
    # Check that NULL is rendered instead of None
    assert "Bob" in res[0].text
    assert "NULL" in res[0].text
    assert "None" not in res[0].text

    # Describe the table
    res = await handle_call_tool("describe_table", {"table_name": "null_test"})
    assert "NULL" in res[0].text
    assert "None" not in res[0].text


def test_env_var_defaults(monkeypatch):
    from mcp_db_explorer.server import main

    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DATABASE_URL", "env_test.db")
    monkeypatch.setenv("ALLOW_WRITES", "true")

    parsed_args = None
    import argparse

    original_parse_args = argparse.ArgumentParser.parse_args

    def mock_parse_args(self, args=None, namespace=None):
        nonlocal parsed_args
        res = original_parse_args(self, [])
        parsed_args = res
        return res

    def mock_run(coro):
        if coro:
            coro.close()

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", mock_parse_args)
    monkeypatch.setattr("asyncio.run", mock_run)
    monkeypatch.setattr(db_manager, "connect", lambda *args, **kwargs: (True, "mocked"))

    main()

    assert parsed_args is not None
    assert parsed_args.db_type == "sqlite"
    assert parsed_args.connection_string == "env_test.db"
    assert parsed_args.allow_writes is True
