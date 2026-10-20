"""Pick the parser for a path, and say what a path belongs to."""

from __future__ import annotations

import ast
import hashlib
import inspect
import posixpath
import sys
from functools import cache

from enggraph.core.config import (
    EXTRA_SOURCE_EXTENSIONS,
    IGNORED_FILE_NAMES,
)
from enggraph.indexer.parsers.ansible import AnsibleParser
from enggraph.indexer.parsers.base import CodeParser
from enggraph.indexer.parsers.compose import ComposeParser, is_compose_name
from enggraph.indexer.parsers.languages import (
    BashParser,
    CParser,
    CppParser,
    CSharpParser,
    DockerfileParser,
    EppParser,
    ErbParser,
    GoParser,
    HCLParser,
    HtmlParser,
    JavaParser,
    JavaScriptParser,
    JsonParser,
    KotlinParser,
    MakeParser,
    MarkdownParser,
    PhpParser,
    PhtmlParser,
    PuppetParser,
    PythonParser,
    RubyParser,
    RustParser,
    ScalaParser,
    TOMLParser,
    TSXParser,
    TypeScriptParser,
)
from enggraph.indexer.parsers.pymanifest import (
    SETUP_CFG,
    RequirementsParser,
    SetupCfgParser,
    is_requirements_name,
)
from enggraph.indexer.parsers.rpmspec import RpmSpecParser
from enggraph.indexer.parsers.systemd import UNIT_EXTENSIONS, SystemdParser

EXTENSION_PARSERS: dict[str, type[CodeParser]] = {
    ".bash": BashParser,
    ".c": CParser,
    ".cc": CppParser,
    ".cjs": JavaScriptParser,
    ".cpp": CppParser,
    ".cs": CSharpParser,
    ".cxx": CppParser,
    ".dockerfile": DockerfileParser,
    ".epp": EppParser,
    ".erb": ErbParser,
    ".go": GoParser,
    ".h": CppParser,
    ".hcl": HCLParser,
    ".hh": CppParser,
    ".hpp": CppParser,
    ".htm": HtmlParser,
    ".html": HtmlParser,
    ".hxx": CppParser,
    ".java": JavaParser,
    ".js": JavaScriptParser,
    ".jsx": JavaScriptParser,
    ".json": JsonParser,
    ".kt": KotlinParser,
    ".markdown": MarkdownParser,
    ".md": MarkdownParser,
    ".mjs": JavaScriptParser,
    ".mk": MakeParser,
    ".php": PhpParser,
    ".phtml": PhtmlParser,
    ".pp": PuppetParser,
    ".py": PythonParser,
    ".rb": RubyParser,
    ".rs": RustParser,
    ".scala": ScalaParser,
    ".sh": BashParser,
    ".spec": RpmSpecParser,
    ".tf": HCLParser,
    ".tfvars": HCLParser,
    ".toml": TOMLParser,
    ".ts": TypeScriptParser,
    ".tsx": TSXParser,
    ".yaml": AnsibleParser,
    ".yml": AnsibleParser,
    **dict.fromkeys(UNIT_EXTENSIONS, SystemdParser),
}
FILENAME_PARSERS: dict[str, type[CodeParser]] = {
    "containerfile": DockerfileParser,
    "dockerfile": DockerfileParser,
    "gnumakefile": MakeParser,
    "makefile": MakeParser,
    SETUP_CFG: SetupCfgParser,
}
DEFAULT_SOURCE_EXTENSIONS = tuple(sorted(EXTENSION_PARSERS)) + EXTRA_SOURCE_EXTENSIONS


# What every parser source hashed to when file hashes were mixed with it, before
# each parser had a revision of its own: kept to recognise a hash stored so.
LEGACY_PARSERS_REVISION = "5ea197e18134b789"


# What a module named as a whole, `yamldocs.load(...)`, is folded in under.
WHOLE_MODULE = "*"


@cache
def _module_parts(module_name: str) -> dict[str, str]:
    """Map each top-level name of a module to the source that defines it."""
    source = inspect.getsource(sys.modules[module_name])
    parts: dict[str, str] = {WHOLE_MODULE: source}
    for node in ast.parse(source).body:
        segment = ast.get_source_segment(source, node) or ""
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            parts[node.name] = segment
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        parts[name.id] = segment
    return parts


def _fold(
    module_name: str, name: str, seen: set[tuple[str, str]], out: list[str]
) -> None:
    """Collect one definition and every definition of this package it names."""
    if (module_name, name) in seen:
        return
    seen.add((module_name, name))
    segment = _module_parts(module_name).get(name)
    if segment is None:
        value = getattr(sys.modules[module_name], name, None)
        if inspect.ismodule(value) and value.__name__.startswith("enggraph."):
            _fold(value.__name__, WHOLE_MODULE, seen, out)
            return
        owner = getattr(value, "__module__", None)
        if (
            isinstance(owner, str)
            and owner.startswith("enggraph.")
            and owner != module_name
        ):
            _fold(owner, getattr(value, "__name__", name), seen, out)
        return
    out.append(f"{module_name}.{name}\0{segment}\0")
    for node in ast.walk(ast.parse(segment)):
        if isinstance(node, ast.Name):
            _fold(module_name, node.id, seen, out)


@cache
def parser_revision(matched: type[CodeParser] | None) -> str:
    """Fingerprint the source one parser runs: its class and all it names.

    A file is read again when this moves, so a parser that starts naming its
    nodes differently invalidates its own files and no one else's.
    """
    if matched is None:
        return "none"
    out: list[str] = []
    _fold(matched.__module__, matched.__name__, set(), out)
    return hashlib.sha256("".join(out).encode("utf-8")).hexdigest()[:16]


def revision_for(file_path: str) -> str:
    """Return the revision of the parser reading a path."""
    return parser_revision(parser_class(file_path))


@cache
def _parser_instance(matched: type[CodeParser]) -> CodeParser:
    """Return the shared instance of a parser class.

    Compiling the queries costs more than parsing a small file, so the
    instances are reused across the whole run.
    """
    return matched()


def parser_class(file_path: str) -> type[CodeParser] | None:
    """Return the parser class matching a path, by file name then extension."""
    file_name = posixpath.basename(file_path).lower()
    if file_name in FILENAME_PARSERS:
        return FILENAME_PARSERS[file_name]
    # Dockerfile.dev, Dockerfile.ci and friends.
    if file_name.startswith("dockerfile."):
        return DockerfileParser
    # compose.yaml, docker-compose.prod.yml and friends. A compose file under
    # any other name reaches AnsibleParser, which hands it back by its shape.
    if is_compose_name(file_name):
        return ComposeParser
    if is_requirements_name(file_path.lower()):
        return RequirementsParser
    _, extension = posixpath.splitext(file_name)
    return EXTENSION_PARSERS.get(extension)


def get_parser(file_path: str) -> CodeParser | None:
    """Return a parser for the file, or None when the type has no grammar."""
    matched = parser_class(file_path)
    return _parser_instance(matched) if matched else None


def is_default_source(file_path: str) -> bool:
    """Report whether some producer reads a file, so it is indexed."""
    file_name = posixpath.basename(file_path)
    if file_name.lower() in IGNORED_FILE_NAMES:
        return False
    return (
        file_name.endswith(DEFAULT_SOURCE_EXTENSIONS)
        or parser_class(file_path) is not None
    )


def language_family(rel_path: str) -> str:
    """Return the symbol namespace a file belongs to."""
    matched = parser_class(rel_path)
    return matched.FAMILY if matched else ""
