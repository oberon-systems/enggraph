"""The tool list is the contract: every name, description and argument.

The fixture is what the previous server answered, for a session opened on
no project and for one opened on a project.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from enggraph.mcp import handlers, tools
from enggraph.mcp.search import MODES

PLACEHOLDER = "@@P@@"


@pytest.fixture(scope="module")
def recorded() -> dict[str, Any]:
    """Load what the previous server answered."""
    path = Path(__file__).with_name("fixtures") / "tools.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_session_without_a_project_sees_the_list_as_recorded(
    recorded: dict[str, Any],
) -> None:
    """Every scope argument says the project has to be named."""
    assert tools.list_tools(None, None) == recorded["none"]


def test_a_session_on_a_project_sees_the_list_as_recorded(
    recorded: dict[str, Any],
) -> None:
    """Every scope argument names the default."""
    assert tools.list_tools(PLACEHOLDER, None) == recorded["named"]


def test_an_organization_is_told_what_a_read_there_covers(
    recorded: dict[str, Any],
) -> None:
    """With the count spelled for none, one and several."""
    note = tools.organization_note
    assert note(PLACEHOLDER, 3) == recorded["organization_note"]
    assert note(PLACEHOLDER, 0) == recorded["organization_note_empty"]
    assert note(PLACEHOLDER, 1) == recorded["organization_note_one"]
    record_note = tools.organization_record_note(PLACEHOLDER, 3)
    assert record_note == recorded["organization_record_note"]


def test_the_organization_note_reaches_the_scope_arguments() -> None:
    """A search scope is the one argument it is left off."""
    listed = {tool["name"]: tool for tool in tools.list_tools(PLACEHOLDER, 2)}
    properties = listed["get_overview"]["inputSchema"]["properties"]
    assert "is an organization" in properties["project"]["description"]
    search = listed["search_code"]["inputSchema"]["properties"]
    assert "is an organization" not in search["project"]["description"]


def test_search_code_lists_the_modes_it_takes() -> None:
    """The refusal of any other value names the same three."""
    listed = {tool["name"]: tool for tool in tools.list_tools(None, None)}
    mode = listed["search_code"]["inputSchema"]["properties"]["mode"]
    assert tuple(mode["enum"]) == MODES


def test_every_listed_tool_has_a_handler_and_no_handler_is_unlisted() -> None:
    """A tool an agent is shown and cannot call is worse than a missing one."""
    listed = {tool["name"] for tool in tools.list_tools(None, None)}
    handled = {
        *handlers.EARLY,
        *handlers.SCOPED,
        *handlers.SYMBOL_TOOLS,
        *handlers.LINK_TOOLS,
        *handlers.EXPORT_TOOLS,
        *handlers.SKILL_TOOLS,
    }
    assert listed == handled
    assert len(listed) == 49
