"""Unit tests for ToolSearchTool and deferred tool infrastructure (Section 4.10)."""

import asyncio

import pytest

from xxcode.tools import Tool, build_tool
from xxcode.tools.registry import ToolRegistry
from xxcode.tools.search import ToolSearchInput, ToolSearchTool


# ── Dummy tools for testing ────────────────────────────────────────────


class _DummyTool(Tool):
    name = "dummy"
    description = "A test tool"
    input_schema = ToolSearchInput  # reuse for convenience
    _is_read_only = True

    async def execute(self, input, context):
        return "ok"


def _make_deferred_tool(name: str, description: str = "", hint: str = "") -> Tool:
    """Create a tool instance with _should_defer=True for testing."""
    tool = _DummyTool()
    tool.name = name
    tool.description = description or f"Tool: {name}"
    tool._should_defer = True
    tool._search_hint = hint
    return tool


def _make_active_tool(name: str) -> Tool:
    """Create an active (non-deferred) tool instance."""
    tool = _DummyTool()
    tool.name = name
    tool.description = f"Active: {name}"
    return tool


# ── Context helper ──────────────────────────────────────────────────────


def _ctx(registry: ToolRegistry) -> dict:
    return {"_registry": registry}


async def _execute_tool_search_query(registry: ToolRegistry, query: str) -> str:
    return await ToolSearchTool().execute(
        ToolSearchInput(query=query),
        _ctx(registry),
    )


# ── ToolRegistry: deferred registration ─────────────────────────────────


def test_register_deferred_tool_goes_to_deferred():
    registry = ToolRegistry()
    tool = _make_deferred_tool("hidden_tool", "Secret tool", "secret hidden")
    registry.register(tool)

    assert registry.get("hidden_tool") is None
    assert "hidden_tool" in registry.get_deferred_tools()
    # API schemas should NOT include deferred tools
    schemas = registry.get_api_schemas()
    schema_names = [s["name"] for s in schemas]
    assert "hidden_tool" not in schema_names


def test_register_active_tool_normal():
    registry = ToolRegistry()
    tool = _make_active_tool("visible_tool")
    registry.register(tool)

    assert registry.get("visible_tool") is not None
    assert "visible_tool" not in registry.get_deferred_tools()
    schemas = registry.get_api_schemas()
    schema_names = [s["name"] for s in schemas]
    assert "visible_tool" in schema_names


def test_activate_tool_moves_to_active():
    registry = ToolRegistry()
    tool = _make_deferred_tool("lazy_tool", "I am lazy", "lazy deferred")
    registry.register(tool)

    activated = registry.activate_tool("lazy_tool")
    assert activated is not None
    assert activated.name == "lazy_tool"

    # Now it should be findable via get() and in API schemas
    assert registry.get("lazy_tool") is not None
    schemas = registry.get_api_schemas()
    schema_names = [s["name"] for s in schemas]
    assert "lazy_tool" in schema_names
    assert "lazy_tool" not in registry.get_deferred_tools()


def test_activate_tool_not_found():
    registry = ToolRegistry()
    result = registry.activate_tool("nonexistent")
    assert result is None


# ── ToolRegistry: search_deferred ───────────────────────────────────────


def test_search_deferred_select_mode():
    registry = ToolRegistry()
    registry.register(_make_deferred_tool("tool_a", "Alpha"))
    registry.register(_make_deferred_tool("tool_b", "Beta"))
    registry.register(_make_deferred_tool("tool_c", "Gamma"))

    results = registry.search_deferred("select:tool_a,tool_c")
    assert len(results) == 2
    assert {t.name for t in results} == {"tool_a", "tool_c"}


def test_search_deferred_select_partial_match():
    registry = ToolRegistry()
    registry.register(_make_deferred_tool("tool_a", "Alpha"))

    # select: only returns exact matches in deferred
    results = registry.search_deferred("select:tool_a,missing")
    assert len(results) == 1
    assert results[0].name == "tool_a"


def test_search_deferred_keyword():
    registry = ToolRegistry()
    registry.register(_make_deferred_tool("notebook_edit", "Edit notebooks", "notebook jupyter ipynb cell"))
    registry.register(_make_deferred_tool("bash_tool", "Run shell commands", "shell bash terminal"))
    registry.register(_make_deferred_tool("sql_tool", "Query databases", "database sql query"))

    # "notebook" should match notebook_edit (name match = 3pts per keyword)
    results = registry.search_deferred("notebook")
    assert len(results) >= 1
    assert results[0].name == "notebook_edit"

    # "jupyter" should match via search_hint (hint match = 1pt)
    results = registry.search_deferred("jupyter")
    assert len(results) >= 1
    assert results[0].name == "notebook_edit"

    # "data" should match sql_tool via description
    results = registry.search_deferred("database")
    assert len(results) >= 1
    assert any(t.name == "sql_tool" for t in results)


def test_search_deferred_prefix_mode():
    registry = ToolRegistry()
    registry.register(_make_deferred_tool("mcp__github__search", "GitHub search", "github mcp"))
    registry.register(_make_deferred_tool("mcp__slack__send", "Slack send", "slack mcp"))
    registry.register(_make_deferred_tool("notebook_edit", "Edit notebooks", "notebook"))

    # +mcp should filter to only mcp__ tools
    results = registry.search_deferred("+mcp")
    assert len(results) == 2
    assert all(t.name.startswith("mcp__") for t in results)

    # +mcp with keyword
    results = registry.search_deferred("+mcp slack")
    assert len(results) >= 1
    assert results[0].name == "mcp__slack__send"

    # +mcp with non-matching keyword
    results = registry.search_deferred("+mcp github")
    assert len(results) >= 1
    assert results[0].name == "mcp__github__search"


def test_search_deferred_empty():
    registry = ToolRegistry()
    assert registry.search_deferred("anything") == []
    assert registry.search_deferred("") == []


# ── ToolSearchTool: execute (select mode) ───────────────────────────────


@pytest.mark.asyncio
async def test_tool_search_select_activates_tools():
    registry = ToolRegistry()
    registry.register(_make_deferred_tool("notebook_edit", "Edit notebooks", "notebook jupyter"))
    registry.register(_make_deferred_tool("mcp__git__search", "Git search", "git mcp"))

    result = await _execute_tool_search_query(registry, "select:notebook_edit")

    assert "Activated" in result
    assert "notebook_edit" in result
    assert registry.get("notebook_edit") is not None
    assert "mcp__git__search" in registry.get_deferred_tools()


@pytest.mark.asyncio
async def test_tool_search_select_multiple():
    registry = ToolRegistry()
    registry.register(_make_deferred_tool("tool_a", "A"))
    registry.register(_make_deferred_tool("tool_b", "B"))
    registry.register(_make_deferred_tool("tool_c", "C"))

    result = await _execute_tool_search_query(registry, "select:tool_a,tool_b")

    assert "Activated 2" in result
    assert registry.get("tool_a") is not None
    assert registry.get("tool_b") is not None
    assert "tool_c" in registry.get_deferred_tools()


@pytest.mark.asyncio
async def test_tool_search_select_not_found_with_suggestions():
    registry = ToolRegistry()
    registry.register(_make_deferred_tool("notebook_edit", "Edit notebooks"))
    registry.register(_make_deferred_tool("bash_tool", "Shell"))

    result = await _execute_tool_search_query(registry, "select:notebook_edit_fake")

    assert "Not found" in result
    assert "notebook_edit" in result


@pytest.mark.asyncio
async def test_tool_search_select_already_active():
    registry = ToolRegistry()
    registry.register(_make_active_tool("visible_tool"))

    result = await _execute_tool_search_query(registry, "select:visible_tool")

    assert "Already active" in result


@pytest.mark.asyncio
async def test_tool_search_select_empty():
    registry = ToolRegistry()
    result = await _execute_tool_search_query(registry, "select:")
    assert "No tool names" in result


# ── ToolSearchTool: execute (search mode) ────────────────────────────────


@pytest.mark.asyncio
async def test_tool_search_keyword_finds_tools():
    registry = ToolRegistry()
    registry.register(_make_deferred_tool("notebook_edit", "Edit Jupyter notebooks", "notebook jupyter ipynb"))
    registry.register(_make_deferred_tool("sql_runner", "Run SQL queries", "database sql query runner"))

    result = await _execute_tool_search_query(registry, "notebook")

    assert "notebook_edit" in result
    assert "select:Name" in result


@pytest.mark.asyncio
async def test_tool_search_keyword_no_match():
    registry = ToolRegistry()
    registry.register(_make_deferred_tool("tool_a", "Alpha"))
    registry.register(_make_deferred_tool("tool_b", "Beta"))

    result = await _execute_tool_search_query(registry, "zzz_nonexistent_keyword")

    assert "No tools match" in result
    assert "tool_a" in result
    assert "tool_b" in result


@pytest.mark.asyncio
async def test_tool_search_no_deferred_tools():
    registry = ToolRegistry()
    registry.register(_make_active_tool("only_tool"))

    result = await _execute_tool_search_query(registry, "anything")

    assert "No deferred tools" in result


@pytest.mark.asyncio
async def test_tool_search_prefix_mode():
    registry = ToolRegistry()
    registry.register(_make_deferred_tool("mcp__github__pr", "GitHub PRs", "github pr mcp"))
    registry.register(_make_deferred_tool("mcp__slack__msg", "Slack messages", "slack msg mcp"))
    registry.register(_make_deferred_tool("sql_runner", "SQL", "sql"))

    result = await _execute_tool_search_query(registry, "+mcp")

    assert "mcp__github__pr" in result
    assert "mcp__slack__msg" in result
    assert "sql_runner" not in result


# ── ToolSearchTool: UI rendering ────────────────────────────────────────


def test_render_tool_use():
    tool = ToolSearchTool()
    rendered = tool.render_tool_use(ToolSearchInput(query="select:notebook_edit"))
    assert "notebook_edit" in rendered


def test_render_tool_result():
    tool = ToolSearchTool()
    result = tool.render_tool_result("Activated 1 tool", False)
    assert "Tool search" in result


# ── build_tool: should_defer and search_hint ────────────────────────────


def test_build_tool_should_defer():
    instance = build_tool(_DummyTool, should_defer=True, search_hint="test hint")
    assert instance._should_defer is True
    assert instance._search_hint == "test hint"


def test_build_tool_no_defer_defaults():
    instance = build_tool(_DummyTool)
    assert instance._should_defer is False
    assert instance._search_hint == ""


# ── register_class: should_defer route ──────────────────────────────────


def test_register_class_with_should_defer():
    registry = ToolRegistry()
    registry.register_class(_DummyTool, name="lazy_one", should_defer=True, search_hint="lazy")

    assert registry.get("lazy_one") is None
    assert "lazy_one" in registry.get_deferred_tools()

    # Activate and verify
    registry.activate_tool("lazy_one")
    assert registry.get("lazy_one") is not None


# ── Integration: deferred + active tools don't mix in schemas ───────────


def test_get_api_schemas_mixed():
    registry = ToolRegistry()
    registry.register(_make_active_tool("active_a"))
    registry.register(_make_active_tool("active_b"))
    registry.register(_make_deferred_tool("deferred_x", "Hidden X"))
    registry.register(_make_deferred_tool("deferred_y", "Hidden Y"))

    schemas = registry.get_api_schemas()
    schema_names = [s["name"] for s in schemas]

    assert "active_a" in schema_names
    assert "active_b" in schema_names
    assert "deferred_x" not in schema_names
    assert "deferred_y" not in schema_names
    assert len(schema_names) == 2
