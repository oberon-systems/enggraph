"""What `make install` writes into an agent's configuration, and what it keeps."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "mcp_register.py"
URL = "http://localhost:3000/mcp/alpha"
HEADER = {"Authorization": "Bearer ${ENGGRAPH_TOKEN}"}


@pytest.fixture(scope="module")
def register() -> ModuleType:
    """Load the script the installer runs."""
    spec = importlib.util.spec_from_file_location("mcp_register", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def written(path: Path) -> dict:
    """Read back what the script wrote."""
    return json.loads(path.read_text(encoding="utf-8"))


def test_both_agents_get_the_header_and_not_the_token(
    register: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The variable is spelled out for the agent to expand from its settings."""
    monkeypatch.setenv("PERMISSIONS", "0")
    monkeypatch.setattr("sys.argv", ["mcp_register.py", str(tmp_path), "alpha", "3000"])
    register.main()
    capsys.readouterr()
    claude = written(tmp_path / ".mcp.json")["mcpServers"]["enggraph"]
    gemini = written(tmp_path / ".gemini" / "settings.json")["mcpServers"]
    assert claude == {"type": "http", "url": URL, "headers": HEADER}
    assert gemini["enggraph"]["headers"] == HEADER


def test_an_entry_of_this_stack_gains_the_header_once(
    register: ModuleType, tmp_path: Path
) -> None:
    """Nothing else in it changes, and a second run changes nothing."""
    path = tmp_path / ".mcp.json"
    mine = {"type": "http", "url": "http://localhost:3000/mcp/beta", "timeout": 9}
    path.write_text(json.dumps({"mcpServers": {"enggraph": mine}}), encoding="utf-8")
    entry = {"type": "http", "url": URL, "headers": HEADER}
    assert register.register(str(path), entry) == "token header added"
    assert written(path)["mcpServers"]["enggraph"] == {**mine, "headers": HEADER}
    assert register.register(str(path), entry).startswith("kept")


def test_a_foreign_or_already_authorized_entry_is_left_alone(
    register: ModuleType, tmp_path: Path
) -> None:
    """Another address, or a header someone wrote, is a decision to keep."""
    entry = {"type": "http", "url": URL, "headers": HEADER}
    for mine in (
        {"type": "http", "url": "https://graph.example.com/mcp/alpha"},
        {"type": "http", "url": URL, "headers": {"authorization": "Bearer mine"}},
    ):
        path = tmp_path / ".mcp.json"
        path.write_text(
            json.dumps({"mcpServers": {"enggraph": mine}}), encoding="utf-8"
        )
        before = path.read_text(encoding="utf-8")
        assert register.register(str(path), entry).startswith("kept")
        assert path.read_text(encoding="utf-8") == before


def test_a_stack_elsewhere_is_addressed_by_its_url(
    register: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port means this machine; a URL means what it says."""
    monkeypatch.setenv("PERMISSIONS", "0")
    argv = ["mcp_register.py", str(tmp_path), "alpha", "https://stack.example.com/"]
    monkeypatch.setattr("sys.argv", argv)

    register.main()

    url = written(tmp_path / ".mcp.json")["mcpServers"]["enggraph"]["url"]
    assert url == "https://stack.example.com/mcp/alpha"


TOOLS = SCRIPT.parents[1] / "packages/mcp/src/enggraph/mcp/tools.json"


def writes(tool: str) -> bool:
    """Say whether a tool changes what is stored, as the MCP server does."""
    return tool.startswith(("save_", "drop_")) or tool in (
        "clear_file_hash",
        "set_file_hash",
    )


def test_the_read_tools_are_the_ones_the_server_has(register: ModuleType) -> None:
    """A tool added to the server is ruled on here, one way or the other."""
    listed = json.loads(TOOLS.read_text(encoding="utf-8"))
    names = {tool["name"] for tool in listed}
    assert set(register.READ_TOOLS) == {name for name in names if not writes(name)}


def test_no_write_tool_runs_without_a_prompt(
    register: ModuleType, tmp_path: Path
) -> None:
    """On an empty settings file every allow rule names one read tool."""
    path = tmp_path / "settings.json"
    said = register.permit(str(path))
    assert said == f"{len(register.READ_TOOLS)} read rules added"
    allowed = written(path)["permissions"]["allow"]
    assert "mcp__enggraph" not in allowed
    assert all(rule.startswith("mcp__enggraph__") for rule in allowed)
    assert not any(writes(rule.removeprefix("mcp__enggraph__")) for rule in allowed)
    assert register.permit(str(path)).startswith("kept")


def test_a_new_settings_file_is_its_owners_alone(
    register: ModuleType, tmp_path: Path
) -> None:
    """The file may come to hold a token, so nobody else reads it."""
    path = tmp_path / "home" / "settings.json"
    register.permit(str(path))
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_the_rule_for_the_whole_server_is_narrowed(
    register: ModuleType, tmp_path: Path
) -> None:
    """What an earlier install allowed wholesale is cut back to the reads."""
    path = tmp_path / "settings.json"
    asked = ["mcp__enggraph__drop_plan"]
    mine = {"allow": ["mcp__enggraph", "Bash(ls:*)"], "ask": asked}
    path.write_text(json.dumps({"permissions": mine}), encoding="utf-8")
    assert "1 wider rules dropped" in register.permit(str(path))
    after = written(path)["permissions"]
    assert "mcp__enggraph" not in after["allow"] and "Bash(ls:*)" in after["allow"]
    assert after["ask"] == ["mcp__enggraph__drop_plan"]


def test_gemini_is_trusted_only_when_asked(
    register: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without the switch Gemini confirms every call, as it does by default."""
    monkeypatch.setenv("PERMISSIONS", "0")
    for trust, root in (("", tmp_path / "a"), ("1", tmp_path / "b")):
        monkeypatch.setenv("GEMINI_TRUST", trust)
        argv = ["mcp_register.py", str(root), "alpha", "3000"]
        monkeypatch.setattr("sys.argv", argv)
        register.main()
        entry = written(root / ".gemini" / "settings.json")["mcpServers"]["enggraph"]
        assert ("trust" in entry) is (trust == "1")
    capsys.readouterr()
