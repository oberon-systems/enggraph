"""Reading the built-in skills from disk and syncing them to the table."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

from enggraph.api.skills import Skill, digest, read_repo_skills, sync_repo_skills


def write(root: Path, name: str, text: str) -> None:
    """Write one SKILL.md under root."""
    (root / name).mkdir()
    (root / name / "SKILL.md").write_text(text, encoding="utf-8")


def test_digest_is_sha256_of_text() -> None:
    """The version is the sha256 of the text."""
    assert digest("alpha") == hashlib.sha256(b"alpha").hexdigest()


def test_reads_every_skill_directory_sorted(tmp_path: Path) -> None:
    """Only directories holding a SKILL.md count, sorted by name."""
    write(tmp_path, "beta", "---\nname: beta\n---\n")
    write(tmp_path, "alpha", "---\nname: alpha\n---\n")
    (tmp_path / "gamma").mkdir()
    (tmp_path / "notes.md").write_text("loose", encoding="utf-8")

    skills = read_repo_skills(str(tmp_path))

    assert [skill.name for skill in skills] == ["alpha", "beta"]
    assert skills[0].sha256 == digest("---\nname: alpha\n---\n")


def test_missing_directory_reads_as_empty(tmp_path: Path) -> None:
    """An absent directory yields no skills rather than an error."""
    assert read_repo_skills(str(tmp_path / "absent")) == []


def test_sync_upserts_each_and_deletes_the_rest() -> None:
    """Every skill is upserted, then built-ins not listed are deleted."""
    cursor = MagicMock()
    skills = [Skill("alpha", "a", digest("a")), Skill("beta", "b", digest("b"))]

    sync_repo_skills(cursor, skills)

    calls = cursor.execute.call_args_list
    assert [call.args[1][0] for call in calls[:2]] == ["alpha", "beta"]
    assert "source = 'repo'" in calls[2].args[0]
    assert calls[2].args[1] == (["alpha", "beta"],)
