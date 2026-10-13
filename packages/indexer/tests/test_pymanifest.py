"""What a requirements file and a setup.cfg contribute to the graph."""

from __future__ import annotations

import pytest

from enggraph.indexer.parsers.pymanifest import (
    RequirementsParser,
    SetupCfgParser,
    is_requirements_name,
)
from enggraph.indexer.parsers.registry import is_default_source, parser_class
from enggraph.indexer.projectlinks import placeholder_imports, read_manifest

REQUIREMENTS = """
# runtime
Alpha_Lib[extra]>=1.0 ; python_version < "3.12"
beta==2.0  # pinned
-r base.txt
--constraint constraints.txt
-e ./local/gamma
https://example.com/delta-1.0.tar.gz
epsilon @ https://example.com/epsilon.whl
"""

SETUP_CFG = """
[metadata]
name = alpha

[options]
install_requires =
    beta>=1.0
    Gamma_Lib  # comment
setup_requires = file: build-requirements.txt

[options.extras_require]
dev =
    delta

[options.entry_points]
console_scripts =
    alpha = alpha.cli:main
    alpha-admin = alpha.admin:main

[flake8]
max-line-length = 88
"""


def triples(relations: list[dict[str, str]]) -> set[tuple[str, str, str]]:
    """Return relations as (target, type, scope) triples."""
    return {
        (relation["target"], relation["type"], relation["scope"])
        for relation in relations
    }


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("requirements.txt", True),
        ("app/requirements-dev.txt", True),
        ("requirements/test.txt", True),
        ("constraints.in", True),
        ("notes.txt", False),
        ("docs/requirements-guide.md", False),
    ],
)
def test_a_requirements_file_is_known_by_its_name(path: str, expected: bool) -> None:
    """Hold which paths are read as pip requirements."""
    assert is_requirements_name(path) is expected
    assert (parser_class(path) is RequirementsParser) is expected


def test_a_requirements_file_depends_on_packages_and_includes_files() -> None:
    """Hold that each requirement is a placeholder and -r, -c are files."""
    relations = RequirementsParser().get_relations(REQUIREMENTS, "requirements.txt")
    assert triples(relations) == {
        ("pypi:alpha-lib", "depends_on", "symbol"),
        ("pypi:beta", "depends_on", "symbol"),
        ("pypi:epsilon", "depends_on", "symbol"),
        ("base.txt", "includes", "file"),
        ("constraints.txt", "includes", "file"),
    }


def test_the_edges_take_what_the_manifest_takes() -> None:
    """Hold that the edges and the manifest reader name the same imports."""
    path = "requirements.txt"
    relations = RequirementsParser().get_relations(REQUIREMENTS, path)
    rows = [(path, relation["target"], relation["type"]) for relation in relations]
    _, taken = read_manifest(path, REQUIREMENTS, {path})
    assert set(taken) <= set(placeholder_imports(rows))


def test_setup_cfg_declares_sections_and_scripts() -> None:
    """Hold that sections and console scripts become nodes."""
    assert parser_class("alpha/setup.cfg") is SetupCfgParser
    assert is_default_source("alpha/setup.cfg")
    names = {entity["name"] for entity in SetupCfgParser().get_entities(SETUP_CFG, "")}
    assert names == {
        "section.metadata",
        "section.options",
        "section.options.extras_require",
        "section.options.entry_points",
        "section.flake8",
        "entry_point.alpha",
        "entry_point.alpha-admin",
    }


def test_setup_cfg_depends_on_its_requirements() -> None:
    """Hold that every requirement key and extra becomes an edge."""
    relations = SetupCfgParser().get_relations(SETUP_CFG, "setup.cfg")
    assert triples(relations) == {
        ("pypi:beta", "depends_on", "symbol"),
        ("pypi:gamma-lib", "depends_on", "symbol"),
        ("pypi:delta", "depends_on", "symbol"),
        ("build-requirements.txt", "includes", "file"),
    }


def test_a_broken_setup_cfg_yields_nothing() -> None:
    """Hold that a file configparser rejects gives no nodes and no edges."""
    broken = "install_requires = beta\n"
    assert SetupCfgParser().get_entities(broken, "setup.cfg") == []
    assert SetupCfgParser().get_relations(broken, "setup.cfg") == []
