"""What an RPM spec contributes to the graph."""

from __future__ import annotations

from enggraph.indexer.parsers.registry import is_default_source, parser_class
from enggraph.indexer.parsers.rpmspec import RpmSpecParser, expand_macros

SPEC = """
%global srcname alpha
Name:           %{srcname}
Version:        1.2
Release:        1%{?dist}
Source0:        https://example.com/%{name}-%{version}.tar.gz
Source1:        %{name}.service
Patch0:         patches/fix-build.patch
BuildRequires:  gcc, make >= 4.0
Requires:       beta >= 2.0, /usr/bin/python3, python3dist(gamma)
Requires(post): systemd
Requires:       %{undefined_macro}

%description
Requires: not-a-package

%package devel
Summary: Headers
Requires: %{name} = %{version}-%{release}

%package -n delta
Summary: Delta

%prep
%setup -q
"""


def relations(content: str) -> set[tuple[str, str, str]]:
    """Return a spec's relations as (target, type, scope) triples."""
    return {
        (relation["target"], relation["type"], relation["scope"])
        for relation in RpmSpecParser().get_relations(content, "rpm/alpha.spec")
    }


def test_a_spec_is_read_by_its_own_parser() -> None:
    """Hold that a spec is selected and read by the spec parser."""
    assert parser_class("rpm/alpha.spec") is RpmSpecParser
    assert is_default_source("rpm/alpha.spec")


def test_a_spec_declares_its_packages() -> None:
    """Hold that the main package and both kinds of subpackage are nodes."""
    entities = RpmSpecParser().get_entities(SPEC, "rpm/alpha.spec")
    assert entities == [
        {"name": "package.alpha", "type": "package"},
        {"name": "package.alpha-devel", "type": "package"},
        {"name": "package.delta", "type": "package"},
    ]


def test_a_spec_requires_packages_and_builds_from_files() -> None:
    """Hold that requirements are placeholders and local sources are files."""
    assert relations(SPEC) == {
        ("package:gcc", "build_depends_on", "symbol"),
        ("package:make", "build_depends_on", "symbol"),
        ("package:beta", "depends_on", "symbol"),
        ("package:systemd", "depends_on", "symbol"),
        ("package:alpha", "depends_on", "symbol"),
        ("alpha.service", "uses_file", "file"),
        ("fix-build.patch", "uses_file", "file"),
    }


def test_macros_expand_and_conditionals_fall_away() -> None:
    """Hold that known macros expand and an unset conditional is empty."""
    macros = {"name": "alpha", "version": "1.2"}
    assert expand_macros("%{name}-%{version}%{?dist}", macros) == "alpha-1.2"
    assert expand_macros("%{?name:has-name}", macros) == "has-name"
    assert expand_macros("%{!?dist:no-dist}", macros) == "no-dist"
    assert expand_macros("%{unknown}", macros) == "%{unknown}"
