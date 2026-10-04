"""Read the images a Puppet manifest runs and the packages it installs.

A value is taken when it is a literal, or a variable the same manifest assigns a
literal to, the way a class sets `$image` once and passes it on. Anything that
still holds an unresolved variable is left out.
"""

from __future__ import annotations

import re

ASSIGNMENT = re.compile(
    r"^\s*(?:[A-Z][\w:\[\], ]*\s+)?\$(\w+)\s*=\s*(['\"])(.*?)\2\s*,?\s*$", re.M
)
IMAGE_ATTRIBUTE = re.compile(
    r"\b(?:image|image_name|container_image)\s*=>\s*"
    r"('[^'\n]*'|\"[^\"\n]*\"|\$\{?\w+\}?)"
)
PACKAGE_TITLE = re.compile(
    r"(?<![\w:])package\s*\{\s*(\[[^\]]*\]|'[^']+'|\"[^\"]+\"|\$\{?\w+\}?)\s*:"
)
ENSURE_PACKAGES = re.compile(
    r"\bensure_packages\s*\(\s*(\[[^\]]*\]|'[^']+'|\"[^\"]+\"|\$\{?\w+\}?)"
)
CLASS_HEAD = re.compile(
    r"^\s*class\s+([\w:]+)\s*(?:\((.*?)\))?\s*(?:inherits\s+[\w:]+\s*)?\{",
    re.M | re.S,
)
VARIABLE_NAME = re.compile(r"\$\{?(\w+)\}?")
LEADING_VARIABLE = re.compile(r"^\"?\$\{?(\w+)\}?")
QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"")
INTERPOLATION = re.compile(r"\$\{(\w+)\}|\$(\w+)")
PROVIDER = re.compile(r"\bprovider\s*=>\s*['\"]?(\w+)")
# Providers that install something other than an OS package, by link kind;
# None means a kind no project provides.
PROVIDER_KINDS: dict[str, str | None] = {
    "pip": "pypi",
    "pip3": "pypi",
    "gem": None,
    "npm": None,
}

USES_IMAGE = "uses_image"
INSTALLS = "installs"


def _interpolate(text: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return variables.get(name, match.group(0))

    return INTERPOLATION.sub(replace, text)


def _value(raw: str, variables: dict[str, str]) -> str:
    if raw.startswith("$"):
        return variables.get(raw.strip("${}"), raw)
    text = raw[1:-1]
    return _interpolate(text, variables) if raw.startswith('"') else text


def _titles(raw: str) -> list[str]:
    return [single or double for single, double in QUOTED.findall(raw)]


def class_parameters(content: str) -> tuple[str, set[str]]:
    """Return the class a manifest declares and the names of its parameters."""
    head = CLASS_HEAD.search(content)
    if head is None:
        return "", set()
    return head.group(1).lstrip(":"), set(VARIABLE_NAME.findall(head.group(2) or ""))


def puppet_parameter_uses(content: str) -> list[tuple[str, str, str]]:
    """Return (kind, relation, hiera key) for what a class parameter decides.

    `image => $image` with `$image` a parameter of class `app` is decided by
    whatever data sets `app::image`, which is where the value is looked up.
    """
    owner, parameters = class_parameters(content)
    if not owner:
        return []
    found: list[tuple[str, str, str]] = []

    def parameter_keys(raw: str) -> list[str]:
        names = [raw] if raw.startswith(("$", '"')) else []
        if raw.startswith("["):
            names = VARIABLE_NAME.findall(raw)
            return [f"{owner}::{name}" for name in names if name in parameters]
        matches = [LEADING_VARIABLE.match(name) for name in names]
        return [
            f"{owner}::{match.group(1)}"
            for match in matches
            if match and match.group(1) in parameters
        ]

    for raw in IMAGE_ATTRIBUTE.findall(content):
        found.extend(("image", USES_IMAGE, key) for key in parameter_keys(raw))
    for match in PACKAGE_TITLE.finditer(content):
        body = content[match.end() : content.find("}", match.end())]
        provider = PROVIDER.search(body)
        kind = (
            PROVIDER_KINDS.get(provider.group(1), "package") if provider else "package"
        )
        if kind is not None:
            found.extend(
                (kind, INSTALLS, key) for key in parameter_keys(match.group(1))
            )
    for raw in ENSURE_PACKAGES.findall(content):
        found.extend(("package", INSTALLS, key) for key in parameter_keys(raw))
    return list(dict.fromkeys(found))


def puppet_uses(content: str) -> list[tuple[str, str]]:
    """Return (placeholder target, relation) for every image and package."""
    variables: dict[str, str] = {}
    for name, quote, value in ASSIGNMENT.findall(content):
        variables[name] = _interpolate(value, variables) if quote == '"' else value
    found: list[tuple[str, str]] = []
    for raw in IMAGE_ATTRIBUTE.findall(content):
        image = _value(raw, variables)
        if image and "$" not in image:
            found.append((f"image:{image}", USES_IMAGE))
    for match in PACKAGE_TITLE.finditer(content):
        body = content[match.end() : content.find("}", match.end())]
        provider = PROVIDER.search(body)
        kind = (
            PROVIDER_KINDS.get(provider.group(1), "package") if provider else "package"
        )
        if kind is not None:
            found.extend(
                (f"{kind}:{name}", INSTALLS) for name in _titles(match.group(1))
            )
    for raw in ENSURE_PACKAGES.findall(content):
        found.extend((f"package:{name}", INSTALLS) for name in _titles(raw))
    return [
        (target, relation)
        for target, relation in dict.fromkeys(found)
        if "$" not in target
    ]
