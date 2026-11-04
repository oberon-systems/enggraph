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
