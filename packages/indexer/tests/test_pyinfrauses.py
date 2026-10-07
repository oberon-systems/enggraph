"""What a pyinfra deploy installs and runs, its config resolved."""

from __future__ import annotations

from enggraph.indexer.parsers.pyinfrauses import pyinfra_uses

MAIN = """from pyinfra.operations import apt, docker, pip as python
from pyinfra.operations import server


class Config:
    package: str = 'alpha-web'
    ensure: str = 'present'
    image: str = 'example.com/alpha/worker:2'
    tools: list[str] = []


def spec(name: str, ensure: str) -> str:
    return name if ensure == 'present' else f'{name}-{ensure}'


def deploy(config: Config) -> None:
    apt.packages(packages=[spec(config.package, config.ensure)])
    python.packages(packages=config.tools)
    docker.container(container='worker', image=config.image)
    server.shell(commands=[f'echo {config.package}'])
    apt.packages(packages=[undefined_name])
"""


def _uses(data: dict[str, list[str]]) -> list[tuple[str, str]]:
    return pyinfra_uses({"code/main.py": MAIN}, lambda key: data.get(key, []))


def test_operations_take_what_the_config_defaults_to() -> None:
    """Defaults stand in where the data sets nothing; a pinned branch is dropped."""
    assert _uses({}) == [
        ("package:alpha-web", "installs"),
        ("image:example.com/alpha/worker:2", "uses_image"),
    ]


def test_the_data_wins_over_the_default() -> None:
    """A value the data sets replaces the model default."""
    found = _uses(
        {"package": ["beta-web"], "ensure": ["1.2.0"], "tools": ["alpha-cli"]}
    )
    assert ("package:beta-web", "installs") in found
    assert ("package:beta-web-1.2.0", "installs") not in found
    assert ("pypi:alpha-cli", "installs") in found


def test_code_without_pyinfra_operations_takes_nothing() -> None:
    """A call on a name not bound to pyinfra.operations is no operation."""
    code = "import apt\n\napt.packages(packages=['alpha'])\n"
    assert pyinfra_uses({"main.py": code}, lambda key: []) == []
    assert pyinfra_uses({"main.py": "def broken(:\n"}, lambda key: []) == []
