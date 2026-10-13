"""What a systemd unit contributes to the graph."""

from __future__ import annotations

from enggraph.indexer.parsers.registry import is_default_source, parser_class
from enggraph.indexer.parsers.systemd import SystemdParser, read_unit

SERVICE = """
[Unit]
Description=Alpha
Requires=beta.service postgres.service
Wants=network-online.target
After=network-online.target \\
      beta.service
Before=gamma@one.service
PartOf=%i.target

[Service]
EnvironmentFile=-/etc/default/alpha
EnvironmentFile=config/alpha.env
ExecStart=/usr/bin/alpha

[Install]
WantedBy=multi-user.target
"""

SOURCE = "unit.alpha.service"


def triples(content: str, rel_path: str) -> set[tuple[str, str, str, str]]:
    """Return a unit's relations as (source, target, type, scope)."""
    return {
        (relation["source"], relation["target"], relation["type"], relation["scope"])
        for relation in SystemdParser().get_relations(content, rel_path)
    }


def test_a_unit_is_read_by_its_own_parser() -> None:
    """Hold that every unit extension reaches the systemd parser."""
    for path in ("etc/alpha.service", "etc/alpha.timer", "etc/alpha.socket"):
        assert parser_class(path) is SystemdParser
        assert is_default_source(path)


def test_a_unit_file_is_one_node() -> None:
    """Hold that the file declares the unit it names."""
    entities = SystemdParser().get_entities(SERVICE, "etc/alpha.service")
    assert entities == [{"name": SOURCE, "type": "unit"}]


def test_a_continued_line_is_one_value() -> None:
    """Hold that a trailing backslash continues the value."""
    after = dict(read_unit(SERVICE)["Unit"])["After"]
    assert after.split() == ["network-online.target", "beta.service"]


def test_a_service_names_the_units_it_needs_and_orders_against() -> None:
    """Hold the dependency, ordering and install edges of a service."""
    assert triples(SERVICE, "etc/alpha.service") == {
        (SOURCE, "unit.beta.service", "requires", "symbol"),
        (SOURCE, "unit.postgres.service", "requires", "symbol"),
        (SOURCE, "unit.network-online.target", "wants", "symbol"),
        (SOURCE, "unit.network-online.target", "after", "symbol"),
        (SOURCE, "unit.beta.service", "after", "symbol"),
        (SOURCE, "unit.gamma@.service", "before", "symbol"),
        (SOURCE, "unit.multi-user.target", "wanted_by", "symbol"),
        (SOURCE, "config/alpha.env", "reads_vars", "file"),
    }


def test_a_timer_activates_its_service() -> None:
    """Hold that a timer starts the service of its name, or the one it names."""
    plain = "[Timer]\nOnCalendar=daily\n"
    named = "[Timer]\nOnCalendar=daily\nUnit=beta.service\n"
    source = "unit.alpha.timer"
    assert triples(plain, "etc/alpha.timer") == {
        (source, "unit.alpha.service", "activates", "symbol")
    }
    assert triples(named, "etc/alpha.timer") == {
        (source, "unit.beta.service", "activates", "symbol")
    }


def test_an_accepting_socket_activates_the_template() -> None:
    """Hold that a socket with Accept=yes starts the template service."""
    socket = "[Socket]\nListenStream=8080\nAccept=yes\n"
    assert triples(socket, "etc/alpha.socket") == {
        ("unit.alpha.socket", "unit.alpha@.service", "activates", "symbol")
    }
