"""Resolve every pinned dependency of this tree into hashed requirement files.

Reached through `make lock`. An image installs from its own
`packages/<image>/requirements.txt` with `--require-hashes`, and the developer
environment from the three files at the root. The inputs are the pins of each
package's `pyproject.toml` and of `requirements.toml`; the packages of this
tree are left out, since they are installed from the checkout itself.

`uv pip compile --universal` writes one resolution good for every platform
and for Python 3.11 upwards, which is what the images and CI run. The
toolchain is resolved for 3.12 upwards, the least its own packages take.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.11"
TOOLS_PYTHON_VERSION = "3.12"
TOOL_FILES = ("requirements-tools.txt", "requirements-cz.txt")
LOCAL = re.compile(r"^enggraph-([a-z]+)(?:\[([a-z,]+)\])?==")
# The packages each image installs from the checkout, in the order it does.
IMAGES = {
    "api": ("core", "indexer", "api"),
    "embed": ("core", "embed"),
    "summarize": ("core", "summarize"),
    "viewer": ("core", "viewer"),
    "mcp": ("core", "mcp"),
    "web": ("core", "web"),
}
PACKAGES = ("core", "indexer", "api", "embed", "summarize", "viewer", "mcp", "web")
HEADER = "# Written by `make lock` from {source}. Do not edit by hand.\n"


def project(package: str) -> dict:
    """Return the [project] table of one package of this tree."""
    path = ROOT / "packages" / package / "pyproject.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def third_party(packages: tuple[str, ...]) -> list[str]:
    """Return the pins the packages need from outside this tree."""
    found: set[str] = set()
    for package in packages:
        table = project(package)
        for requirement in table.get("dependencies", []):
            local = LOCAL.match(requirement)
            if local is None:
                found.add(requirement)
                continue
            # An extra of a package of this tree is that package's own pins.
            extras = project(local.group(1)).get("optional-dependencies", {})
            for extra in filter(None, (local.group(2) or "").split(",")):
                found.update(extras[extra])
    return sorted(found)


def groups() -> dict[str, list[str]]:
    """Return the requirement groups of requirements.toml."""
    text = (ROOT / "requirements.toml").read_text(encoding="utf-8")
    return {name: table["requires"] for name, table in tomllib.loads(text).items()}


def targets() -> dict[str, tuple[str, list[str]]]:
    """Return every file to write: its path, where it comes from, its pins."""
    stated = groups()
    build = stated["build"]
    written = {
        f"packages/{image}/requirements.txt": (
            f"the pyproject.toml of {', '.join(packages)}",
            [*third_party(packages), *build],
        )
        for image, packages in IMAGES.items()
    }
    everything = [*third_party(PACKAGES), *build, *stated["dev"]]
    source = "requirements.toml"
    written["requirements-dev.txt"] = (f"{source} and every package", everything)
    written["requirements-tools.txt"] = (source, stated["tools"])
    written["requirements-cz.txt"] = (source, stated["cz"])
    return written


def compile_one(path: str, source: str, pins: list[str]) -> None:
    """Resolve one list of pins into a hashed requirements file."""
    with tempfile.TemporaryDirectory() as work:
        stated = Path(work) / "requirements.in"
        stated.write_text("\n".join(sorted(set(pins))) + "\n", encoding="utf-8")
        resolved = Path(work) / "requirements.txt"
        command = [
            "uv",
            "pip",
            "compile",
            "--universal",
            "--python-version",
            TOOLS_PYTHON_VERSION if path in TOOL_FILES else PYTHON_VERSION,
            "--generate-hashes",
            "--no-header",
            "--no-annotate",
            "--quiet",
            "--output-file",
            str(resolved),
            str(stated),
        ]
        try:
            subprocess.run(command, check=True)  # noqa: S603 - a fixed command
        except FileNotFoundError:
            raise SystemExit(
                "uv is not installed: https://docs.astral.sh/uv/"
            ) from None
        except subprocess.CalledProcessError as error:
            raise SystemExit(f"{path}: uv could not resolve it") from error
        body = resolved.read_text(encoding="utf-8")
    (ROOT / path).write_text(HEADER.format(source=source) + body, encoding="utf-8")
    print(f"  {path}")


def main() -> None:
    """Write every requirements file, or only the ones named."""
    wanted = set(sys.argv[1:])
    for path, (source, pins) in targets().items():
        if not wanted or path in wanted:
            compile_one(path, source, pins)


if __name__ == "__main__":
    main()
