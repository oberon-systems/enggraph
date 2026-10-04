"""Read the images a Puppet manifest runs and the packages it installs.

A value is followed through the manifest's own variables, its class
parameters and, given a lookup, the parameters of other classes and the data
setting them. Whatever still holds an unresolved variable is left out, except
in a URL whose artifact name is known before the version is.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable

Lookup = Callable[[str], list[str]]

ASSIGNMENT = re.compile(
    r"^\s*(?:[A-Z][\w:\[\], ]*\s+)?\$(\w+)\s*=\s*"
    r"('[^'\n]*'|\"[^\"\n]*\"|\$\{?(?:::)?[\w:]+\}?)\s*,?\s*$",
    re.M,
)
# An attribute or a hash key, bare or quoted and in any case: `image =>`, and
# `'IMAGE' =>` in the environment a compose file is rendered with.
IMAGE_ATTRIBUTE = re.compile(
    r"(?:\b|['\"])(?:image|image_name|container_image|docker_image)['\"]?\s*=>\s*"
    r"('[^'\n]*'|\"[^\"\n]*\"|\$\{?(?:::)?[\w:]+\}?)",
    re.I,
)
TITLE = r"(\[[^\]]*\]|'[^']+'|\"[^\"]+\"|\$\{?(?:::)?[\w:]+\}?)"
PACKAGE_TITLE = re.compile(r"(?<![\w:])package\s*\{\s*" + TITLE + r"\s*:")
ENSURE_PACKAGES = re.compile(r"\bensure_packages\s*\(\s*" + TITLE)
ENSURE_RESOURCE = re.compile(
    r"\bensure_resource\s*\(\s*['\"]package['\"]\s*,\s*" + TITLE
)
# A key read straight from the data, as a module reading a hash of resources
# does: `hiera_hash('packages', {})`, `lookup('app::packages')`.
LOOKUP_CALL = r"(?:lookup|hiera|hiera_hash|hiera_array)\(\s*['\"]([\w:]+)['\"]"
LOOKUP_KEY = re.compile(LOOKUP_CALL)
LOOKUP_ASSIGNMENT = re.compile(r"^\s*\$(\w+)\s*=\s*" + LOOKUP_CALL, re.M)
CREATE_RESOURCES = re.compile(
    r"\bcreate_resources\w*\(\s*['\"]?@{0,2}package['\"]?\s*,\s*"
    r"(\$\{?(?:::)?[\w:]+\}?|" + LOOKUP_CALL + r")"
)
LOOKUP_PREFIX = "@lookup:"
LIST_ITEM = re.compile(r"'([^']+)'|\"([^\"]+)\"|(\$\{?(?:::)?[\w:]+\}?)")
STRING = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")
INTERPOLATION = re.compile(r"\$\{(?:::)?([\w:]+)\}|\$(?:::)?([\w:]+)")
CLASS_HEAD = re.compile(
    r"^\s*class\s+([\w:]+)\s*(?:\((.*?)\))?\s*(?:inherits\s+[\w:]+\s*)?\{",
    re.M | re.S,
)
PARAMETER_DEFAULT = re.compile(r"\$(\w+)\s*=\s*('[^'\n]*'|\"[^\"\n]*\")")
PROVIDER = re.compile(r"\bprovider\s*=>\s*['\"]?(\w+)")
# A Python artifact in a package index, its version possibly still a variable.
PIP_ARTIFACT = re.compile(
    r"/(?:pip|pypi|simple)/(?:[^/\s'\"]+/)*"
    r"([A-Za-z0-9][\w.-]*?)-(?:\d[\w.]*|\$\{?[\w:]+\}?)\.(?:tar\.gz|zip|whl)"
)
RPM_ARTIFACT = re.compile(
    r"([A-Za-z0-9][\w.+-]*?)-\d[\w.]*-[\w.]+\.(?:x86_64|noarch|aarch64|i686)\.rpm"
)
# Providers that install something other than an OS package, by link kind;
# None means a kind no project provides.
PROVIDER_KINDS: dict[str, str | None] = {
    "pip": "pypi",
    "pip3": "pypi",
    "gem": None,
    "npm": None,
}
MAX_VALUES = 16

USES_IMAGE = "uses_image"
INSTALLS = "installs"


def lookup_keys(content: str) -> list[str]:
    """Return the data keys a manifest reads directly."""
    return [key.lstrip(":") for key in LOOKUP_KEY.findall(content)]


def class_parameters(content: str) -> tuple[str, dict[str, str]]:
    """Return the class a manifest declares and its literal parameter defaults."""
    head = CLASS_HEAD.search(content)
    if head is None:
        return "", {}
    defaults = {
        name: raw[1:-1] for name, raw in PARAMETER_DEFAULT.findall(head.group(2) or "")
    }
    return head.group(1).lstrip(":"), defaults


class _Scope:
    """The variables one manifest can see, resolved on demand."""

    def __init__(self, content: str, lookup: Lookup | None) -> None:
        self.owner, self.defaults = class_parameters(content)
        self.raw = dict(ASSIGNMENT.findall(content))
        for name, key in LOOKUP_ASSIGNMENT.findall(content):
            self.raw[name] = f"{LOOKUP_PREFIX}{key}"
        self.lookup = lookup
        self.resolving: set[str] = set()

    def variable(self, name: str) -> list[str]:
        name = name.lstrip(":")
        if name in self.resolving:
            return []
        self.resolving.add(name)
        try:
            if name in self.raw:
                return self.value(self.raw[name])
            if "::" not in name and self.lookup is not None and self.owner:
                found = self.lookup(f"{self.owner}::{name}")
                if found:
                    return found
            if "::" not in name:
                return [self.defaults[name]] if name in self.defaults else []
            return self.lookup(name) if self.lookup is not None else []
        finally:
            self.resolving.discard(name)

    def value(self, raw: str) -> list[str]:
        """Return what a literal, a variable or an interpolated string can be."""
        if raw.startswith(LOOKUP_PREFIX):
            key = raw[len(LOOKUP_PREFIX) :]
            return self.lookup(key) if self.lookup is not None else []
        if raw.startswith("$"):
            return self.variable(raw.strip("${}"))
        if raw.startswith("'"):
            return [raw[1:-1]]
        return self.interpolate(raw[1:-1] if raw.startswith('"') else raw)

    def interpolate(self, text: str) -> list[str]:
        """Return every string the interpolations of a text can make."""
        parts = INTERPOLATION.split(text)
        # split() yields text, group 1, group 2, text, ... for every match.
        choices: list[list[str]] = []
        for index, part in enumerate(parts):
            if index % 3 == 0:
                choices.append([part])
            elif part is not None:
                found = self.variable(part)
                choices.append(found[:MAX_VALUES] or [f"${{{part}}}"])
        return [
            "".join(combination)
            for combination in itertools.islice(itertools.product(*choices), MAX_VALUES)
        ]


def _titles(raw: str, scope: _Scope) -> list[str]:
    if not raw.startswith("["):
        return scope.value(raw)
    names: list[str] = []
    for single, double, variable in LIST_ITEM.findall(raw):
        names.extend(scope.value(variable) if variable else [single or double])
    return names


def puppet_uses(content: str, lookup: Lookup | None = None) -> list[tuple[str, str]]:
    """Return (placeholder target, relation) for every image and package.

    `lookup` resolves what the manifest alone cannot: a parameter of this or
    another class, by `class::param`.
    """
    scope = _Scope(content, lookup)
    found: list[tuple[str, str]] = []
    for raw in IMAGE_ATTRIBUTE.findall(content):
        found.extend((f"image:{image}", USES_IMAGE) for image in scope.value(raw))
    for match in PACKAGE_TITLE.finditer(content):
        body = content[match.end() : content.find("}", match.end())]
        provider = PROVIDER.search(body)
        kind = (
            PROVIDER_KINDS.get(provider.group(1), "package") if provider else "package"
        )
        if kind is not None:
            found.extend(
                (f"{kind}:{name}", INSTALLS) for name in _titles(match.group(1), scope)
            )
    for pattern in (ENSURE_PACKAGES, ENSURE_RESOURCE):
        for raw in pattern.findall(content):
            found.extend((f"package:{name}", INSTALLS) for name in _titles(raw, scope))
    for variable, key in CREATE_RESOURCES.findall(content):
        raw = f"{LOOKUP_PREFIX}{key}" if key else variable
        found.extend((f"package:{name}", INSTALLS) for name in scope.value(raw))
    for single, double in STRING.findall(content):
        for text in [single] if single else scope.interpolate(double):
            found.extend(
                (f"pypi:{name}", INSTALLS) for name in PIP_ARTIFACT.findall(text)
            )
            found.extend(
                (f"package:{name}", INSTALLS) for name in RPM_ARTIFACT.findall(text)
            )
    return [
        (target, relation)
        for target, relation in dict.fromkeys(found)
        if "$" not in target and target.split(":", 1)[1].strip()
    ]
