"""What a model is shown about a directory or an entity, and what it is asked."""

from __future__ import annotations

from typing import Any

import pytest

from enggraph import jobs, nodetext
from enggraph.nodetext import Node
from enggraph.summaries import entity_summary
from enggraph.summary_text import content_key

SOURCE = """import alpha

# Claims the next batch.
@retry
def claim(queue,
          size):
    '''Take up to size tasks.'''
    return queue[:size]


def release():

    \"\"\"
    Hand a batch back.
    \"\"\"


def settle():
    note = \"\"\"not a docstring\"\"\"
""".splitlines()


def test_a_docstring_describes_its_entity() -> None:
    """Preferred over the comment above, which often describes a section."""
    assert entity_summary(SOURCE, 5) == "Take up to size tasks."


def test_a_docstring_on_its_own_lines_is_read() -> None:
    """The opening quotes alone on a line, the text on the next."""
    assert entity_summary(SOURCE, 11) == "Hand a batch back."


def test_a_string_in_the_body_is_not_a_docstring() -> None:
    """Only the first statement of the body counts."""
    assert entity_summary(SOURCE, 18) == ""


def test_a_comment_above_describes_what_has_no_docstring() -> None:
    """JSDoc and friends: the block right above, decorators stepped over."""
    lines = ["/**", " * Builds the alpha widget.", " * @param x", " */", "fn()"]
    assert entity_summary(lines, 5) == "Builds the alpha widget."


def test_a_line_outside_the_file_says_nothing() -> None:
    """A stale id must not index past the end."""
    assert entity_summary(SOURCE, 999) == ""


def test_each_kind_is_named_its_own_way() -> None:
    """The head of the prompt, and what an answer must say more than."""
    entity = Node("src/queue.py::claim()@L5", "entity", "src/queue.py")
    directory = Node("src/", "directory", "src/")
    assert nodetext.subject(entity) == "Symbol: claim() in src/queue.py"
    assert nodetext.label(entity) == "claim()"
    assert nodetext.subject(directory) == "Directory: src/"
    assert nodetext.label(directory) == "src/"


def test_an_entity_is_read_up_to_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through `sources`, the one seam onto the tree."""
    monkeypatch.setattr(
        nodetext,
        "entity_lines",
        lambda *_: [
            ("src/queue.py::claim()@L5", 5),
            ("src/queue.py::release()@L11", 11),
        ],
    )
    monkeypatch.setattr(
        nodetext.sources, "read", lambda *_args, **_kw: ("\n".join(SOURCE), "")
    )
    node = Node("src/queue.py::claim()@L5", "entity", "src/queue.py")
    text, reason = nodetext.read(None, "alpha", node, 0)  # type: ignore[arg-type]
    assert reason == ""
    assert text.splitlines()[0] == "def claim(queue,"
    assert text.splitlines()[-1] == ""
    assert "def release" not in text


class Rows:
    """A cursor answering each statement with the next list of rows."""

    def __init__(self, *answers: list[Any]) -> None:
        """Take the rows of each statement, in order."""
        self.answers = list(answers)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Nothing to send; the rows are already chosen."""

    def fetchall(self) -> list[Any]:
        """Return the rows of the next statement."""
        return self.answers.pop(0)


def test_only_a_changed_directory_is_owed_again() -> None:
    """Its listing hashes to what it was described from, or it is stale."""
    listing = "- queue.py: Claims tasks."
    cursor = Rows(
        [
            ("src/", "llm", content_key(listing), False),
            ("docs/", "llm", "stale", False),
            ("./", "manual", "", False),
        ],
        [("src/", "src/queue.py", "Claims tasks."), ("docs/", "docs/a.md", "")],
    )
    owed = jobs.owed_directories(  # type: ignore[arg-type]
        cursor, "alpha", False, [], 2000
    )
    assert owed == ["docs/"]


def test_a_file_being_described_owes_every_directory_above_it() -> None:
    """Its new summary changes each listing on the way up."""
    cursor = Rows(
        [
            ("src/alpha/", "llm", "x", True),
            ("src/", "llm", content_key(""), False),
            ("./", "llm", content_key(""), False),
        ],
        [],
    )
    owed = jobs.owed_directories(  # type: ignore[arg-type]
        cursor, "alpha", False, ["src/alpha/queue.py"], 2000
    )
    assert owed == ["./", "src/"]
