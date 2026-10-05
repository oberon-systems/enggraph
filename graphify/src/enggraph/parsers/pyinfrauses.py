"""Read what a pyinfra deploy installs and runs, following its config.

A module's code calls pyinfra operations, and the values it hands them come
from its config: what the data sets, else the defaults and properties of the
config model. A value nothing resolves is left out.
"""

from __future__ import annotations

import ast
import itertools
from collections.abc import Callable, Iterator

from enggraph.parsers.puppetuses import MAX_VALUES, PIP_ARTIFACT, RPM_ARTIFACT

Lookup = Callable[[str], list[str]]

OPERATIONS_MODULE = "pyinfra.operations"
# Package operations by the link kind of what they install.
PACKAGE_OPERATIONS = {
    "apk": "package",
    "apt": "package",
    "brew": "package",
    "dnf": "package",
    "pacman": "package",
    "pkg": "package",
    "pkgin": "package",
    "xbps": "package",
    "yum": "package",
    "zypper": "package",
    "pip": "pypi",
    "pipx": "pypi",
    "npm": "npm",
}
IMAGE_OPERATIONS = ("container", "image")
# What a package can be pinned to instead of a version: `name-present` is the
# branch of a `name` or `name-version` choice that never runs.
PACKAGE_STATES = frozenset({"present", "absent", "latest", "installed", "removed"})
CONFIG_NAMES = frozenset({"config", "cfg", "conf", "settings", "self"})
PROPERTY_DECORATORS = frozenset({"property", "cached_property", "computed_field"})
COLLECTION_CALLS = frozenset({"sorted", "list", "set", "tuple", "str"})
MAPPING_VIEWS = frozenset({"items", "keys", "values"})

INSTALLS = "installs"
USES_IMAGE = "uses_image"

Function = ast.FunctionDef | ast.AsyncFunctionDef


def _is_config(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in CONFIG_NAMES
    return isinstance(node, ast.Attribute) and node.attr in CONFIG_NAMES


def _constant(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
        if isinstance(node.value, str | int | float):
            return [str(node.value)]
    return []


def _product(choices: list[list[str]]) -> list[str]:
    if any(not choice for choice in choices):
        return []
    return [
        "".join(parts)
        for parts in itertools.islice(itertools.product(*choices), MAX_VALUES)
    ]


class _Module:
    """The code of one module: its functions, its model fields and properties."""

    def __init__(self, trees: list[ast.Module], lookup: Lookup) -> None:
        self.trees = trees
        self.lookup = lookup
        self.functions: dict[str, Function] = {}
        self.defaults: dict[str, list[str]] = {}
        self.properties: dict[str, tuple[ast.expr, Function]] = {}
        self.resolving: set[str] = set()
        self.bound: dict[int, dict[str, list[str]]] = {}
        for tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    self.functions.setdefault(node.name, node)
                elif isinstance(node, ast.ClassDef):
                    self._read_class(node)

    def _read_class(self, node: ast.ClassDef) -> None:
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and statement.value is not None:
                if isinstance(statement.target, ast.Name):
                    found = _constant(statement.value)
                    if found:
                        self.defaults.setdefault(statement.target.id, found)
            elif isinstance(statement, ast.FunctionDef):
                decorators = {
                    getattr(one, "id", getattr(one, "attr", ""))
                    for one in statement.decorator_list
                }
                returned = [
                    one.value
                    for one in statement.body
                    if isinstance(one, ast.Return) and one.value is not None
                ]
                if decorators & PROPERTY_DECORATORS and returned:
                    self.properties.setdefault(
                        statement.name, (returned[-1], statement)
                    )

    def attribute(self, name: str) -> list[str]:
        """Return what a config attribute can be."""
        if name in self.resolving:
            return []
        self.resolving.add(name)
        try:
            found = self.lookup(name)
            if found:
                return found
            if name in self.properties:
                expression, function = self.properties[name]
                return self.value(expression, function)
            return self.defaults.get(name, [])
        finally:
            self.resolving.discard(name)

    def _local(self, name: str, function: Function | None) -> list[str]:
        if function is None:
            return []
        bound = self.bound.get(id(function), {})
        if name in bound:
            return bound[name]
        found: list[str] = []
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and any(
                isinstance(one, ast.Name) and one.id == name for one in node.targets
            ):
                found.extend(self.value(node.value, function))
            elif (
                isinstance(node, ast.AnnAssign | ast.NamedExpr)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
                and node.value is not None
            ):
                found.extend(self.value(node.value, function))
            elif isinstance(node, ast.For | ast.comprehension):
                target = node.target
                if isinstance(target, ast.Tuple) and target.elts:
                    target = target.elts[0]
                if isinstance(target, ast.Name) and target.id == name:
                    found.extend(self.value(node.iter, function))
        return found[:MAX_VALUES]

    def _helper(self, function: Function) -> list[str]:
        """Return what the config attributes a helper reads can be."""
        found: list[str] = []
        for node in ast.walk(function):
            if isinstance(node, ast.Attribute) and _is_config(node.value):
                found.extend(self.attribute(node.attr))
        return found[:MAX_VALUES]

    def _returned(
        self, helper: Function, node: ast.Call, caller: Function | None
    ) -> list[str]:
        """Return what a helper gives back, its parameters bound to the call."""
        parameters = [one.arg for one in helper.args.args]
        bound = {
            name: self.value(argument, caller)
            for name, argument in zip(parameters, node.args, strict=False)
        }
        bound.update(
            (one.arg, self.value(one.value, caller)) for one in node.keywords if one.arg
        )
        self.bound[id(helper)] = bound
        try:
            found = [
                value
                for one in ast.walk(helper)
                if isinstance(one, ast.Return) and one.value is not None
                for value in self.value(one.value, helper)
            ]
            # A helper building a list returns what the config attributes it reads hold.
            return (found or self._helper(helper))[:MAX_VALUES]
        finally:
            del self.bound[id(helper)]

    def _call(self, node: ast.Call, function: Function | None) -> list[str]:
        callee = node.func
        if isinstance(callee, ast.Name):
            if callee.id in COLLECTION_CALLS and node.args:
                return self.value(node.args[0], function)
            helper = self.functions.get(callee.id)
            if helper is None or id(helper) in self.bound:
                return []
            return self._returned(helper, node, function)
        if not isinstance(callee, ast.Attribute):
            return []
        if callee.attr in MAPPING_VIEWS:
            return self.value(callee.value, function)
        if callee.attr == "join" and not isinstance(callee.value, ast.Constant):
            choices: list[list[str]] = []
            for index, argument in enumerate(node.args):
                choices.extend([["/"]] if index else [])
                choices.append(self.value(argument, function))
            return _product(choices)
        if callee.attr == "format":
            return self._format(node, callee.value, function)
        return []

    def _format(
        self, node: ast.Call, template: ast.expr, function: Function | None
    ) -> list[str]:
        results: list[str] = []
        for text in self.value(template, function):
            filled = [text]
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue
                given = self.value(keyword.value, function)
                placeholder = "{" + keyword.arg + "}"
                filled = [
                    one.replace(placeholder, value)
                    for one in filled
                    for value in (given if placeholder in one else [""])
                ][:MAX_VALUES]
            results.extend(filled)
        return results[:MAX_VALUES]

    def value(self, node: ast.expr, function: Function | None) -> list[str]:
        """Return every string an expression can be, at most MAX_VALUES."""
        if isinstance(node, ast.Constant):
            return _constant(node)
        if isinstance(node, ast.JoinedStr):
            return _product(
                [
                    _constant(part)
                    if isinstance(part, ast.Constant)
                    else self.value(part.value, function)
                    for part in node.values
                    if isinstance(part, ast.Constant | ast.FormattedValue)
                ]
            )
        if isinstance(node, ast.List | ast.Tuple | ast.Set):
            return [value for one in node.elts for value in self.value(one, function)][
                :MAX_VALUES
            ]
        if isinstance(node, ast.Attribute):
            return self.attribute(node.attr) if _is_config(node.value) else []
        if isinstance(node, ast.Name):
            return self._local(node.id, function)
        if isinstance(node, ast.Call):
            return self._call(node, function)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return _product(
                [self.value(node.left, function), self.value(node.right, function)]
            )
        if isinstance(node, ast.IfExp):
            return self.value(node.body, function) + self.value(node.orelse, function)
        if isinstance(node, ast.BoolOp):
            return [
                value for one in node.values for value in self.value(one, function)
            ][:MAX_VALUES]
        return []


def _operations(tree: ast.Module) -> dict[str, str]:
    """Return the names the pyinfra operation modules are bound to here."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == OPERATIONS_MODULE:
            for alias in node.names:
                bound[alias.asname or alias.name] = alias.name
    return bound


def _calls(tree: ast.Module) -> Iterator[tuple[ast.Call, Function | None]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    yield inner, node
    for statement in tree.body:
        if not isinstance(
            statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            for inner in ast.walk(statement):
                if isinstance(inner, ast.Call):
                    yield inner, None


def _argument(node: ast.Call, keyword: str, position: int) -> ast.expr | None:
    for one in node.keywords:
        if one.arg == keyword:
            return one.value
    return node.args[position] if len(node.args) > position else None


def _artifacts(values: list[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for text in values:
        found.extend(("package", name) for name in RPM_ARTIFACT.findall(text))
        found.extend(("pypi", name) for name in PIP_ARTIFACT.findall(text))
    return found


def _pinned(text: str, names: set[str]) -> bool:
    """Report whether a value is another name of the call with a version or state."""
    base, separator, suffix = text.rpartition("-")
    return (
        bool(separator)
        and base in names
        and (suffix[:1].isdigit() or suffix in PACKAGE_STATES)
    )


def _plain(kind: str, values: list[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    names = set(values)
    for text in values:
        if "/" in text or text.endswith((".rpm", ".deb", ".whl", ".tar.gz")):
            found.extend(_artifacts([text]))
        elif (
            text
            and not any(mark in text for mark in "{}$ ")
            and not _pinned(text, names)
        ):
            found.append((kind, text))
    return found


def pyinfra_uses(sources: dict[str, str], lookup: Lookup) -> list[tuple[str, str]]:
    """Return (placeholder target, relation) for what one module's code takes.

    `sources` is the module's Python files; `lookup` returns what the data sets
    under one attribute of the module's config.
    """
    trees: list[ast.Module] = []
    for content in sources.values():
        try:
            trees.append(ast.parse(content))
        except (SyntaxError, ValueError):
            continue
    module = _Module(trees, lookup)
    found: list[tuple[str, str, str]] = []
    for tree in trees:
        bound = _operations(tree)
        for call, function in _calls(tree):
            callee = call.func
            if not (
                isinstance(callee, ast.Attribute)
                and isinstance(callee.value, ast.Name)
                and callee.value.id in bound
            ):
                continue
            operation, verb = bound[callee.value.id], callee.attr
            if verb == "packages" and operation in PACKAGE_OPERATIONS:
                argument = _argument(call, "packages", 0)
                values = module.value(argument, function) if argument else []
                kind = PACKAGE_OPERATIONS[operation]
                found.extend((k, n, INSTALLS) for k, n in _plain(kind, values))
            elif operation == "files" and verb == "download":
                argument = _argument(call, "src", 0)
                values = module.value(argument, function) if argument else []
                found.extend((k, n, INSTALLS) for k, n in _artifacts(values))
            elif operation == "docker" and verb in IMAGE_OPERATIONS:
                argument = _argument(call, "image", 0 if verb == "image" else 1)
                values = module.value(argument, function) if argument else []
                found.extend(("image", one, USES_IMAGE) for one in values)
    return list(
        dict.fromkeys((f"{kind}:{name}", relation) for kind, name, relation in found)
    )
