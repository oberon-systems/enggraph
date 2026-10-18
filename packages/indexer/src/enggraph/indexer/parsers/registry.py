"""Pick the parser for a path, and say what a path belongs to."""

from __future__ import annotations

import hashlib
import posixpath
from functools import cache
from pathlib import Path

from enggraph.core.config import (
    EXTRA_SOURCE_EXTENSIONS,
    GRAPHIFYY_EXTENSIONS,
    IGNORED_FILE_NAMES,
)
from enggraph.indexer.parsers.ansible import AnsibleParser
from enggraph.indexer.parsers.base import CodeParser
from enggraph.indexer.parsers.compose import ComposeParser, is_compose_name
from enggraph.indexer.parsers.languages import (
    BashParser,
    CParser,
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
    ".cjs": JavaScriptParser,
    ".cs": CSharpParser,
    ".dockerfile": DockerfileParser,
    ".epp": EppParser,
    ".erb": ErbParser,
    ".go": GoParser,
    ".h": CParser,
    ".hcl": HCLParser,
    ".htm": HtmlParser,
    ".html": HtmlParser,
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


@cache
def parsers_revision() -> str:
    """Fingerprint the parser sources of this package.

    The indexer skips a file whose content is unchanged, so a parser that
    starts naming its nodes differently would leave the old nodes in place
    while emitting edges against the new ones. Mixing this into the stored
    hash makes a parser change invalidate the tree by itself.
    """
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


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
    _, extension = posixpath.splitext(file_name.lower())
    return (
        file_name.endswith(DEFAULT_SOURCE_EXTENSIONS)
        or extension in GRAPHIFYY_EXTENSIONS
        or parser_class(file_path) is not None
    )


def language_family(rel_path: str) -> str:
    """Return the symbol namespace a file belongs to."""
    matched = parser_class(rel_path)
    return matched.FAMILY if matched else ""
