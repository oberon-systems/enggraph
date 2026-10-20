"""Language parsers and the dispatch that picks one for a path.

The rest of the package talks to this facade rather than to the individual
grammar modules, so adding a language stays a change inside `parsers`.
"""

from enggraph.indexer.parsers.base import (
    CodeParser,
    node_text,
    strip_literal,
    unique_pairs,
)
from enggraph.indexer.parsers.languages import MarkdownParser
from enggraph.indexer.parsers.registry import (
    DEFAULT_SOURCE_EXTENSIONS,
    LEGACY_PARSERS_REVISION,
    get_parser,
    is_default_source,
    language_family,
    parser_class,
    revision_for,
)

__all__ = [
    "DEFAULT_SOURCE_EXTENSIONS",
    "LEGACY_PARSERS_REVISION",
    "CodeParser",
    "MarkdownParser",
    "get_parser",
    "is_default_source",
    "language_family",
    "node_text",
    "parser_class",
    "revision_for",
    "strip_literal",
    "unique_pairs",
]
