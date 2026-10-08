"""The words a session is told about its skills, and the version stamp."""

from __future__ import annotations

from enggraph.mcp.skills import (
    skill_instructions,
    skill_path,
    stamp,
    with_skill_check,
)


def test_the_version_is_added_to_the_frontmatter() -> None:
    """After the fields already there."""
    assert (
        stamp("---\nname: alpha\n---\n\nbody\n", "v1")
        == "---\nname: alpha\nversion: v1\n---\n\nbody\n"
    )


def test_a_version_already_there_is_replaced() -> None:
    """One stamp, wherever the old one stood."""
    assert (
        stamp("---\nversion: old\nname: alpha\n---\nbody", "v2")
        == "---\nname: alpha\nversion: v2\n---\nbody"
    )


def test_text_without_a_frontmatter_gets_one() -> None:
    """A skill imported as plain text still carries its version."""
    assert stamp("body\n", "v3") == "---\nversion: v3\n---\n\nbody\n"


def test_every_skill_is_listed_with_its_version() -> None:
    """The list is what an agent compares its local copies with."""
    text = skill_instructions(
        [
            {"name": "alpha", "content": "", "sha256": "aa", "source": "repo"},
            {"name": "beta", "content": "", "sha256": "bb", "source": "import"},
        ]
    )
    assert "- alpha aa" in text
    assert "- beta bb" in text
    assert "get_skill" in text


def test_a_list_that_could_not_be_read_points_at_the_tool() -> None:
    """The session still learns where to ask."""
    assert "list_skills" in skill_instructions(None)


def test_a_skill_lives_where_the_agents_read_skills_from() -> None:
    """One path, relative to the codebase."""
    assert skill_path("alpha") == ".claude/skills/alpha/SKILL.md"


def test_the_check_is_appended_after_the_tool_answer() -> None:
    """What the tool returned stays first, and so does its error flag."""
    result = with_skill_check(
        {"content": [{"type": "text", "text": "[]"}], "isError": False}, "check"
    )
    assert result["content"] == [
        {"type": "text", "text": "[]"},
        {"type": "text", "text": "check"},
    ]
    assert result["isError"] is False
