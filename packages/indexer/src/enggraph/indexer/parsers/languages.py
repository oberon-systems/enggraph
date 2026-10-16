"""One parser per tree-sitter grammar.

Each class is a pair of queries; the behaviour lives in CodeParser.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from typing import Any

import tree_sitter_bash
import tree_sitter_c_sharp
import tree_sitter_css
import tree_sitter_dockerfile
import tree_sitter_embedded_template
import tree_sitter_go
import tree_sitter_hcl
import tree_sitter_html
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_json
import tree_sitter_kotlin
import tree_sitter_make
import tree_sitter_markdown
import tree_sitter_php
import tree_sitter_puppet
import tree_sitter_python
import tree_sitter_ruby
import tree_sitter_rust
import tree_sitter_toml
import tree_sitter_typescript
import tree_sitter_yaml
from tree_sitter import Language, Node, Parser, Query, QueryCursor

from enggraph.indexer.parsers.base import (
    CALLS,
    DECLARES,
    IMPORTS,
    IMPORTS_NAME,
    INHERITS,
    RECEIVER,
    CodeParser,
    CodeReference,
    CodeSymbol,
    node_text,
    qualified,
    strip_literal,
    symbol_key,
    unique_pairs,
)
from enggraph.indexer.parsers.puppetuses import puppet_uses
from enggraph.indexer.parsers.terraform import terraform_relations

# graphifyy starts an arrow function's node on its declarator and a method's on
# the definition, so both lines are offered.
ECMASCRIPT_SCOPE_TYPES = frozenset(
    {
        "function_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
        "variable_declarator",
        "class_declaration",
    }
)


class PythonParser(CodeParser):
    """Python specific AST parser."""

    FAMILY = "python"
    SCOPE_TYPES = frozenset({"function_definition", "class_definition"})
    ENTITY_QUERY = """
        (module (function_definition name: (identifier) @function))
        (module (decorated_definition
            definition: (function_definition name: (identifier) @function)))
        (module (class_definition name: (identifier) @class))
        (module (decorated_definition
            definition: (class_definition name: (identifier) @class)))
        (class_definition body:
            (block (function_definition name: (identifier) @method)))
        (class_definition body: (block (decorated_definition
            definition: (function_definition name: (identifier) @method))))
    """
    RELATION_QUERY = """
        (call function: (identifier) @calls)
        (call function: (attribute attribute: (identifier) @calls))
        (class_definition superclasses: (argument_list (identifier) @inherits))
        (class_definition superclasses:
            (argument_list (attribute attribute: (identifier) @inherits)))
        (import_statement name: (dotted_name) @imports)
        (import_statement name: (aliased_import name: (dotted_name) @imports))
        (import_from_statement module_name: (dotted_name) @imports)
        (import_from_statement module_name: (relative_import) @imports)
    """

    def __init__(self) -> None:
        """Initialize Python parser."""
        super().__init__(tree_sitter_python.language())

    def extract_code(
        self, content: str
    ) -> tuple[list[CodeSymbol], list[CodeReference]]:
        """Return the classes, functions and methods, and what they refer to.

        A function nested in another is no symbol: its calls belong to the
        function around it.
        """
        symbols: list[CodeSymbol] = []
        references: dict[tuple[str, str, str, str], CodeReference] = {}

        def refer(scope: str, kind: str, name: str, receiver: str, node: Node) -> None:
            if name:
                references.setdefault(
                    (scope, kind, name, receiver),
                    CodeReference(scope, kind, name, receiver, node.start_point[0] + 1),
                )

        stack: list[tuple[Node, str, str, bool]] = [
            (self.parse(content), "", "", False)
        ]
        while stack:
            node, scope, owner, nested = stack.pop()
            children = [(child, scope, owner, nested) for child in node.named_children]
            if node.type == "import_statement":
                for name in node.children_by_field_name("name"):
                    refer("", IMPORTS, python_module_name(name), "", node)
                continue
            if node.type == "import_from_statement":
                module_node = node.child_by_field_name("module_name")
                module = node_text(module_node).strip() if module_node else ""
                refer("", IMPORTS, module, "", node)
                joiner = "" if module.endswith(".") else "."
                for name in node.children_by_field_name("name"):
                    taken = python_module_name(name)
                    if module and taken:
                        refer("", IMPORTS_NAME, f"{module}{joiner}{taken}", "", node)
                continue
            name_node = node.child_by_field_name("name")
            name = node_text(name_node).strip() if name_node else ""
            line = node.start_point[0] + 1
            if node.type == "class_definition" and name and not nested:
                key = symbol_key(name, line)
                symbols.append(CodeSymbol(key, name, "class", line, owner))
                bases = node.child_by_field_name("superclasses")
                for base in bases.named_children if bases else []:
                    if base.type == "identifier":
                        refer(key, INHERITS, node_text(base), "", base)
                    elif base.type == "attribute":
                        refer(key, INHERITS, *attribute_call(base), base)
                body = node.child_by_field_name("body")
                children = [(body, key, key, False)] if body else []
            elif node.type == "function_definition" and name and not nested:
                label = f".{name}()" if owner else f"{name}()"
                key = symbol_key(label, line)
                symbols.append(
                    CodeSymbol(
                        key, label, "method" if owner else "function", line, owner
                    )
                )
                parameters = node.child_by_field_name("parameters")
                body = node.child_by_field_name("body")
                children = [(parameters, scope, owner, nested)] if parameters else []
                if body:
                    children.append((body, key, "", True))
            elif node.type == "call":
                function = node.child_by_field_name("function")
                if function is not None and function.type == "identifier":
                    refer(scope, CALLS, node_text(function), "", node)
                elif function is not None and function.type == "attribute":
                    refer(scope, CALLS, *attribute_call(function), node)
            stack.extend(reversed(children))
        return symbols, list(references.values())


def python_module_name(node: Node) -> str:
    """Return the dotted name an import names, without its alias."""
    if node.type == "aliased_import":
        target = node.child_by_field_name("name")
        return node_text(target).strip() if target else ""
    return node_text(node).strip()


def attribute_call(node: Node) -> tuple[str, str]:
    """Split `receiver.name` into the name and the receiver text."""
    attribute = node.child_by_field_name("attribute")
    receiver = node.child_by_field_name("object")
    return (
        node_text(attribute).strip() if attribute else "",
        node_text(receiver).strip() if receiver else "",
    )


class TypeScriptParser(CodeParser):
    """TypeScript specific AST parser."""

    FAMILY = "ecmascript"
    SCOPE_TYPES = ECMASCRIPT_SCOPE_TYPES
    ENTITY_QUERY = """
        (function_declaration name: (identifier) @function)
        (class_declaration name: (type_identifier) @class)
        (interface_declaration name: (type_identifier) @interface)
        (type_alias_declaration name: (type_identifier) @type)
        (method_definition name: (property_identifier) @method)
    """
    RELATION_QUERY = """
        (call_expression function: (identifier) @calls)
        (call_expression function:
            (member_expression property: (property_identifier) @calls))
        (class_declaration (class_heritage (extends_clause value: (_) @inherits)))
        (class_declaration (class_heritage
            (implements_clause (type_identifier) @inherits)))
        (interface_declaration (extends_type_clause (type_identifier) @inherits))
        (import_statement source: (string) @imports)
        (call_expression
            function: (import)
            arguments: (arguments (string) @imports))
    """

    def __init__(self) -> None:
        """Initialize TypeScript parser."""
        super().__init__(tree_sitter_typescript.language_typescript())

    def extract_code(
        self, content: str
    ) -> tuple[list[CodeSymbol], list[CodeReference]]:
        """Return the classes, interfaces, functions and methods, and their uses."""
        return ecmascript_code(self.parse(content))


ECMASCRIPT_FUNCTIONS = frozenset(
    {"function_declaration", "generator_function_declaration"}
)
ECMASCRIPT_CLASSES = frozenset({"class_declaration", "abstract_class_declaration"})
ECMASCRIPT_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression"})
ECMASCRIPT_MEMBER_NAMES = frozenset(
    {"property_identifier", "private_property_identifier"}
)
# TypeScript names a class field by `name`, JavaScript by `property`.
ECMASCRIPT_FIELDS = frozenset({"public_field_definition", "field_definition"})
JSX_ELEMENTS = frozenset({"jsx_opening_element", "jsx_self_closing_element"})


def ecmascript_code(root: Node) -> tuple[list[CodeSymbol], list[CodeReference]]:
    """Walk an ECMAScript tree into symbols and unresolved references.

    A function declared inside another is no symbol: its calls belong to the
    symbol around it. Lines are the ones the upstream extractor gave its ids.
    """
    symbols: list[CodeSymbol] = []
    references: dict[tuple[str, str, str, str], CodeReference] = {}

    def refer(scope: str, kind: str, name: str, receiver: str, node: Node) -> None:
        if name:
            references.setdefault(
                (scope, kind, name, receiver),
                CodeReference(scope, kind, name, receiver, node.start_point[0] + 1),
            )

    def declare(label: str, kind: str, node: Node, parent: str) -> str:
        line = node.start_point[0] + 1
        key = symbol_key(label, line)
        symbols.append(CodeSymbol(key, label, kind, line, parent))
        return key

    stack: list[tuple[Node, str, str, bool]] = [(root, "", "", False)]
    while stack:
        node, scope, owner, nested = stack.pop()
        children = [(child, scope, owner, nested) for child in node.named_children]
        kind = node.type
        if kind in ("import_statement", "export_statement"):
            source = module_source(node)
            if source:
                refer("", IMPORTS, source, "", node)
                continue
        name_node = node.child_by_field_name("name")
        name = node_text(name_node).strip() if name_node else ""
        top = bool(name) and not nested and not owner
        if kind in ECMASCRIPT_FUNCTIONS and top:
            key = declare(f"{name}()", "function", node, owner)
            children = enter_body(node, key, scope, owner, nested)
        elif kind in ECMASCRIPT_CLASSES and name and not nested:
            key = declare(name, "class", node, owner)
            for base, receiver, base_node in heritage(node):
                refer(key, INHERITS, base, receiver, base_node)
            children = [
                (child, scope, owner, nested)
                for child in node.named_children
                if child.type == "decorator"
            ]
            body = node.child_by_field_name("body")
            if body:
                children.append((body, key, key, False))
        elif kind == "interface_declaration" and top:
            key = declare(name, "interface", node, owner)
            for base, receiver, base_node in heritage(node):
                refer(key, INHERITS, base, receiver, base_node)
            children = []
        elif kind == "method_definition" and owner and not nested:
            if name_node is not None and name_node.type in ECMASCRIPT_MEMBER_NAMES:
                key = declare(f".{name}()", "method", node, owner)
                children = enter_body(node, key, scope, owner, nested)
        elif kind in ECMASCRIPT_FIELDS and owner and not nested:
            value = node.child_by_field_name("value")
            field = name_node or node.child_by_field_name("property")
            if (
                value is not None
                and value.type in ECMASCRIPT_FUNCTION_VALUES
                and field is not None
                and field.type in ECMASCRIPT_MEMBER_NAMES
            ):
                key = declare(f".{node_text(field).strip()}()", "method", node, owner)
                children = [(value, key, "", True)]
        elif kind == "variable_declarator" and top:
            value = node.child_by_field_name("value")
            if (
                value is not None
                and value.type in ECMASCRIPT_FUNCTION_VALUES
                and name_node is not None
                and name_node.type == "identifier"
            ):
                key = declare(f"{name}()", "function", node, owner)
                children = [(value, key, "", True)]
        elif kind == "call_expression":
            function = node.child_by_field_name("function")
            module = module_argument(node)
            if function is not None and function.type == "import":
                refer("", IMPORTS, module, "", node)
            elif module and callee(function) == ("require", ""):
                refer("", IMPORTS, module, "", node)
            else:
                refer(scope, CALLS, *callee(function), node)
        elif kind == "new_expression":
            refer(scope, CALLS, *callee(node.child_by_field_name("constructor")), node)
        elif kind in JSX_ELEMENTS and name_node is not None:
            if name_node.type != "identifier" or name[:1].isupper():
                refer(scope, CALLS, *callee(name_node), node)
        stack.extend(reversed(children))
    return symbols, list(references.values())


def enter_body(
    node: Node, key: str, scope: str, owner: str, nested: bool
) -> list[tuple[Node, str, str, bool]]:
    """Walk a declaration's body as the symbol's own scope, the rest as before."""
    body = node.child_by_field_name("body")
    return [
        (child, key, "", True) if child == body else (child, scope, owner, nested)
        for child in node.named_children
    ]


def string_literal(node: Node | None) -> str:
    """Return the text of a plain string literal, empty for anything else."""
    if node is None or node.type != "string":
        return ""
    return strip_literal(node_text(node))


def module_source(node: Node) -> str:
    """Return the module an import or re-export statement names."""
    source = node.child_by_field_name("source")
    if source is None:
        clause = next(
            (one for one in node.named_children if one.type == "import_require_clause"),
            None,
        )
        source = clause.child_by_field_name("source") if clause else None
    return string_literal(source)


def module_argument(node: Node) -> str:
    """Return the string a call is given first, as `require` and `import` take it."""
    arguments = node.child_by_field_name("arguments")
    given = arguments.named_children if arguments is not None else []
    return string_literal(given[0] if given else None)


def callee(node: Node | None) -> tuple[str, str]:
    """Split what is called into its name and the receiver it is called on."""
    if node is None:
        return "", ""
    if node.type in ("identifier", "type_identifier"):
        return node_text(node).strip(), ""
    if node.type == "member_expression":
        target = node.child_by_field_name("property")
        receiver = node.child_by_field_name("object")
    elif node.type == "nested_type_identifier":
        target = node.child_by_field_name("name")
        receiver = node.child_by_field_name("module")
    elif node.type == "generic_type":
        return callee(node.child_by_field_name("name"))
    else:
        return "", ""
    return (
        node_text(target).strip() if target else "",
        node_text(receiver).strip() if receiver else "",
    )


def heritage(node: Node) -> list[tuple[str, str, Node]]:
    """Return what a class extends and implements, or an interface extends.

    The JavaScript grammar puts the base expression straight under
    `class_heritage`; a call there, as in `extends mixin(A)`, names no class.
    """
    targets: list[Node] = []
    for child in node.named_children:
        if child.type == "extends_type_clause":
            targets.extend(child.named_children)
        elif child.type == "class_heritage":
            for clause in child.named_children:
                if clause.type == "extends_clause":
                    targets.extend(clause.children_by_field_name("value"))
                elif clause.type == "implements_clause":
                    targets.extend(clause.named_children)
                else:
                    targets.append(clause)
    bases: list[tuple[str, str, Node]] = []
    for target in targets:
        name, receiver = callee(target)
        if name:
            bases.append((name, receiver, target))
    return bases


class TSXParser(TypeScriptParser):
    """TSX specific AST parser.

    Same queries as TypeScript, but the TSX grammar is a separate language:
    parsing `.tsx` with the plain TypeScript grammar produces error nodes on
    every JSX element.
    """

    def __init__(self) -> None:
        """Initialize TSX parser."""
        CodeParser.__init__(self, tree_sitter_typescript.language_tsx())


class JavaScriptParser(CodeParser):
    """JavaScript specific AST parser."""

    FAMILY = "ecmascript"
    SCOPE_TYPES = ECMASCRIPT_SCOPE_TYPES
    ENTITY_QUERY = """
        (function_declaration name: (identifier) @function)
        (class_declaration name: (identifier) @class)
        (method_definition name: (property_identifier) @method)
        (lexical_declaration (variable_declarator
            name: (identifier) @function value: (arrow_function)))
    """
    RELATION_QUERY = """
        (call_expression function: (identifier) @calls)
        (call_expression function:
            (member_expression property: (property_identifier) @calls))
        (class_declaration (class_heritage (identifier) @inherits))
        (import_statement source: (string) @imports)
    """

    def __init__(self) -> None:
        """Initialize JavaScript parser."""
        super().__init__(tree_sitter_javascript.language())

    def extract_code(
        self, content: str
    ) -> tuple[list[CodeSymbol], list[CodeReference]]:
        """Return the classes, functions and methods, and their uses."""
        return ecmascript_code(self.parse(content))


# Attributes naming another file, by the tag carrying them and the edge each
# becomes.
HTML_ASSET_ATTRIBUTES: dict[tuple[str, str], str] = {
    ("script", "src"): "uses_script",
    ("link", "href"): "uses_style",
    ("img", "src"): "uses_file",
    ("source", "src"): "uses_file",
    ("iframe", "src"): "uses_file",
    ("form", "action"): "uses_file",
}
# A value that names something other than a file of this project: another
# origin, a bare fragment, a scheme of its own, or a placeholder only the
# engine rendering the page can expand.
HTML_FOREIGN_PREFIXES = ("//", "#")
HTML_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
HTML_PLACEHOLDERS = ("{{", "${", "<?", "<%")


def asset_path(value: str) -> str:
    """Return the project path an attribute points at, or "" when it is not one."""
    target = value.strip()
    if not target or target.startswith(HTML_FOREIGN_PREFIXES):
        return ""
    if HTML_SCHEME.match(target) or any(mark in target for mark in HTML_PLACEHOLDERS):
        return ""
    return target.split("?", 1)[0].split("#", 1)[0]


def tag_header(node: Node) -> Node | None:
    """Return the opening tag of an element, in either of its two forms."""
    for child in node.children:
        if child.type in {"start_tag", "self_closing_tag"}:
            return child
    return None


def tag_name(node: Node) -> str:
    """Return the lowercased name of an element."""
    header = tag_header(node)
    if header is None:
        return ""
    for child in header.children:
        if child.type == "tag_name":
            return node_text(child).lower()
    return ""


def attribute_value(node: Node) -> str:
    """Return the value of an attribute, quoted or bare."""
    for child in node.children:
        if child.type == "attribute_value":
            return node_text(child)
        if child.type == "quoted_attribute_value":
            for inner in child.children:
                if inner.type == "attribute_value":
                    return node_text(inner)
            return ""
    return ""


def tag_attributes(node: Node) -> dict[str, str]:
    """Return the attributes of an element, lowercased names to raw values."""
    header = tag_header(node)
    if header is None:
        return {}
    found: dict[str, str] = {}
    for attribute in header.children:
        if attribute.type != "attribute":
            continue
        for child in attribute.children:
            if child.type == "attribute_name":
                found[node_text(child).lower()] = attribute_value(attribute)
    return found


def element_text(node: Node) -> str:
    """Return the direct text of an element, with its whitespace collapsed."""
    parts = [node_text(child) for child in node.children if child.type == "text"]
    return " ".join(" ".join(parts).split())


def local_relations(
    relations: list[dict[str, str]], declared: set[str]
) -> list[dict[str, str]]:
    """Drop the symbol relations whose target this file does not declare itself.

    A symbol edge resolves against the entities our own parsers wrote, and the
    code in the `.js` and `.php` files next to this one belongs to the code
    extractor - so a call leaving the file would become an unresolved
    placeholder rather than an edge worth having.
    """
    return [
        relation
        for relation in relations
        if relation["scope"] == "file" or relation["target"] in declared
    ]


class CssParser(CodeParser):
    """CSS specific AST parser.

    Registered for no extension of its own: it is here for the `<style>`
    blocks HtmlParser hands over.
    """

    FAMILY = "css"
    ENTITY_QUERY = "(rule_set (selectors) @style)"

    def __init__(self) -> None:
        """Initialize CSS parser."""
        super().__init__(tree_sitter_css.language())

    def get_entities(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return one node per rule, named by the selector it is written under."""
        if self.entity_query is None:
            return []
        root = self.parse(content)
        return unique_pairs(
            (qualified(kind, " ".join(text.split())), kind)
            for kind, text in self.capture_texts(self.entity_query, root)
        )


class HtmlParser(CodeParser):
    """HTML specific AST parser: the page, its assets and the code inside it.

    A page is markup and a place code is written, so the `<script>` and
    `<style>` bodies are joined in source order and handed to the grammar that
    can read them - the same division of labour TemplateParser makes for
    `<% %>`, without needing to reconstruct a half-open program.
    """

    # Deliberately the ecmascript family: what an inline script declares is a
    # JavaScript symbol, and the markup around it changes nothing about that.
    FAMILY = "ecmascript"
    ENTITY_QUERY = """
        (element) @element
        (script_element) @element
        (style_element) @element
    """

    def __init__(self) -> None:
        """Initialize HTML parser and the two grammars it delegates to."""
        super().__init__(tree_sitter_html.language())
        self.script_parser = JavaScriptParser()
        self.style_parser = CssParser()

    def elements(self, content: str) -> list[Node]:
        """Return every element of the page, tags with a body included."""
        if self.entity_query is None:
            return []
        captures = self.capture_nodes(self.entity_query, self.parse(content))
        return sorted(captures.get("element", []), key=lambda node: node.start_byte)

    @staticmethod
    def embedded(elements: list[Node], tag: str) -> str:
        """Join the bodies of every `<tag>` of the page into one source."""
        spans = [
            node_text(child).strip()
            for element in elements
            if tag_name(element) == tag
            for child in element.children
            if child.type == "raw_text"
        ]
        return "\n".join(span for span in spans if span)

    def get_entities(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the structure of the page plus what its inline code declares."""
        elements = self.elements(content)
        pairs: list[tuple[str, str]] = []
        for element in elements:
            name = tag_name(element)
            if name == "title":
                heading = element_text(element)
                if heading:
                    pairs.append((qualified("title", heading), "title"))
            anchor = tag_attributes(element).get("id", "").strip()
            if anchor:
                pairs.append((qualified("anchor", anchor), "anchor"))
            # A hyphen is what tells a custom element from a standard one, and
            # only the custom ones are a declaration rather than structure.
            if "-" in name:
                pairs.append((qualified("element", name), "element"))
        entities = unique_pairs(iter(pairs))
        script = self.embedded(elements, "script")
        if script:
            entities.extend(self.script_parser.get_entities(script, rel_path))
        style = self.embedded(elements, "style")
        if style:
            entities.extend(self.style_parser.get_entities(style, rel_path))
        return entities

    def get_relations(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the files the page pulls in, and the calls its own code makes."""
        elements = self.elements(content)
        pairs: list[tuple[str, str]] = []
        for element in elements:
            name = tag_name(element)
            for attribute, value in tag_attributes(element).items():
                relation_type = HTML_ASSET_ATTRIBUTES.get((name, attribute))
                if relation_type is not None:
                    pairs.append((asset_path(value), relation_type))
        relations = [
            {"target": entry["name"], "type": entry["type"], "scope": "file"}
            for entry in unique_pairs(iter(pairs))
        ]
        script = self.embedded(elements, "script")
        if script:
            declared = {
                entity["name"]
                for entity in self.script_parser.get_entities(script, rel_path)
            }
            relations.extend(
                local_relations(
                    self.script_parser.get_relations(script, rel_path), declared
                )
            )
        return relations


class GoParser(CodeParser):
    """Go specific AST parser."""

    FAMILY = "go"
    ENTITY_QUERY = """
        (function_declaration name: (identifier) @function)
        (method_declaration name: (field_identifier) @method)
        (type_spec name: (type_identifier) @type)
    """
    RELATION_QUERY = """
        (call_expression function: (identifier) @calls)
        (call_expression function: (selector_expression
            field: (field_identifier) @calls))
        (import_spec path: (interpreted_string_literal) @imports)
    """

    def __init__(self) -> None:
        """Initialize Go parser."""
        super().__init__(tree_sitter_go.language())

    def extract_code(
        self, content: str
    ) -> tuple[list[CodeSymbol], list[CodeReference]]:
        """Return the types, functions and methods, and what they refer to.

        A method's parent is its receiver type when this file declares it;
        otherwise the file holds it and a `receiver` reference names the type.
        An import carries its package alias as the receiver.
        """
        root = self.parse(content)
        symbols: list[CodeSymbol] = []
        references: dict[tuple[str, str, str, str], CodeReference] = {}

        def refer(scope: str, kind: str, name: str, receiver: str, node: Node) -> None:
            if name:
                references.setdefault(
                    (scope, kind, name, receiver),
                    CodeReference(scope, kind, name, receiver, node.start_point[0] + 1),
                )

        types: dict[str, str] = {}
        for spec in go_type_specs(root):
            types.setdefault(go_name(spec), symbol_key(go_name(spec), go_line(spec)))

        for node in root.named_children:
            if node.type == "import_declaration":
                for spec in go_import_specs(node):
                    path_node = spec.child_by_field_name("path")
                    path = strip_literal(node_text(path_node)) if path_node else ""
                    refer("", IMPORTS, path, go_import_alias(spec, path), spec)
            elif node.type == "type_declaration":
                for spec in go_type_specs(node):
                    name, line = go_name(spec), go_line(spec)
                    value = spec.child_by_field_name("type")
                    interface = value is not None and value.type == "interface_type"
                    key = symbol_key(name, line)
                    symbols.append(
                        CodeSymbol(
                            key, name, "interface" if interface else "class", line, ""
                        )
                    )
                    for base in go_embedded(value):
                        refer(key, INHERITS, *go_type_name(base), base)
            elif node.type == "function_declaration" and go_name(node):
                key = symbol_key(f"{go_name(node)}()", go_line(node))
                symbols.append(
                    CodeSymbol(key, f"{go_name(node)}()", "function", go_line(node), "")
                )
                go_calls(node.child_by_field_name("body"), key, "", refer)
            elif node.type == "method_declaration" and go_name(node):
                variable, receiver = go_receiver(node)
                parent = types.get(receiver, "")
                label = f".{go_name(node)}()"
                key = symbol_key(label, go_line(node))
                symbols.append(CodeSymbol(key, label, "method", go_line(node), parent))
                if receiver and not parent:
                    refer(key, RECEIVER, receiver, "", node)
                go_calls(node.child_by_field_name("body"), key, variable, refer)
            else:
                go_calls(node, "", "", refer)
        return symbols, list(references.values())


GO_SPECS = frozenset({"type_spec", "type_alias"})
GO_VERSION_SUFFIX = re.compile(r"\.v\d+$")


def go_name(node: Node) -> str:
    """Return the declared name of a Go declaration or spec."""
    name = node.child_by_field_name("name")
    return node_text(name).strip() if name is not None else ""


def go_line(node: Node) -> int:
    """Return the 1-based line a node starts on."""
    return node.start_point[0] + 1


def go_type_specs(node: Node) -> list[Node]:
    """Return the named type specs at the top of a file or in one declaration."""
    if node.type == "type_declaration":
        return [
            one for one in node.named_children if one.type in GO_SPECS and go_name(one)
        ]
    return [
        spec
        for declaration in node.named_children
        if declaration.type == "type_declaration"
        for spec in go_type_specs(declaration)
    ]


def go_import_specs(node: Node) -> list[Node]:
    """Return the import specs of one import declaration, grouped or not."""
    specs: list[Node] = []
    for child in node.named_children:
        if child.type == "import_spec":
            specs.append(child)
        elif child.type == "import_spec_list":
            specs.extend(
                one for one in child.named_children if one.type == "import_spec"
            )
    return specs


def go_import_alias(spec: Node, path: str) -> str:
    """Return the name a file uses for an imported package, `.` or `_` included.

    Without an explicit alias it is the package name, guessed from the path:
    a major version suffix and a `go-` prefix or `-go` suffix are dropped.
    """
    alias = spec.child_by_field_name("name")
    if alias is not None:
        return node_text(alias).strip()
    segments = [one for one in path.split("/") if one]
    if len(segments) > 1 and re.fullmatch(r"v\d+", segments[-1]):
        segments.pop()
    name = GO_VERSION_SUFFIX.sub("", segments[-1]) if segments else ""
    return name.removeprefix("go-").removesuffix("-go").replace("-", "_")


def go_type_name(node: Node | None) -> tuple[str, str]:
    """Split a type into its name and the package it is qualified by."""
    if node is None:
        return "", ""
    if node.type == "type_identifier":
        return node_text(node).strip(), ""
    if node.type == "qualified_type":
        name = node.child_by_field_name("name")
        package = node.child_by_field_name("package")
        return (
            node_text(name).strip() if name else "",
            node_text(package).strip() if package else "",
        )
    if node.type == "generic_type":
        return go_type_name(node.child_by_field_name("type"))
    if node.type == "pointer_type" and node.named_children:
        return go_type_name(node.named_children[0])
    return "", ""


def go_callee(node: Node | None) -> tuple[str, str]:
    """Split what a call calls into its name and the operand it is called on."""
    if node is None:
        return "", ""
    if node.type == "identifier":
        return node_text(node).strip(), ""
    if node.type == "selector_expression":
        field = node.child_by_field_name("field")
        operand = node.child_by_field_name("operand")
        return (
            node_text(field).strip() if field else "",
            node_text(operand).strip() if operand else "",
        )
    if node.type == "index_expression":
        return go_callee(node.child_by_field_name("operand"))
    return "", ""


def go_receiver(node: Node) -> tuple[str, str]:
    """Return a method's receiver variable and the name of its type."""
    receiver = node.child_by_field_name("receiver")
    for parameter in receiver.named_children if receiver is not None else []:
        if parameter.type == "parameter_declaration":
            return go_name(parameter), go_type_name(
                parameter.child_by_field_name("type")
            )[0]
    return "", ""


def go_embedded(node: Node | None) -> list[Node]:
    """Return the types a struct or an interface embeds."""
    if node is None:
        return []
    embedded: list[Node] = []
    if node.type == "struct_type":
        for fields in node.named_children:
            for field in fields.named_children:
                kind = field.child_by_field_name("type")
                if field.type == "field_declaration" and not go_name(field) and kind:
                    embedded.append(kind)
    elif node.type == "interface_type":
        for element in node.named_children:
            if element.type == "type_elem" and len(element.named_children) == 1:
                embedded.append(element.named_children[0])
    return [one for one in embedded if go_type_name(one)[0]]


def go_calls(
    node: Node | None,
    scope: str,
    variable: str,
    refer: Callable[[str, str, str, str, Node], None],
) -> None:
    """Refer every call and composite literal under a node to one scope.

    A call on the method's receiver variable is a self call.
    """
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        if current.type == "call_expression":
            name, receiver = go_callee(current.child_by_field_name("function"))
        elif current.type in ("composite_literal", "type_conversion_expression"):
            name, receiver = go_type_name(current.child_by_field_name("type"))
        else:
            name, receiver = "", ""
        if name:
            if variable and receiver == variable:
                receiver = "self"
            refer(scope, CALLS, name, receiver, current)
        stack.extend(reversed(current.named_children))


class RustParser(CodeParser):
    """Rust specific AST parser."""

    FAMILY = "rust"
    ENTITY_QUERY = """
        (function_item name: (identifier) @function)
        (struct_item name: (type_identifier) @struct)
        (enum_item name: (type_identifier) @enum)
        (trait_item name: (type_identifier) @trait)
    """
    RELATION_QUERY = """
        (call_expression function: (identifier) @calls)
        (call_expression function: (field_expression field: (field_identifier) @calls))
        (use_declaration argument: (_) @imports)
    """

    def __init__(self) -> None:
        """Initialize Rust parser."""
        super().__init__(tree_sitter_rust.language())

    def extract_code(
        self, content: str
    ) -> tuple[list[CodeSymbol], list[CodeReference]]:
        """Return the types, functions and methods, and what they refer to.

        A method's parent is the type its impl names when this file declares
        it; otherwise a `receiver` reference names the type. Inline modules
        belong to the file. A `use` leaf or a `mod` declaration is an import
        carrying the local name as its receiver, `*` for a glob.
        """
        root = self.parse(content)
        symbols: list[CodeSymbol] = []
        references: dict[tuple[str, str, str, str], CodeReference] = {}

        def refer(scope: str, kind: str, name: str, receiver: str, node: Node) -> None:
            if name:
                references.setdefault(
                    (scope, kind, name, receiver),
                    CodeReference(scope, kind, name, receiver, node.start_point[0] + 1),
                )

        types: dict[str, str] = {}
        for node in rust_items(root):
            name = go_name(node) if node.type in RUST_TYPES else ""
            if name:
                types.setdefault(name, symbol_key(name, go_line(node)))

        def method(node: Node, parent: str) -> str:
            label = f".{go_name(node)}()"
            key = symbol_key(label, go_line(node))
            symbols.append(CodeSymbol(key, label, "method", go_line(node), parent))
            return key

        def walk(container: Node, depth: int) -> None:
            for node in container.named_children:
                if node.type == "mod_item":
                    body = node.child_by_field_name("body")
                    if body is not None:
                        walk(body, depth + 1)
                    elif go_name(node):
                        name = go_name(node)
                        refer("", IMPORTS, f"self::{name}", name, node)
                elif node.type in ("use_declaration", "extern_crate_declaration"):
                    rust_imports(node, depth, refer)
                elif node.type == "function_item" and go_name(node):
                    label, line = f"{go_name(node)}()", go_line(node)
                    key = symbol_key(label, line)
                    symbols.append(CodeSymbol(key, label, "function", line, ""))
                    rust_calls(node.child_by_field_name("body"), key, depth, refer)
                elif node.type in RUST_TYPES and go_name(node):
                    trait = node.type == "trait_item"
                    key = symbol_key(go_name(node), go_line(node))
                    symbols.append(
                        CodeSymbol(
                            key,
                            go_name(node),
                            "interface" if trait else "class",
                            go_line(node),
                            "",
                        )
                    )
                    if trait:
                        for base in rust_bounds(node.child_by_field_name("bounds")):
                            refer(key, INHERITS, *rust_split(base, depth), base)
                        body = node.child_by_field_name("body")
                        for one in body.named_children if body is not None else []:
                            if one.type in RUST_FUNCTIONS and go_name(one):
                                scope = method(one, key)
                                body_node = one.child_by_field_name("body")
                                rust_calls(body_node, scope, depth, refer)
                elif node.type == "impl_item":
                    rust_impl(node, depth, types, method, refer)
                else:
                    rust_calls(node, "", depth, refer)

        walk(root, 0)
        return symbols, list(references.values())


RUST_TYPES = frozenset(
    {"struct_item", "enum_item", "union_item", "type_item", "trait_item"}
)
RUST_FUNCTIONS = frozenset({"function_item", "function_signature_item"})
RUST_PATHS = frozenset(
    {
        "identifier",
        "type_identifier",
        "scoped_identifier",
        "scoped_type_identifier",
        "self",
        "super",
        "crate",
        "generic_type",
    }
)
RUST_GLOB = "*"
RUST_SELF = "self"
# The receiver of a method called on a value: its type is not known.
RUST_VALUE = "."


def rust_items(node: Node) -> list[Node]:
    """Return the items of a file, the ones inside inline modules included."""
    items: list[Node] = []
    for child in node.named_children:
        items.append(child)
        body = child.child_by_field_name("body") if child.type == "mod_item" else None
        if body is not None:
            items.extend(rust_items(body))
    return items


def rust_segments(node: Node | None) -> list[str]:
    """Return the segments of a path, generic arguments left out."""
    if node is None:
        return []
    if node.type in ("scoped_identifier", "scoped_type_identifier"):
        name = node.child_by_field_name("name")
        tail = [node_text(name).strip()] if name is not None else []
        return rust_segments(node.child_by_field_name("path")) + tail
    if node.type == "generic_type":
        return rust_segments(node.child_by_field_name("type"))
    if node.type in RUST_PATHS:
        return [node_text(node).strip()]
    return []


def rust_relative(segments: list[str], depth: int) -> list[str]:
    """Rewrite a path written inside inline modules to one from the file.

    The first `super`s of a path inside `depth` inline modules stay in the
    file; a path left with nothing before its name starts at `self`.
    """
    climbed = 0
    while climbed < min(depth, len(segments)) and segments[climbed] == "super":
        climbed += 1
    if not climbed:
        return segments
    rest = segments[climbed:]
    return rest if rest and rest[0] == "super" else ["self", *rest]


def rust_split(node: Node, depth: int) -> tuple[str, str]:
    """Split a type or a called path into its name and the path it is under.

    `Self` and a leading `self` module are the file itself: the first is
    the self receiver, the second leaves the name bare.
    """
    segments = rust_relative(rust_segments(node), depth)
    if not segments:
        return "", ""
    name, path = segments[-1], segments[:-1]
    if path[:1] == ["Self"]:
        return name, RUST_SELF if len(path) == 1 else ""
    if path[:1] == ["self"]:
        path = path[1:]
    return name, "::".join(path)


def rust_use_leaves(node: Node, prefix: list[str]) -> list[tuple[list[str], str]]:
    """Return each path a use tree brings in, with the name it is bound to."""
    if node.type == "use_list":
        return [
            leaf for one in node.named_children for leaf in rust_use_leaves(one, prefix)
        ]
    if node.type == "scoped_use_list":
        inner = node.child_by_field_name("list")
        path = [*prefix, *rust_segments(node.child_by_field_name("path"))]
        return rust_use_leaves(inner, path) if inner is not None else []
    if node.type == "use_as_clause":
        alias = node.child_by_field_name("alias")
        path = [*prefix, *rust_segments(node.child_by_field_name("path"))]
        if path[-1:] == ["self"] and len(path) > 1:
            path = path[:-1]
        return [(path, node_text(alias).strip())] if alias is not None else []
    if node.type == "use_wildcard":
        inner = node.named_children[0] if node.named_children else None
        return [([*prefix, *rust_segments(inner)], RUST_GLOB)]
    leaf = rust_segments(node)
    if not leaf:
        return []
    path = [*prefix, *leaf]
    if path[-1:] == ["self"] and len(path) > 1:
        path = path[:-1]
    return [(path, path[-1])] if path else []


def rust_imports(
    node: Node, depth: int, refer: Callable[[str, str, str, str, Node], None]
) -> None:
    """Refer every leaf of a use declaration, or an extern crate, to the file."""
    if node.type == "extern_crate_declaration":
        name = go_name(node)
        alias = node.child_by_field_name("alias")
        refer("", IMPORTS, name, node_text(alias).strip() if alias else name, node)
        return
    argument = node.child_by_field_name("argument")
    for path, alias in rust_use_leaves(argument, []) if argument else []:
        refer("", IMPORTS, "::".join(rust_relative(path, depth)), alias, node)


def rust_bounds(node: Node | None) -> list[Node]:
    """Return the traits a bound list names, lifetimes and `?Sized` left out."""
    if node is None:
        return []
    return [one for one in node.named_children if one.type in RUST_PATHS]


def rust_impl(
    node: Node,
    depth: int,
    types: dict[str, str],
    method: Callable[[Node, str], str],
    refer: Callable[[str, str, str, str, Node], None],
) -> None:
    """Write the methods of an impl block under the type it names.

    A trait implemented for a type of this file is a base of that type.
    """
    kind = node.child_by_field_name("type")
    name, path = rust_split(kind, depth) if kind is not None else ("", "")
    parent = types.get(name, "") if not path else ""
    trait = node.child_by_field_name("trait")
    if parent and trait is not None:
        refer(parent, INHERITS, *rust_split(trait, depth), trait)
    body = node.child_by_field_name("body")
    for one in body.named_children if body is not None else []:
        if one.type not in RUST_FUNCTIONS or not go_name(one):
            continue
        scope = method(one, parent)
        if name and not parent:
            refer(scope, RECEIVER, name, path, one)
        rust_calls(one.child_by_field_name("body"), scope, depth, refer)


def rust_callee(node: Node | None, depth: int) -> tuple[str, str]:
    """Split what a call calls into its name and its receiver.

    The receiver is a path for `a::f()`, `self` for a call on `self` or
    `Self`, and RUST_VALUE for a method called on any other value.
    """
    if node is None:
        return "", ""
    if node.type == "identifier":
        return node_text(node).strip(), ""
    if node.type == "scoped_identifier":
        return rust_split(node, depth)
    if node.type == "generic_function":
        return rust_callee(node.child_by_field_name("function"), depth)
    if node.type == "field_expression":
        field = node.child_by_field_name("field")
        value = node.child_by_field_name("value")
        own = value is not None and value.type == "self"
        name = node_text(field).strip() if field is not None else ""
        if not name.isidentifier():
            return "", ""
        return name, RUST_SELF if own else RUST_VALUE
    return "", ""


def rust_calls(
    node: Node | None,
    scope: str,
    depth: int,
    refer: Callable[[str, str, str, str, Node], None],
) -> None:
    """Refer every call and struct literal under a node to one scope.

    Macro arguments are token trees, not expressions, so no call is read
    inside them. A `use` inside a body is an import of the file.
    """
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        if current.type == "macro_invocation":
            continue
        if current.type in ("use_declaration", "extern_crate_declaration"):
            rust_imports(current, depth, refer)
            continue
        if current.type == "call_expression":
            name, receiver = rust_callee(current.child_by_field_name("function"), depth)
        elif current.type == "struct_expression":
            kind = current.child_by_field_name("name")
            name, receiver = rust_split(kind, depth) if kind is not None else ("", "")
            if name == "Self":
                name = ""
        else:
            name, receiver = "", ""
        if name:
            refer(scope, CALLS, name, receiver, current)
        stack.extend(reversed(current.named_children))


class BashParser(CodeParser):
    """Bash specific AST parser."""

    FAMILY = "bash"
    ENTITY_QUERY = "(function_definition name: (word) @function)"

    def __init__(self) -> None:
        """Initialize Bash parser."""
        super().__init__(tree_sitter_bash.language())


class DockerfileParser(CodeParser):
    """Dockerfile specific AST parser."""

    FAMILY = "dockerfile"
    ENTITY_QUERY = """
        (from_instruction (image_spec (image_name) @image))
        (from_instruction (image_alias) @stage)
    """

    def __init__(self) -> None:
        """Initialize Dockerfile parser."""
        super().__init__(tree_sitter_dockerfile.language())


class HCLParser(CodeParser):
    """HCL and Terraform specific AST parser."""

    FAMILY = "hcl"
    ENTITY_QUERY = "(block) @block"

    def __init__(self) -> None:
        """Initialize HCL parser."""
        super().__init__(tree_sitter_hcl.language())

    def get_entities(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Name a block by its type and labels, e.g. resource.aws_s3_bucket.main."""
        if self.entity_query is None:
            return []
        captures = self.capture_nodes(self.entity_query, self.parse(content))
        pairs: list[tuple[str, str]] = []
        for block in captures.get("block", []):
            kind = ""
            labels: list[str] = []
            for child in block.named_children:
                if child.type == "identifier" and not kind:
                    kind = node_text(child)
                elif child.type == "string_lit":
                    labels.extend(
                        node_text(part)
                        for part in child.named_children
                        if part.type == "template_literal"
                    )
            if kind:
                pairs.append((".".join([kind, *labels]), "block"))
        return unique_pairs(iter(pairs))

    def get_relations(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return module sources, the files read and the configs included."""
        return terraform_relations(self.parse(content), rel_path)


class MakeParser(CodeParser):
    """Makefile specific AST parser."""

    FAMILY = "make"
    ENTITY_QUERY = "(rule (targets (word) @target))"
    RELATION_QUERY = """
        (rule (targets (word) @target) (prerequisites (word) @prerequisite))
    """
    # Directives that read as targets. `.PHONY: help init` names three other
    # targets, and taking it for a rule leaves a false node in every Makefile.
    # Matched by name rather than by the leading dot: `.venv:` and `.env:` are
    # ordinary rules and keep their nodes.
    SPECIAL_MAKE_TARGETS = frozenset(
        {
            ".PHONY",
            ".SUFFIXES",
            ".DEFAULT",
            ".PRECIOUS",
            ".INTERMEDIATE",
            ".SECONDARY",
            ".SECONDEXPANSION",
            ".DELETE_ON_ERROR",
            ".IGNORE",
            ".SILENT",
            ".EXPORT_ALL_VARIABLES",
            ".NOTPARALLEL",
            ".ONESHELL",
            ".POSIX",
        }
    )

    def __init__(self) -> None:
        """Initialize Makefile parser."""
        super().__init__(tree_sitter_make.language())

    @staticmethod
    def blank_recipes(content: str) -> str:
        """Replace every recipe line with an empty one.

        A recipe is shell, and the grammar reads it as make. One unbalanced
        quote in it - `psql -tAc "select ..."` continued over four lines - and
        the parse turns into a single string node that swallows the rest of
        the file: the root Makefile here yielded 11 of its 23 targets, the
        ones declared above the first such recipe. Nothing in a recipe is
        wanted anyway, since only targets and prerequisites become nodes.

        The lines are blanked rather than dropped so the line numbering of
        what is left still matches the file on disk. A physical line starting
        with a tab is only a recipe when it starts a logical line: the
        continuation of a `.PHONY` list broken over several lines is indented
        the same way and has to survive, or the trailing backslash left
        hanging swallows the rule below it.
        """
        blanked: list[str] = []
        continued = False
        in_recipe = False
        for line in content.split("\n"):
            if not continued:
                in_recipe = line.startswith("\t")
            continued = line.endswith("\\")
            blanked.append("" if in_recipe else line)
        return "\n".join(blanked)

    def parse(self, content: str) -> Node:
        """Parse the file with its recipes blanked out."""
        return super().parse(self.blank_recipes(content))

    def get_entities(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Filter out GNU make special targets."""
        entities = super().get_entities(content, rel_path)
        return [e for e in entities if e["name"] not in self.SPECIAL_MAKE_TARGETS]

    def get_relations(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return one depends_on relation per prerequisite of a rule.

        The base implementation flattens every capture of the file into one
        bag, which loses the rule a prerequisite belonged to. Matches keep the
        two captures paired, and that pairing is the whole content of the
        edge: the source is the target being defined, not the file.

        A `$(OBJS)` prerequisite is a `variable_reference` in the grammar
        rather than a `word`, so the pattern already leaves it out.
        """
        if self.relation_query is None:
            return []
        seen: set[tuple[str, str]] = set()
        relations: list[dict[str, str]] = []
        for _, captures in QueryCursor(self.relation_query).matches(
            self.parse(content)
        ):
            for target_node in captures.get("target", ()):
                target = node_text(target_node)
                if target in self.SPECIAL_MAKE_TARGETS:
                    continue
                for prerequisite_node in captures.get("prerequisite", ()):
                    prerequisite = node_text(prerequisite_node)
                    if "$(" in prerequisite or "${" in prerequisite:
                        continue
                    if (target, prerequisite) in seen:
                        continue
                    seen.add((target, prerequisite))
                    relations.append(
                        {
                            "source": target,
                            "target": prerequisite,
                            "type": "depends_on",
                            "scope": "symbol",
                        }
                    )
        return relations


class MarkdownParser(CodeParser):
    """Markdown specific AST parser."""

    FAMILY = "markdown"
    ENTITY_QUERY = """
        (atx_heading (atx_h1_marker) (inline) @heading)
        (atx_heading (atx_h2_marker) (inline) @heading)
    """

    def __init__(self) -> None:
        """Initialize Markdown parser."""
        super().__init__(tree_sitter_markdown.language())


class TOMLParser(CodeParser):
    """TOML specific AST parser."""

    FAMILY = "toml"
    ENTITY_QUERY = """
        (table (bare_key) @table)
        (table (dotted_key) @table)
    """

    def __init__(self) -> None:
        """Initialize TOML parser."""
        super().__init__(tree_sitter_toml.language())


class YAMLParser(CodeParser):
    """YAML specific AST parser."""

    FAMILY = "yaml"
    # Top level keys only. Capturing every nested key turns a compose file into
    # hundreds of nodes named `image` or `ports`.
    ENTITY_QUERY = """
        (document (block_node (block_mapping
            (block_mapping_pair key: (flow_node) @key))))
    """

    def __init__(self) -> None:
        """Initialize YAML parser."""
        super().__init__(tree_sitter_yaml.language())


# JSON keeps its meaning in the values rather than in the syntax, so the
# helpers below take a captured pair apart instead of matching ever deeper
# query patterns.
def pair_key(node: Node) -> str:
    """Return the key of a `pair` node, unquoted."""
    key_node = node.child_by_field_name("key")
    return strip_literal(node_text(key_node)) if key_node is not None else ""


def pair_value(node: Node) -> Node | None:
    """Return the value of a `pair` node."""
    return node.child_by_field_name("value")


def object_pairs(node: Node | None) -> list[Node]:
    """Return the `pair` children of an object node."""
    if node is None or node.type != "object":
        return []
    return [child for child in node.named_children if child.type == "pair"]


def object_keys(node: Node | None) -> list[str]:
    """Return the keys an object node declares."""
    return [pair_key(pair) for pair in object_pairs(node)]


def string_value(node: Node | None) -> str:
    """Return the text of a string node, unquoted, and "" for anything else."""
    if node is None or node.type != "string":
        return ""
    return strip_literal(node_text(node))


def local_target(node: Node | None) -> str:
    """Return a string value when it names a path inside the project.

    Only a relative target can resolve to a file of the tree. A bare specifier
    such as `@tsconfig/node20/tsconfig.json` names a package, and keeping it
    would add a placeholder node pointing at nothing.
    """
    target = string_value(node)
    return target if target.startswith((".", "/")) else ""


def extends_targets(value: Node | None) -> list[str]:
    """Return what an `extends` key points at, in either form it takes."""
    if value is None:
        return []
    if value.type == "array":
        candidates = [local_target(child) for child in value.named_children]
    else:
        candidates = [local_target(value)]
    return [target for target in candidates if target]


def reference_targets(value: Node | None) -> list[str]:
    """Return the paths of a tsconfig `references` array."""
    if value is None or value.type != "array":
        return []
    targets: list[str] = []
    for entry in value.named_children:
        for pair in object_pairs(entry):
            if pair_key(pair) == "path":
                target = local_target(pair_value(pair))
                if target:
                    targets.append(target)
    return targets


# The dependency sections worth reading, with the prefix their targets carry,
# keyed by file name so the keys of one ecosystem are never read out of
# another file's JSON.
#
# The prefix is not decoration. Without it `resolve_symbol` would bind a
# dependency called `typescript` to a top level key node of that name in some
# other JSON file of the same family. Prefixed, it matches no declared symbol,
# so one external node is written per package - the same shape the Ansible
# `role:` placeholders have.
DEPENDENCY_SECTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "package.json": (
        "npm:",
        (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ),
    ),
    "composer.json": ("composer:", ("require", "require-dev")),
}
# The sections descended into for entities, and the type their keys become.
# The list is closed on purpose: descending into every second level turns a
# data file into hundreds of nodes named after its keys.
SECTION_ENTITIES = {
    "mcpServers": "mcp_server",
    "scripts": "script",
    "servers": "mcp_server",
}


class JsonParser(CodeParser):
    """JSON specific AST parser.

    JSON declares nothing, so its syntax alone yields no entity worth a node.
    Top level keys become nodes for any file, and the manifests that do carry
    structure - `package.json`, `tsconfig.json`, `composer.json`, `.mcp.json` -
    are read one level deeper, by name.
    """

    FAMILY = "json"
    # Each top level pair is captured whole and taken apart in Python. Deep
    # patterns over nested pairs are brittle, and the query already does the
    # one thing worth doing declaratively: anchoring to the top level, so a
    # document whose root is an array yields nothing.
    ENTITY_QUERY = "(document (object (pair) @entry))"

    def __init__(self) -> None:
        """Initialize JSON parser."""
        super().__init__(tree_sitter_json.language())

    def top_level_pairs(self, content: str) -> list[Node]:
        """Return the pairs of the top level object of a document."""
        if self.entity_query is None:
            return []
        captures = self.capture_nodes(self.entity_query, self.parse(content))
        return captures.get("entry", [])

    def get_entities(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the top level keys, and the keys of the known sections."""
        pairs: list[tuple[str, str]] = []
        for pair in self.top_level_pairs(content):
            key = pair_key(pair)
            pairs.append((key, "key"))
            entity_type = SECTION_ENTITIES.get(key)
            if entity_type is not None:
                pairs.extend(
                    (name, entity_type) for name in object_keys(pair_value(pair))
                )
        return unique_pairs(iter(pairs))

    def get_relations(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return what a manifest depends on and what it extends."""
        prefix, sections = DEPENDENCY_SECTIONS.get(
            posixpath.basename(rel_path), ("", ())
        )
        pairs: list[tuple[str, str]] = []
        for pair in self.top_level_pairs(content):
            key = pair_key(pair)
            value = pair_value(pair)
            if key in sections:
                pairs.extend(
                    (f"{prefix}{name}", "depends_on") for name in object_keys(value)
                )
            elif key == "extends":
                pairs.extend((target, "extends") for target in extends_targets(value))
            elif key == "references":
                pairs.extend((target, "extends") for target in reference_targets(value))
        return [
            {
                "target": entry["name"],
                "type": entry["type"],
                # A dependency is a name; an `extends` target is a path, which
                # resolve_file_target tries against this file's directory and
                # then the project root.
                "scope": "symbol" if entry["type"] == "depends_on" else "file",
            }
            for entry in unique_pairs(iter(pairs))
        ]


class PuppetParser(CodeParser):
    """Puppet manifest AST parser.

    Three things the query pair cannot express on its own, so all of them are
    handled in the overrides below: a resource is named after two of its fields
    rather than a single captured node, a template reference is the only Puppet
    relation whose target is a path instead of a symbol, and `class_identifier`
    is one node type serving three unrelated jobs - a declaration's name, a
    resource's type, and a qualified variable read - which only the surrounding
    nodes tell apart.
    """

    FAMILY = "puppet"
    # A single-segment Puppet name is a plain `identifier` node; only a
    # qualified name is a `class_identifier`. Both must be listed or
    # `class package_repo` is lost as an entity.
    ENTITY_QUERY = """
        (class_definition [(class_identifier) (identifier)] @class)
        (defined_resource_type [(class_identifier) (identifier)] @define)
        (node_definition (node_name) @node)
        (function_declaration [(class_identifier) (identifier)] @function)
    """
    # `include stdlib` yields a bare identifier while `include a::b` yields a
    # class_identifier, so both alternatives have to be listed or every
    # reference to an unqualified class is lost.
    RELATION_QUERY = """
        (class_inherits [(class_identifier) (identifier)] @inherits)
        (include_statement [(class_identifier) (identifier)] @includes)
        (require_statement [(class_identifier) (identifier)] @requires)
    """
    # Resources, ordering metaparameters, the -> and ~> chains, and the
    # template calls all need their children inspected, so they are captured
    # whole and taken apart in Python.
    DETAIL_QUERY = """
        (resource_declaration) @resource
        (attribute) @attribute
        (relation) @relation
        (function_call) @call
        (class_identifier) @scoped
    """
    # `require =>` and `notify =>` are the ordering metaparameters worth an
    # edge; the rest of an attribute list is configuration data.
    ATTRIBUTE_RELATIONS = {"require": "requires", "notify": "notifies"}
    # `$a::b` and `"${a::b}"` put the qualified name under one of these, which
    # is what tells a variable read apart from the same node type used for a
    # declaration or a resource type. The top scope form `$::a::b` is parented
    # by whatever consumes it and is recognised by its own sigil instead.
    SCOPED_READ_PARENTS = frozenset({"variable", "interpolation"})
    # The functions that render a template, in either template language.
    TEMPLATE_FUNCTIONS = frozenset({"epp", "template"})

    def __init__(self) -> None:
        """Initialize Puppet parser."""
        super().__init__(tree_sitter_puppet.language())
        self.detail_query = self._compile(self.DETAIL_QUERY)

    def resource_name(self, node: Node) -> str:
        """Name a resource declaration after its type and title.

        `package { 'nginx': }` becomes `package.nginx`, which is what a
        `Package['nginx']` reference elsewhere in the tree resolves to.
        """
        type_node = node.child_by_field_name("type")
        title_node = node.child_by_field_name("title")
        if type_node is None or title_node is None:
            return ""
        return f"{node_text(type_node).lower()}.{strip_literal(node_text(title_node))}"

    def reference_name(self, node: Node) -> str:
        """Name what a `Package['nginx']` style reference points at.

        `Class['a::b']` names a class and resolves to the class entity, while
        every other reference names a resource and matches `resource_name`.
        """
        parts = [child for child in node.named_children if child.type != "comment"]
        if len(parts) < 2:
            return ""
        kind = node_text(parts[0]).lower()
        title = strip_literal(node_text(parts[1]))
        return title if kind == "class" else f"{kind}.{title}"

    def template_argument(self, node: Node) -> str:
        """Return the template a `template(...)` or `epp(...)` call renders."""
        parts = node.named_children
        if not parts or node_text(parts[0]).lower() not in self.TEMPLATE_FUNCTIONS:
            return ""
        for child in parts[1:]:
            if child.type == "string":
                return strip_literal(node_text(child))
        return ""

    def scoped_variable_class(self, node: Node) -> str:
        """Name the class a qualified variable reference reads a parameter of.

        `$::package_repo::user` and `$package_repo::base_dir` both read a
        parameter of class `package_repo`, and `$profile::web::port` reads one
        of `profile::web`: the last segment is the variable, everything before
        it is the class. A single segment therefore names no class at all,
        which is what `$::osfamily` and the other top scope facts are.

        The segments come from the child nodes rather than from splitting the
        text, because the text of the top scope form keeps its `$::` prefix and
        would leave that on the first segment.
        """
        if not (
            node_text(node).startswith("$")
            or (
                node.parent is not None and node.parent.type in self.SCOPED_READ_PARENTS
            )
        ):
            return ""
        segments = [
            node_text(child)
            for child in node.named_children
            if child.type == "identifier"
        ]
        if len(segments) < 2:
            return ""
        return "::".join(segments[:-1])

    def declared_resource_type(self, node: Node) -> str:
        """Name the defined type a resource declaration instantiates.

        `accounts::user { 'repo': }` is an instance of `define accounts::user`,
        so the whole qualified name is the target and nothing is stripped off
        it. A built-in type is a bare `identifier` and never reaches here,
        which leaves exactly the module defined types.

        Compared by equality rather than identity: tree-sitter hands out a
        fresh wrapper object for the same underlying node, so `is` would fail
        on a node that is in fact the type field.
        """
        parent = node.parent
        if parent is None or parent.type != "resource_declaration":
            return ""
        if parent.child_by_field_name("type") != node:
            return ""
        return node_text(node)

    def get_entities(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the declarations and the resources a manifest holds."""
        root = self.parse(content)
        pairs: list[tuple[str, str]] = []
        if self.entity_query is not None:
            for kind, text in self.capture_texts(self.entity_query, root):
                # A node definition is quoted; a class identifier is not.
                pairs.append((strip_literal(text.strip()), kind))
        if self.detail_query is not None:
            for node in self.capture_nodes(self.detail_query, root).get("resource", []):
                pairs.append((self.resource_name(node), "resource"))
        return unique_pairs(iter(pairs))

    def get_relations(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return what a manifest inherits, includes, orders after and renders."""
        root = self.parse(content)
        pairs: list[tuple[str, str]] = []
        if self.relation_query is not None:
            for kind, text in self.capture_texts(self.relation_query, root):
                # `include ::stdlib` and `include stdlib` name the same class.
                pairs.append((text.strip().lstrip(":"), kind))

        templates: list[str] = []
        captures = (
            self.capture_nodes(self.detail_query, root)
            if self.detail_query is not None
            else {}
        )

        for node in captures.get("attribute", []):
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if name_node is None or value_node is None:
                continue
            relation = self.ATTRIBUTE_RELATIONS.get(node_text(name_node).lower())
            if relation and value_node.type == "resource_reference":
                pairs.append((self.reference_name(value_node), relation))

        # `A -> B` orders B after A, which is the same statement `require`
        # makes from B, so both arrive as one relation type.
        for node in captures.get("relation", []):
            references = [
                child
                for child in node.named_children
                if child.type == "resource_reference"
            ]
            for left, right in zip(references, references[1:], strict=False):
                pairs.append((self.reference_name(right), "requires"))
                pairs.append((self.reference_name(left), "requires"))

        for node in captures.get("call", []):
            argument = self.template_argument(node)
            if argument:
                templates.append(argument)

        # One capture, two edges: every `::` joined name in the manifest arrives
        # here, including the declarations and whatever a line the grammar
        # cannot parse leaves behind, so each edge admits only the shapes it
        # recognises rather than excluding the ones it does not.
        for node in captures.get("scoped", []):
            owner = self.scoped_variable_class(node)
            if owner:
                pairs.append((owner, "reads_var"))
            declared = self.declared_resource_type(node)
            if declared:
                pairs.append((declared, "references"))

        relations = [
            {"target": entry["name"], "type": entry["type"], "scope": "symbol"}
            for entry in unique_pairs(iter(pairs))
        ]
        relations.extend(
            {"target": entry["name"], "type": entry["type"], "scope": "file"}
            for entry in unique_pairs((target, "uses_template") for target in templates)
        )
        # Images and packages name nothing in the tree: they become placeholders
        # other projects provide.
        relations.extend(
            {"target": target, "type": relation, "scope": "file"}
            for target, relation in puppet_uses(content)
        )
        return relations


class TemplateParser(CodeParser):
    """Base class for the `<% %>` template languages.

    The embedded-template grammar isolates the code spans but leaves what is
    inside them opaque, and re-parsing a span on its own fails: `<% items.each
    do |i| %>` is a half-open block. Joining every span back together in source
    order reconstructs a program the inner grammar can read in one parse.
    """

    # Run over the reconstructed buffer by the inner grammar of a subclass.
    INNER_QUERY: str = ""
    CODE_QUERY = "(code) @code"

    def __init__(self, inner_language: Any) -> None:  # noqa: ANN401
        """Compile the outer template queries and the inner grammar's own."""
        super().__init__(tree_sitter_embedded_template.language())
        self.code_query = self._compile(self.CODE_QUERY)
        self.inner_language = Language(inner_language)
        self.inner_parser = Parser(self.inner_language)
        self.inner_query = Query(self.inner_language, self.INNER_QUERY)

    def code_buffer(self, content: str) -> str:
        """Reassemble the code spans of a template into one program."""
        if self.code_query is None:
            return ""
        nodes = self.capture_nodes(self.code_query, self.parse(content)).get("code", [])
        return "\n".join(
            node_text(node).strip()
            for node in sorted(nodes, key=lambda n: n.start_byte)
        )

    def get_entities(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the variables a template reads."""
        buffer = self.code_buffer(content)
        if not buffer:
            return []
        # An error node is expected here and is not a failure: an EPP parameter
        # block is not manifest syntax. Recovery still yields the variables.
        root = self.inner_parser.parse(buffer.encode("utf-8")).root_node
        return unique_pairs(
            (node_text(node).strip(), "variable")
            for node in self.capture_nodes(self.inner_query, root).get("variable", [])
        )


class ErbParser(TemplateParser):
    """ERB template parser, reading the Ruby embedded in one."""

    # Deliberately not "ruby": `.rb` files go to the graphifyy extractor and
    # have no parser here, so no ruby family exists in this pass and claiming
    # one would only let a template variable bind to something unrelated.
    FAMILY = "erb"
    INNER_QUERY = "(instance_variable) @variable"

    def __init__(self) -> None:
        """Initialize ERB parser."""
        super().__init__(tree_sitter_ruby.language())


class EppParser(TemplateParser):
    """EPP template parser, reading the Puppet embedded in one."""

    FAMILY = "puppet"
    INNER_QUERY = "(variable (identifier) @variable)"

    def __init__(self) -> None:
        """Initialize EPP parser."""
        super().__init__(tree_sitter_puppet.language())


class PhtmlParser(CodeParser):
    """PHP template parser, reading the PHP islands of a `.phtml` file.

    `.php` itself belongs to PhpParser and the code extractor, which route on
    that one suffix and so never see a template. No reassembly is needed: the
    grammar this uses is the variant that reads text interleaved with
    `<?php ?>` rather than PHP alone.
    """

    FAMILY = "php"
    ENTITY_QUERY = """
        (function_definition name: (name) @function)
        (class_declaration name: (name) @class)
        (interface_declaration name: (name) @interface)
        (trait_declaration name: (name) @trait)
        (method_declaration name: (name) @method)
    """
    RELATION_QUERY = """
        (function_call_expression function: (name) @calls)
        (function_call_expression function: (qualified_name (name) @calls))
        (member_call_expression name: (name) @calls)
        (scoped_call_expression name: (name) @calls)
        (base_clause (name) @inherits)
        (class_interface_clause (name) @inherits)
        (class_interface_clause (qualified_name (name) @inherits))
    """
    # The one relation of a template that reaches another file, and the reason
    # `.phtml` is worth a parser at all.
    INCLUDE_QUERY = """
        (include_expression (string (string_content) @includes))
        (include_once_expression (string (string_content) @includes))
        (require_expression (string (string_content) @includes))
        (require_once_expression (string (string_content) @includes))
    """

    def __init__(self) -> None:
        """Initialize PHP template parser."""
        super().__init__(tree_sitter_php.language_php())
        self.include_query = self._compile(self.INCLUDE_QUERY)

    def get_relations(self, content: str, rel_path: str) -> list[dict[str, str]]:
        """Return the files the template includes, and the calls it answers itself."""
        declared = {entity["name"] for entity in self.get_entities(content, rel_path)}
        relations = local_relations(super().get_relations(content, rel_path), declared)
        if self.include_query is None:
            return relations
        root = self.parse(content)
        includes = unique_pairs(
            (text.strip(), kind)
            for kind, text in self.capture_texts(self.include_query, root)
        )
        relations.extend(
            {"target": entry["name"], "type": entry["type"], "scope": "file"}
            for entry in includes
        )
        return relations


class PhpParser(CodeParser):
    """PHP specific AST parser, for the `.php` files the code extractor reads."""

    FAMILY = "php"
    ENTITY_QUERY = PhtmlParser.ENTITY_QUERY
    RELATION_QUERY = PhtmlParser.RELATION_QUERY

    def __init__(self) -> None:
        """Initialize PHP parser."""
        super().__init__(tree_sitter_php.language_php())

    def extract_code(
        self, content: str
    ) -> tuple[list[CodeSymbol], list[CodeReference]]:
        """Return the types, functions and methods, and what they refer to.

        Class and function names are qualified here, from the namespace and
        the `use` clauses in force where they are written, and every type and
        function says the qualified name it declares. A function inside
        another, a closure and an anonymous class are no symbols: what they
        call belongs to the scope around them.
        """
        symbols: list[CodeSymbol] = []
        references: dict[tuple[str, str, str, str], CodeReference] = {}

        def refer(scope: str, kind: str, name: str, receiver: str, node: Node) -> None:
            if name:
                references.setdefault(
                    (scope, kind, name, receiver),
                    CodeReference(scope, kind, name, receiver, php_line(node)),
                )

        names = PhpNames()
        stack: list[tuple[Node, str, str, bool, str]] = [
            (self.parse(content), "", "", False, "")
        ]
        while stack:
            node, scope, owner, nested, base = stack.pop()
            children = [
                (child, scope, owner, nested, base) for child in node.named_children
            ]
            kind = node.type
            name = php_name(node)
            if kind == "namespace_definition":
                names.enter(php_text(node.child_by_field_name("name")))
            elif kind == "namespace_use_declaration":
                for qualified_name, alias in names.use(node):
                    refer("", IMPORTS, qualified_name, alias, node)
                continue
            elif kind in PHP_INCLUDES:
                refer("", IMPORTS, php_include_target(node), "", node)
            elif kind in PHP_TYPES and name and not nested:
                key = symbol_key(name, php_line(node))
                symbols.append(
                    CodeSymbol(key, name, PHP_TYPES[kind], php_line(node), "")
                )
                refer(key, DECLARES, names.declared(name), PHP_CLASS, node)
                parent = ""
                for clause in node.named_children:
                    if clause.type not in PHP_BASE_CLAUSES:
                        continue
                    for one in php_names(clause):
                        refer(key, INHERITS, names.qualify(php_text(one)), "", one)
                        if clause.type == "base_clause" and not parent:
                            parent = names.qualify(php_text(one))
                body = node.child_by_field_name("body")
                children = [
                    (child, key, key, False, parent)
                    for child in (body.named_children if body else [])
                ]
            elif kind == "use_declaration" and owner:
                for one in php_names(node):
                    refer(owner, INHERITS, names.qualify(php_text(one)), "", one)
                continue
            elif kind == "method_declaration" and owner and name and not nested:
                label = f".{name}()"
                key = symbol_key(label, php_line(node))
                symbols.append(CodeSymbol(key, label, "method", php_line(node), owner))
                children = [
                    (child, key, owner, True, base) for child in node.named_children
                ]
            elif kind == "function_definition" and name and not nested:
                label = f"{name}()"
                key = symbol_key(label, php_line(node))
                symbols.append(CodeSymbol(key, label, "function", php_line(node), ""))
                refer(key, DECLARES, names.declared(name), PHP_FUNCTION, node)
                children = [(child, key, "", True, "") for child in node.named_children]
            elif kind == "function_call_expression":
                function = node.child_by_field_name("function")
                if function is not None and function.type in PHP_NAMES:
                    refer(scope, CALLS, *names.function(php_text(function)), node)
            elif kind in PHP_MEMBER_CALLS:
                method = node.child_by_field_name("name")
                target = php_text(node.child_by_field_name("object"))
                if method is not None and method.type == "name":
                    receiver = "self" if target == "$this" else target
                    refer(scope, CALLS, php_text(method), receiver, node)
            elif kind == "scoped_call_expression":
                method = node.child_by_field_name("name")
                receiver = names.scope(node.child_by_field_name("scope"), base)
                if method is not None and method.type == "name" and receiver:
                    refer(scope, CALLS, php_text(method), receiver, node)
            elif kind == "object_creation_expression":
                created = next(
                    (one for one in node.named_children if one.type in PHP_NAMES), None
                )
                text = php_text(created)
                if created is not None and text.lower() not in PHP_RELATIVE:
                    refer(scope, CALLS, names.qualify(text), PHP_NEW, node)
            stack.extend(reversed(children))
        return symbols, list(references.values())


PHP_TYPES = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "trait_declaration": "class",
    "enum_declaration": "class",
}
PHP_BASE_CLAUSES = frozenset({"base_clause", "class_interface_clause"})
PHP_INCLUDES = frozenset(
    {
        "include_expression",
        "include_once_expression",
        "require_expression",
        "require_once_expression",
    }
)
PHP_MEMBER_CALLS = frozenset(
    {"member_call_expression", "nullsafe_member_call_expression"}
)
PHP_NAMES = frozenset({"name", "qualified_name"})
PHP_RELATIVE = frozenset({"self", "static", "parent"})
PHP_STRINGS = frozenset({"string", "encapsed_string"})
PHP_STRING_PARTS = frozenset({"string_content", "escape_sequence"})
# What a declaration says it is, and the receivers a call carries for a name
# resolved exactly or for a class it creates.
PHP_CLASS = "class"
PHP_FUNCTION = "function"
PHP_EXACT = "\\"
PHP_NEW = "new"


def php_text(node: Node | None) -> str:
    """Return a node's text without surrounding whitespace, empty for none."""
    return node_text(node).strip() if node is not None else ""


def php_name(node: Node) -> str:
    """Return the declared name of a PHP declaration."""
    return php_text(node.child_by_field_name("name"))


def php_line(node: Node) -> int:
    """Return the 1-based line a node starts on."""
    return node.start_point[0] + 1


def php_names(node: Node) -> list[Node]:
    """Return the class names listed directly under a clause."""
    return [one for one in node.named_children if one.type in PHP_NAMES]


def php_use_kind(node: Node) -> str:
    """Return `function` or `const` for such a `use`, empty for classes."""
    return next(
        (
            one.type
            for one in node.children
            if not one.is_named and one.type in ("function", "const")
        ),
        "",
    )


class PhpNames:
    """The namespace and the `use` aliases in force at one point of a file."""

    def __init__(self) -> None:
        """Start in the global namespace with no aliases."""
        self.enter("")

    def enter(self, namespace: str) -> None:
        """Open a namespace; the aliases of the one before do not carry over."""
        self.namespace = namespace.strip("\\")
        self.classes: dict[str, str] = {}
        self.functions: dict[str, str] = {}

    def declared(self, name: str) -> str:
        """Return the qualified name of something declared here."""
        return f"{self.namespace}\\{name}" if self.namespace else name

    def qualify(self, text: str) -> str:
        """Return the qualified name of a class written here."""
        if text.startswith("\\"):
            return text.lstrip("\\")
        head, separator, rest = text.partition("\\")
        alias = self.classes.get(head.lower())
        if alias:
            return f"{alias}\\{rest}" if separator else alias
        if separator and head.lower() == "namespace":
            return self.declared(rest)
        return self.declared(text)

    def function(self, text: str) -> tuple[str, str]:
        """Return a called function's qualified name and how it resolves.

        An unqualified name without an alias falls back to the global
        function of that name, so its receiver stays empty.
        """
        if "\\" in text:
            return self.qualify(text), PHP_EXACT
        alias = self.functions.get(text.lower())
        if alias:
            return alias, PHP_EXACT
        return self.declared(text), ""

    def scope(self, node: Node | None, base: str) -> str:
        """Return the receiver of a static call: `self`, a class or a variable."""
        text = php_text(node)
        if node is None or not text:
            return ""
        if text.lower() in ("self", "static"):
            return "self"
        if text.lower() == "parent":
            return f"\\{base}" if base else ""
        if node.type in PHP_NAMES:
            return f"\\{self.qualify(text)}"
        return text

    def use(self, node: Node) -> list[tuple[str, str]]:
        """Take the aliases of one `use`; return its classes with their alias."""
        kind = php_use_kind(node)
        prefix = ""
        clauses = list(node.named_children)
        for child in node.named_children:
            if child.type == "namespace_name":
                prefix = php_text(child).strip("\\")
            elif child.type == "namespace_use_group":
                clauses = list(child.named_children)
        imported: list[tuple[str, str]] = []
        for clause in clauses:
            if clause.type != "namespace_use_clause" or not clause.named_children:
                continue
            path = php_text(clause.named_children[0]).lstrip("\\")
            target = f"{prefix}\\{path}" if prefix else path
            alias = php_text(clause.child_by_field_name("alias"))
            alias = alias or target.rpartition("\\")[2]
            clause_kind = php_use_kind(clause) or kind
            if clause_kind == "function":
                self.functions[alias.lower()] = target
            elif not clause_kind:
                self.classes[alias.lower()] = target
                imported.append((target, alias))
        return imported


def php_include_target(node: Node) -> str:
    """Return the path an include names, as far as the source spells it.

    After `__DIR__` or `dirname(__FILE__)` it is relative to the file (`./`,
    `../`); after any other expression only the literal tail is kept.
    """
    argument = node.named_children[0] if node.named_children else None
    parts = php_path_parts(argument)
    start = 1 if parts and isinstance(parts[0], int) else 0
    unknown = [
        index for index in range(start, len(parts)) if not isinstance(parts[index], str)
    ]
    cut = unknown[-1] if unknown else -1
    literal = "".join(
        part for part in parts[max(cut + 1, start) :] if isinstance(part, str)
    )
    if cut >= 0:
        return literal.lstrip("/")
    if start and literal.strip("/"):
        levels = parts[0] if isinstance(parts[0], int) else 0
        return f"{'../' * levels or './'}{literal.lstrip('/')}"
    return "" if start else literal


def php_path_parts(node: Node | None) -> list[str | int | None]:
    """Split a path expression into literals, directory levels and unknowns.

    An int is a directory `levels` above the file's own; None is anything
    whose value the source does not give.
    """
    if node is None:
        return [None]
    if node.type == "parenthesized_expression" and node.named_children:
        return php_path_parts(node.named_children[0])
    if node.type == "binary_expression" and any(
        not one.is_named and one.type == "." for one in node.children
    ):
        return php_path_parts(node.child_by_field_name("left")) + php_path_parts(
            node.child_by_field_name("right")
        )
    if node.type in PHP_STRINGS:
        return [
            node_text(one) if one.type in PHP_STRING_PARTS else None
            for one in node.named_children
        ]
    level = php_directory_level(node)
    return [level] if level is not None and level >= 0 else [None]


def php_directory_level(node: Node | None) -> int | None:
    """Return how far above the file's directory an expression points.

    `__FILE__` is -1, the file itself; `__DIR__` is 0; each `dirname` climbs.
    """
    if node is None:
        return None
    text = php_text(node)
    if node.type == "name":
        return {"__DIR__": 0, "__FILE__": -1}.get(text.upper())
    if node.type == "parenthesized_expression" and node.named_children:
        return php_directory_level(node.named_children[0])
    if node.type != "function_call_expression":
        return None
    if php_text(node.child_by_field_name("function")).lower() != "dirname":
        return None
    arguments = node.child_by_field_name("arguments")
    values = [
        one.named_children[0]
        for one in (arguments.named_children if arguments else [])
        if one.type == "argument" and one.named_children
    ]
    inner = php_directory_level(values[0]) if values else None
    climb = php_text(values[1]) if len(values) > 1 else "1"
    if inner is None or not climb.isdigit():
        return None
    return inner + int(climb)


class JavaParser(CodeParser):
    """Java specific AST parser."""

    FAMILY = "jvm"

    def __init__(self) -> None:
        """Initialize Java parser."""
        super().__init__(tree_sitter_java.language())

    def extract_code(
        self, content: str
    ) -> tuple[list[CodeSymbol], list[CodeReference]]:
        """Return the types, methods and constructors, and what they refer to.

        The file declares its package and every type its qualified name. A
        call carries what it is made on: `self`, `super`, `new`, a type as
        written, or JAVA_VALUE; a variable stands for its declared type. The
        members of anonymous and local classes are no symbols.
        """
        root = self.parse(content)
        symbols: list[CodeSymbol] = []
        references: dict[tuple[str, str, str, str], CodeReference] = {}

        def refer(scope: str, kind: str, name: str, receiver: str, node: Node) -> None:
            if name:
                references.setdefault(
                    (scope, kind, name, receiver),
                    CodeReference(scope, kind, name, receiver, node.start_point[0] + 1),
                )

        def declare(
            node: Node, prefix: str, parent: str, outer: dict[str, str]
        ) -> None:
            name, line = go_name(node), go_line(node)
            key = symbol_key(name, line)
            kind = "interface" if node.type in JAVA_INTERFACES else "class"
            symbols.append(CodeSymbol(key, name, kind, line, parent))
            qualified = f"{prefix}.{name}" if prefix else name
            refer(key, DECLARES, qualified, JAVA_TYPE, node)
            for base in java_bases(node):
                refer(key, INHERITS, java_type(base), "", base)
            members = java_members(node)
            fields = {**outer, **java_fields(node, members)}
            for member in members:
                if member.type in JAVA_TYPES and go_name(member):
                    declare(member, qualified, key, fields)
                elif member.type in JAVA_METHODS and go_name(member):
                    label = f".{go_name(member)}()"
                    scope = symbol_key(label, go_line(member))
                    symbols.append(
                        CodeSymbol(scope, label, "method", go_line(member), key)
                    )
                    variables = {**fields, **java_variables(member)}
                    body = member.child_by_field_name("body")
                    java_calls(body, scope, variables, refer)
                else:
                    java_calls(member, key, fields, refer)

        package = ""
        for node in root.named_children:
            if node.type == "package_declaration" and node.named_children:
                package = java_dotted(node.named_children[-1])
                refer("", DECLARES, package, JAVA_PACKAGE, node)
            elif node.type == "import_declaration":
                java_import(node, refer)
            elif node.type in JAVA_TYPES and go_name(node):
                declare(node, package, "", {})
        return symbols, list(references.values())


JAVA_TYPES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }
)
JAVA_INTERFACES = frozenset({"interface_declaration", "annotation_type_declaration"})
JAVA_METHODS = frozenset(
    {
        "method_declaration",
        "constructor_declaration",
        "compact_constructor_declaration",
    }
)
JAVA_TYPE_NAMES = frozenset(
    {"type_identifier", "scoped_type_identifier", "generic_type"}
)
JAVA_BASE_CLAUSES = frozenset({"superclass", "super_interfaces", "extends_interfaces"})
JAVA_DECLARATORS = frozenset(
    {"field_declaration", "constant_declaration", "local_variable_declaration"}
)
JAVA_LOCALS = frozenset(
    {
        "formal_parameter",
        "local_variable_declaration",
        "enhanced_for_statement",
        "catch_formal_parameter",
        "resource",
    }
)
# What a declaration says it is, how an import binds its name, and what a call
# is made on besides a type as written.
JAVA_TYPE = "type"
JAVA_PACKAGE = "package"
JAVA_GLOB = "*"
JAVA_STATIC = "static"
JAVA_STATIC_GLOB = "static*"
JAVA_SELF = "self"
JAVA_SUPER = "super"
JAVA_NEW = "new"
JAVA_VALUE = "."


def java_dotted(node: Node | None) -> str:
    """Return a dotted name as written, whitespace left out."""
    return "".join(node_text(node).split()) if node is not None else ""


def java_type(node: Node | None) -> str:
    """Return a type as written, dotted, without its type arguments.

    Primitive, array and `var` types are empty: no method of the tree is
    called on them.
    """
    if node is None:
        return ""
    if node.type == "type_identifier":
        text = node_text(node).strip()
        return "" if text == "var" else text
    if node.type == "scoped_type_identifier":
        parts = [
            java_type(one) for one in node.named_children if one.type in JAVA_TYPE_NAMES
        ]
        return ".".join(parts) if parts and all(parts) else ""
    if node.type in ("generic_type", "annotated_type"):
        return java_type(
            next(
                (one for one in node.named_children if one.type in JAVA_TYPE_NAMES),
                None,
            )
        )
    return ""


def java_import(node: Node, refer: Callable[[str, str, str, str, Node], None]) -> None:
    """Refer one import to the file, bound to the name it brings in.

    A type is bound to its simple name, `pkg.*` to JAVA_GLOB, a static member
    to JAVA_STATIC and a static `*` to JAVA_STATIC_GLOB.
    """
    static = any(not one.is_named and one.type == "static" for one in node.children)
    glob = any(one.type == "asterisk" for one in node.named_children)
    path = next(
        (
            one
            for one in node.named_children
            if one.type in ("scoped_identifier", "identifier")
        ),
        None,
    )
    name = java_dotted(path)
    if static:
        receiver = JAVA_STATIC_GLOB if glob else JAVA_STATIC
    else:
        receiver = JAVA_GLOB if glob else name.rpartition(".")[2]
    refer("", IMPORTS, name, receiver, node)


def java_bases(node: Node) -> list[Node]:
    """Return the types a type declaration extends or implements."""
    bases: list[Node] = []
    for clause in node.named_children:
        if clause.type not in JAVA_BASE_CLAUSES:
            continue
        for one in clause.named_children:
            bases.extend(one.named_children if one.type == "type_list" else [one])
    return [one for one in bases if java_type(one)]


def java_members(node: Node) -> list[Node]:
    """Return what a type's body declares, an enum's constants included."""
    body = node.child_by_field_name("body")
    members: list[Node] = []
    for child in body.named_children if body is not None else []:
        if child.type == "enum_body_declarations":
            members.extend(child.named_children)
        else:
            members.append(child)
    return members


def java_typed(kind: Node | None, value: Node | None) -> str:
    """Return a declared type, or the class a `var` is created from."""
    if kind is None or node_text(kind).strip() != "var":
        return java_type(kind)
    if value is not None and value.type == "object_creation_expression":
        return java_type(value.child_by_field_name("type"))
    return ""


def java_declared(node: Node) -> list[tuple[str, str]]:
    """Return each name a declaration introduces with the type it is given."""
    kind = node.child_by_field_name("type")
    if node.type == "catch_formal_parameter":
        caught = [one for one in node.named_children if one.type == "catch_type"]
        inner = caught[0].named_children if caught else []
        kind = inner[0] if len(inner) == 1 else None
    if node.type in JAVA_DECLARATORS:
        named = [
            (one.child_by_field_name("name"), one.child_by_field_name("value"))
            for one in node.children_by_field_name("declarator")
        ]
    else:
        named = [(node.child_by_field_name("name"), node.child_by_field_name("value"))]
    return [
        (node_text(name).strip(), java_typed(kind, value))
        for name, value in named
        if name is not None
    ]


def java_fields(node: Node, members: list[Node]) -> dict[str, str]:
    """Return the type of each field of a type, record components included.

    An enum constant is a field of the enum's own type.
    """
    fields: dict[str, str] = {}
    for holder in node.named_children:
        if holder.type == "formal_parameters":
            for one in holder.named_children:
                if one.type == "formal_parameter":
                    fields.update(java_declared(one))
    for one in members:
        if one.type in ("field_declaration", "constant_declaration"):
            fields.update(java_declared(one))
        elif one.type == "enum_constant" and go_name(one):
            fields[go_name(one)] = go_name(node)
    return fields


def java_variables(node: Node) -> dict[str, str]:
    """Return the type of each parameter and local a method declares.

    A name declared twice with different types is left untyped.
    """
    found: dict[str, str] = {}
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in JAVA_LOCALS:
            for name, kind in java_declared(current):
                found[name] = kind if found.get(name, kind) == kind else ""
        stack.extend(current.named_children)
    return found


def java_name_path(node: Node | None) -> str:
    """Return a name made of identifiers and dots, empty for anything else."""
    if node is None:
        return ""
    if node.type == "identifier":
        return node_text(node).strip()
    if node.type == "field_access":
        field = node.child_by_field_name("field")
        head = java_name_path(node.child_by_field_name("object"))
        if head and field is not None and field.type == "identifier":
            return f"{head}.{node_text(field).strip()}"
    return ""


def java_receiver(node: Node | None, variables: dict[str, str]) -> str:
    """Return what a call is made on, empty for a bare call.

    A variable stands for its declared type; a dotted name no variable
    starts is a type when its last part is capitalised.
    """
    if node is None:
        return ""
    if node.type == "this":
        return JAVA_SELF
    if node.type == "super":
        return JAVA_SUPER
    if node.type == "object_creation_expression":
        return java_type(node.child_by_field_name("type")) or JAVA_VALUE
    if node.type in JAVA_TYPE_NAMES:
        return java_type(node) or JAVA_VALUE
    if node.type == "field_access":
        target = node.child_by_field_name("object")
        field = node.child_by_field_name("field")
        if field is not None and field.type == "this":
            return JAVA_SELF
        if target is not None and target.type == "this" and field is not None:
            return variables.get(node_text(field).strip(), "") or JAVA_VALUE
    dotted = java_name_path(node)
    head = dotted.partition(".")[0]
    if head in variables:
        return (variables[head] if head == dotted else "") or JAVA_VALUE
    if dotted and dotted.rpartition(".")[2][:1].isupper():
        return dotted
    return JAVA_VALUE


def java_reference(node: Node, variables: dict[str, str]) -> tuple[str, str]:
    """Split a method reference into the name it calls and its receiver."""
    target = node.named_children[0] if node.named_children else None
    last = node.children[-1] if node.children else None
    if last is None or target is None or last == target:
        return "", ""
    if last.type == "new":
        return java_type(target) or java_name_path(target), JAVA_NEW
    if last.type != "identifier":
        return "", ""
    return node_text(last).strip(), java_receiver(target, variables)


def java_calls(
    node: Node | None,
    scope: str,
    variables: dict[str, str],
    refer: Callable[[str, str, str, str, Node], None],
) -> None:
    """Refer every call, `new` and method reference under a node to one scope."""
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        name, receiver = "", ""
        if current.type == "method_invocation":
            method = current.child_by_field_name("name")
            name = node_text(method).strip() if method is not None else ""
            receiver = java_receiver(current.child_by_field_name("object"), variables)
        elif current.type == "object_creation_expression":
            name = java_type(current.child_by_field_name("type"))
            receiver = JAVA_NEW
        elif current.type == "method_reference":
            name, receiver = java_reference(current, variables)
        if name:
            refer(scope, CALLS, name, receiver, current)
        stack.extend(reversed(current.named_children))


class KotlinParser(CodeParser):
    """Kotlin specific AST parser."""

    FAMILY = "jvm"

    def __init__(self) -> None:
        """Initialize Kotlin parser."""
        super().__init__(tree_sitter_kotlin.language())

    def extract_code(
        self, content: str
    ) -> tuple[list[CodeSymbol], list[CodeReference]]:
        """Return the classes, objects and functions, and what they refer to.

        The file declares its package, every type and top-level function its
        qualified name, and every extension function its name. Members of a
        companion object are methods of the class around it; members of
        object literals, local classes and local functions are no symbols.
        """
        root = self.parse(content)
        symbols: list[CodeSymbol] = []
        references: dict[tuple[str, str, str, str], CodeReference] = {}

        def refer(scope: str, kind: str, name: str, receiver: str, node: Node) -> None:
            if name:
                references.setdefault(
                    (scope, kind, name, receiver),
                    CodeReference(scope, kind, name, receiver, node.start_point[0] + 1),
                )

        def function(
            node: Node, label: str, kind: str, parent: str, fields: dict[str, str]
        ) -> str:
            key = symbol_key(label, go_line(node))
            symbols.append(CodeSymbol(key, label, kind, go_line(node), parent))
            variables = {**fields, **kotlin_variables(node)}
            kotlin_calls(kotlin_body(node), key, variables, refer)
            return key

        def members(
            body: Node | None, qualified: str, key: str, fields: dict[str, str]
        ) -> None:
            for member in body.named_children if body is not None else []:
                if member.type in KOTLIN_TYPES and go_name(member):
                    declare(member, qualified, key, fields)
                elif member.type == "companion_object":
                    inner = kotlin_class_body(member)
                    shared = {**fields, **kotlin_fields(member, inner)}
                    members(inner, qualified, key, shared)
                elif member.type == "function_declaration" and go_name(member):
                    function(member, f".{go_name(member)}()", "method", key, fields)
                elif member.type == "secondary_constructor":
                    variables = {**fields, **kotlin_variables(member)}
                    kotlin_calls(member, key, variables, refer)
                else:
                    kotlin_calls(member, key, fields, refer)

        def declare(
            node: Node, prefix: str, parent: str, outer: dict[str, str]
        ) -> None:
            name, line = go_name(node), go_line(node)
            key = symbol_key(name, line)
            interface = any(one.type == "interface" for one in node.children)
            kind = "interface" if interface else "class"
            symbols.append(CodeSymbol(key, name, kind, line, parent))
            qualified = f"{prefix}.{name}" if prefix else name
            refer(key, DECLARES, qualified, JAVA_TYPE, node)
            for base in kotlin_bases(node):
                refer(key, INHERITS, kotlin_type(base), "", base)
            body = kotlin_class_body(node)
            members(body, qualified, key, {**outer, **kotlin_fields(node, body)})

        package = ""
        for node in root.named_children:
            if node.type == "package_header" and node.named_children:
                package = java_dotted(node.named_children[0])
                refer("", DECLARES, package, JAVA_PACKAGE, node)
            elif node.type == "import":
                kotlin_import(node, refer)
            elif node.type in KOTLIN_TYPES and go_name(node):
                declare(node, package, "", {})
            elif node.type == "function_declaration" and go_name(node):
                name = go_name(node)
                key = function(node, f"{name}()", "function", "", {})
                full = f"{package}.{name}" if package else name
                refer(key, DECLARES, full, KOTLIN_FUNCTION, node)
                if kotlin_extended(node):
                    refer(key, DECLARES, name, KOTLIN_EXTENSION, node)
            else:
                kotlin_calls(node, "", {}, refer)
        return symbols, list(references.values())


KOTLIN_TYPES = frozenset({"class_declaration", "object_declaration"})
KOTLIN_BODIES = frozenset({"class_body", "enum_class_body"})
KOTLIN_TYPE_NAMES = frozenset({"user_type", "nullable_type"})
KOTLIN_UNWRAP = frozenset({"parenthesized_expression", "unary_expression"})
KOTLIN_NAMED = frozenset({"parameter", "class_parameter", "catch_block"})
KOTLIN_LOCALS = frozenset(
    {"parameter", "property_declaration", "for_statement", "catch_block"}
)
# What a top-level function and an extension function are declared as.
KOTLIN_FUNCTION = "function"
KOTLIN_EXTENSION = "extension"
KOTLIN_COMPANION = "Companion"


def kotlin_class_body(node: Node) -> Node | None:
    """Return the body of a class, object or companion, enum bodies included."""
    return next((one for one in node.named_children if one.type in KOTLIN_BODIES), None)


def kotlin_body(node: Node) -> Node | None:
    """Return the body of a function declaration."""
    return next(
        (one for one in node.named_children if one.type == "function_body"), None
    )


def kotlin_first(node: Node, *kinds: str) -> Node | None:
    """Return the first named child of one of these types."""
    return next((one for one in node.named_children if one.type in kinds), None)


def kotlin_type(node: Node | None) -> str:
    """Return a type as written, dotted, without type arguments or `?`."""
    if node is None:
        return ""
    if node.type in ("nullable_type", "constructor_invocation"):
        return kotlin_type(kotlin_first(node, *KOTLIN_TYPE_NAMES))
    if node.type != "user_type":
        return ""
    return ".".join(
        node_text(one).strip()
        for one in node.named_children
        if one.type == "identifier"
    )


def kotlin_typed(node: Node) -> str:
    """Return the type a declaration names, its first type child."""
    return kotlin_type(kotlin_first(node, *KOTLIN_TYPE_NAMES))


def kotlin_import(
    node: Node, refer: Callable[[str, str, str, str, Node], None]
) -> None:
    """Refer one import to the file, bound to its alias, last name or JAVA_GLOB."""
    name = java_dotted(kotlin_first(node, "qualified_identifier"))
    alias = kotlin_first(node, "identifier")
    if any(one.type == "*" for one in node.children):
        receiver = JAVA_GLOB
    elif alias is not None:
        receiver = node_text(alias).strip()
    else:
        receiver = name.rpartition(".")[2]
    refer("", IMPORTS, name, receiver, node)


def kotlin_bases(node: Node) -> list[Node]:
    """Return the types a class or object extends, implements or delegates."""
    bases: list[Node] = []
    for clause in node.named_children:
        if clause.type != "delegation_specifiers":
            continue
        for one in clause.named_children:
            inner = next(iter(one.named_children), None)
            if inner is not None and inner.type == "explicit_delegation":
                inner = next(iter(inner.named_children), None)
            if inner is not None and kotlin_type(inner):
                bases.append(inner)
    return bases


def kotlin_extended(node: Node) -> bool:
    """Say whether a function declaration has a receiver type before its name."""
    name = node.child_by_field_name("name")
    return name is not None and any(
        one.type in KOTLIN_TYPE_NAMES and one.end_byte <= name.start_byte
        for one in node.named_children
    )


def kotlin_constructed(node: Node | None) -> str:
    """Return the class a call creates when its callee is a capitalised name."""
    if node is None or node.type != "call_expression":
        return ""
    path = kotlin_name_path(next(iter(node.named_children), None))
    return path if path.rpartition(".")[2][:1].isupper() else ""


def kotlin_declared(node: Node) -> list[tuple[str, str]]:
    """Return each name a declaration introduces with the type it is given.

    A property with no type takes the class its initializer creates.
    """
    if node.type in KOTLIN_NAMED:
        name = kotlin_first(node, "identifier")
        if name is None:
            return []
        return [(node_text(name).strip(), kotlin_typed(node))]
    found: list[tuple[str, str]] = []
    for one in node.named_children:
        if one.type == "multi_variable_declaration":
            found.extend(kotlin_declared(one))
        if one.type != "variable_declaration":
            continue
        name = kotlin_first(one, "identifier")
        if name is None:
            continue
        kind = kotlin_typed(one)
        if not kind and node.type == "property_declaration":
            kind = kotlin_constructed(one.next_named_sibling)
        found.append((node_text(name).strip(), kind))
    return found


def kotlin_fields(node: Node, body: Node | None) -> dict[str, str]:
    """Return the type of each property of a class, constructor ones included.

    An enum entry is a property of the enum's own type.
    """
    fields: dict[str, str] = {}
    constructor = kotlin_first(node, "primary_constructor")
    parameters = None
    if constructor is not None:
        parameters = kotlin_first(constructor, "class_parameters")
    for one in parameters.named_children if parameters is not None else []:
        if one.type == "class_parameter":
            fields.update(kotlin_declared(one))
    for one in body.named_children if body is not None else []:
        if one.type == "property_declaration":
            fields.update(kotlin_declared(one))
        elif one.type == "enum_entry" and one.named_children:
            fields[node_text(one.named_children[0]).strip()] = go_name(node)
    return fields


def kotlin_variables(node: Node) -> dict[str, str]:
    """Return the type of each parameter and local under a node.

    A name declared twice with different types is left untyped.
    """
    found: dict[str, str] = {}
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in KOTLIN_LOCALS:
            for name, kind in kotlin_declared(current):
                found[name] = kind if found.get(name, kind) == kind else ""
        stack.extend(current.named_children)
    return found


def kotlin_name_path(node: Node | None) -> str:
    """Return a name made of identifiers and dots, empty for anything else."""
    if node is None:
        return ""
    if node.type == "identifier":
        return node_text(node).strip()
    if node.type != "navigation_expression" or len(node.named_children) != 2:
        return ""
    if not any(one.type == "." for one in node.children):
        return ""
    head = kotlin_name_path(node.named_children[0])
    tail = node.named_children[1]
    if not head or tail.type != "identifier":
        return ""
    return f"{head}.{node_text(tail).strip()}"


def kotlin_receiver(node: Node | None, variables: dict[str, str]) -> str:
    """Return what a call is made on, empty for a bare call.

    A variable stands for its type, a cast for the type cast to; a dotted
    name no variable starts is a type when its last part is capitalised.
    """
    if node is None:
        return ""
    if node.type in KOTLIN_UNWRAP:
        return kotlin_receiver(next(iter(node.named_children), None), variables)
    if node.type == "this_expression":
        return JAVA_SELF
    if node.type == "super_expression":
        return JAVA_SUPER
    if node.type == "as_expression":
        return kotlin_typed(node) or JAVA_VALUE
    if node.type == "call_expression":
        return kotlin_constructed(node) or JAVA_VALUE
    children = node.named_children
    if node.type == "navigation_expression" and len(children) == 2:
        if children[0].type == "this_expression":
            return variables.get(node_text(children[1]).strip(), "") or JAVA_VALUE
    dotted = kotlin_name_path(node)
    head = dotted.partition(".")[0]
    if head in variables:
        return (variables[head] if head == dotted else "") or JAVA_VALUE
    if dotted == KOTLIN_COMPANION:
        return JAVA_SELF
    dotted = dotted.removesuffix(f".{KOTLIN_COMPANION}")
    if dotted and dotted.rpartition(".")[2][:1].isupper():
        return dotted
    return JAVA_VALUE


def kotlin_callee(node: Node, variables: dict[str, str]) -> tuple[str, str]:
    """Split a callee into the name it calls and its receiver.

    A dotted name ending in a capital creates that class: JAVA_NEW.
    """
    if node.type == "identifier":
        return node_text(node).strip(), ""
    if node.type != "navigation_expression" or len(node.named_children) != 2:
        return "", ""
    target, member = node.named_children
    if member.type != "identifier":
        return "", ""
    dotted = kotlin_name_path(node)
    if dotted and dotted.partition(".")[0] not in variables:
        if dotted.rpartition(".")[2][:1].isupper():
            return dotted, JAVA_NEW
    return node_text(member).strip(), kotlin_receiver(target, variables)


def kotlin_calls(
    node: Node | None,
    scope: str,
    variables: dict[str, str],
    refer: Callable[[str, str, str, str, Node], None],
) -> None:
    """Refer every call and callable reference under a node to one scope."""
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        name, receiver = "", ""
        children = current.named_children
        if current.type == "call_expression" and children:
            name, receiver = kotlin_callee(children[0], variables)
        elif current.type == "callable_reference" and children:
            name = node_text(children[-1]).strip()
        elif current.type == "navigation_expression" and any(
            one.type == "::" for one in current.children
        ):
            name = node_text(children[-1]).strip()
            receiver = kotlin_receiver(children[0], variables)
        if name:
            refer(scope, CALLS, name, receiver, current)
        stack.extend(reversed(children))


class CSharpParser(CodeParser):
    """C# specific AST parser."""

    FAMILY = "dotnet"

    def __init__(self) -> None:
        """Initialize C# parser."""
        super().__init__(tree_sitter_c_sharp.language())

    def extract_code(
        self, content: str
    ) -> tuple[list[CodeSymbol], list[CodeReference]]:
        """Return the types, methods and constructors, and what they refer to.

        The file declares every namespace it opens, every type its qualified
        name and every extension method its name. Properties, operators,
        local functions and lambdas are no symbols: their calls go to the
        type or method holding them, top-level statements' to the file.
        """
        root = self.parse(content)
        symbols: list[CodeSymbol] = []
        references: dict[tuple[str, str, str, str], CodeReference] = {}

        def refer(scope: str, kind: str, name: str, receiver: str, node: Node) -> None:
            if name:
                references.setdefault(
                    (scope, kind, name, receiver),
                    CodeReference(scope, kind, name, receiver, node.start_point[0] + 1),
                )

        def declare(
            node: Node, prefix: str, parent: str, outer: dict[str, str]
        ) -> None:
            name, line = go_name(node), go_line(node)
            key = symbol_key(name, line)
            kind = "interface" if node.type == "interface_declaration" else "class"
            symbols.append(CodeSymbol(key, name, kind, line, parent))
            qualified = f"{prefix}.{name}" if prefix else name
            refer(key, DECLARES, qualified, JAVA_TYPE, node)
            for base in csharp_bases(node):
                refer(key, INHERITS, csharp_type(base), "", base)
            body = node.child_by_field_name("body")
            members = body.named_children if body is not None else []
            fields = {**outer, **csharp_fields(node, members)}
            for member in members:
                if member.type in CSHARP_TYPES and go_name(member):
                    declare(member, qualified, key, fields)
                elif member.type in CSHARP_METHODS and go_name(member):
                    method = go_name(member)
                    scope = symbol_key(f".{method}()", go_line(member))
                    symbols.append(
                        CodeSymbol(
                            scope, f".{method}()", "method", go_line(member), key
                        )
                    )
                    if csharp_extends(member):
                        refer(scope, DECLARES, method, KOTLIN_EXTENSION, member)
                    variables = {**fields, **csharp_variables(member)}
                    csharp_calls(member, scope, variables, refer)
                else:
                    csharp_calls(member, key, fields, refer)

        def walk(nodes: list[Node], space: str) -> None:
            statements: list[Node] = []
            for node in nodes:
                if node.type == "using_directive":
                    csharp_using(node, refer)
                elif node.type in CSHARP_NAMESPACES:
                    name = csharp_dotted(node.child_by_field_name("name"))
                    inner = f"{space}.{name}" if space and name else name or space
                    refer("", DECLARES, inner, JAVA_PACKAGE, node)
                    body = node.child_by_field_name("body")
                    if node.type == "namespace_declaration":
                        walk(body.named_children if body is not None else [], inner)
                    else:
                        space = inner
                elif node.type in CSHARP_TYPES and go_name(node):
                    declare(node, space, "", {})
                elif node.type == "global_statement":
                    statements.append(node)
            variables: dict[str, str] = {}
            for node in statements:
                variables.update(csharp_variables(node))
            for node in statements:
                csharp_calls(node, "", variables, refer)

        walk(root.named_children, "")
        return symbols, list(references.values())


CSHARP_TYPES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "struct_declaration",
        "record_declaration",
        "record_struct_declaration",
        "enum_declaration",
    }
)
CSHARP_METHODS = frozenset({"method_declaration", "constructor_declaration"})
CSHARP_NAMESPACES = frozenset(
    {"namespace_declaration", "file_scoped_namespace_declaration"}
)
CSHARP_LOCALS = frozenset(
    {
        "parameter",
        "variable_declaration",
        "foreach_statement",
        "catch_declaration",
        "declaration_expression",
        "declaration_pattern",
    }
)
CSHARP_NOT_CALLS = frozenset({"nameof"})


def csharp_dotted(node: Node | None) -> str:
    """Return a dotted name as written, without `global::` and whitespace."""
    return java_dotted(node).removeprefix("global::")


def csharp_type(node: Node | None) -> str:
    """Return a type as written, dotted, without its type arguments.

    Predefined, array, tuple and `var` types are empty: no method of the
    tree is called on them.
    """
    if node is None:
        return ""
    if node.type == "identifier":
        return node_text(node).strip()
    if node.type == "generic_name":
        return csharp_type(node.named_children[0] if node.named_children else None)
    if node.type == "qualified_name":
        head = csharp_type(node.child_by_field_name("qualifier"))
        tail = csharp_type(node.child_by_field_name("name"))
        return f"{head}.{tail}" if head and tail else ""
    if node.type in ("alias_qualified_name", "nullable_type"):
        field = "name" if node.type == "alias_qualified_name" else "type"
        return csharp_type(node.child_by_field_name(field))
    return ""


def csharp_using(node: Node, refer: Callable[[str, str, str, str, Node], None]) -> None:
    """Refer one using directive to the file, bound to the name it brings in.

    A namespace is bound to JAVA_GLOB, `using static` to JAVA_STATIC_GLOB and
    an alias to its own name.
    """
    alias = node.child_by_field_name("name")
    path = next(
        (
            one
            for one in node.named_children
            if alias is None or one.start_byte != alias.start_byte
        ),
        None,
    )
    name = csharp_type(path) if path is not None and path.type == "generic_name" else ""
    name = name or csharp_dotted(path)
    if alias is not None:
        receiver = node_text(alias).strip()
    elif any(not one.is_named and one.type == "static" for one in node.children):
        receiver = JAVA_STATIC_GLOB
    else:
        receiver = JAVA_GLOB
    refer("", IMPORTS, name, receiver, node)


def csharp_bases(node: Node) -> list[Node]:
    """Return the types a type declaration derives from or implements."""
    bases: list[Node] = []
    for clause in node.named_children:
        if clause.type != "base_list":
            continue
        for one in clause.named_children:
            if one.type == "primary_constructor_base_type":
                bases.append(one.child_by_field_name("type") or one)
            else:
                bases.append(one)
    return [one for one in bases if csharp_type(one)]


def csharp_extends(node: Node) -> bool:
    """Say whether a method is an extension method: `this` on its first parameter."""
    parameters = node.child_by_field_name("parameters")
    found = parameters.named_children if parameters is not None else []
    first = next((one for one in found if one.type == "parameter"), None)
    return first is not None and any(
        one.type == "modifier" and node_text(one).strip() == "this"
        for one in first.named_children
    )


def csharp_typed(kind: Node | None, value: Node | None) -> str:
    """Return a declared type, or the class a `var` is created from."""
    if kind is not None and kind.type == "implicit_type":
        if value is not None and value.type == "object_creation_expression":
            return csharp_type(value.child_by_field_name("type"))
        return ""
    return csharp_type(kind)


def csharp_declared(node: Node) -> list[tuple[str, str]]:
    """Return each name a declaration introduces with the type it is given."""
    kind = node.child_by_field_name("type")
    if node.type == "variable_declaration":
        found = []
        for one in node.named_children:
            name = one.child_by_field_name("name")
            if one.type != "variable_declarator" or name is None:
                continue
            values = [
                child
                for child in one.named_children
                if child.start_byte != name.start_byte
            ]
            value = values[-1] if values else None
            found.append((node_text(name).strip(), csharp_typed(kind, value)))
        return found
    name = node.child_by_field_name(
        "left" if node.type == "foreach_statement" else "name"
    )
    if name is None or name.type != "identifier":
        return []
    return [(node_text(name).strip(), csharp_typed(kind, None))]


def csharp_fields(node: Node, members: list[Node]) -> dict[str, str]:
    """Return the type of each field and property of a type.

    The parameters of a record or primary constructor are fields too.
    """
    fields: dict[str, str] = {}
    for holder in node.named_children:
        if holder.type == "parameter_list":
            for one in holder.named_children:
                if one.type == "parameter":
                    fields.update(csharp_declared(one))
    for one in members:
        if one.type in ("field_declaration", "event_field_declaration"):
            for declaration in one.named_children:
                if declaration.type == "variable_declaration":
                    fields.update(csharp_declared(declaration))
        elif one.type == "property_declaration":
            fields.update(csharp_declared(one))
    return fields


def csharp_variables(node: Node) -> dict[str, str]:
    """Return the type of each parameter and local a member declares.

    A name declared twice with different types is left untyped.
    """
    found: dict[str, str] = {}
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in CSHARP_LOCALS:
            for name, kind in csharp_declared(current):
                found[name] = kind if found.get(name, kind) == kind else ""
        stack.extend(current.named_children)
    return found


def csharp_name_path(node: Node | None) -> str:
    """Return a name made of identifiers and dots, empty for anything else."""
    if node is None:
        return ""
    if node.type == "identifier":
        return node_text(node).strip()
    if node.type == "alias_qualified_name":
        return csharp_name_path(node.child_by_field_name("name"))
    if node.type == "member_access_expression":
        name = node.child_by_field_name("name")
        head = csharp_name_path(node.child_by_field_name("expression"))
        if head and name is not None and name.type == "identifier":
            return f"{head}.{node_text(name).strip()}"
    return ""


def csharp_receiver(node: Node | None, variables: dict[str, str]) -> str:
    """Return what a call is made on, empty for a bare call.

    A variable, field or property stands for its declared type; a dotted name
    nothing declared starts is a type when its last part is capitalised.
    """
    if node is None:
        return ""
    if node.type == "this":
        return JAVA_SELF
    if node.type == "base":
        return JAVA_SUPER
    if node.type == "object_creation_expression":
        return csharp_type(node.child_by_field_name("type")) or JAVA_VALUE
    if node.type == "generic_name":
        return csharp_type(node) or JAVA_VALUE
    if node.type == "member_access_expression":
        target = node.child_by_field_name("expression")
        name = node.child_by_field_name("name")
        if target is not None and target.type == "this" and name is not None:
            return variables.get(node_text(name).strip(), "") or JAVA_VALUE
    dotted = csharp_name_path(node)
    head = dotted.partition(".")[0]
    if head in variables:
        return (variables[head] if head == dotted else "") or JAVA_VALUE
    if dotted and dotted.rpartition(".")[2][:1].isupper():
        return dotted
    return JAVA_VALUE


def csharp_callee(node: Node | None, variables: dict[str, str]) -> tuple[str, str]:
    """Split what an invocation calls into the name and its receiver."""
    if node is None:
        return "", ""
    if node.type in ("identifier", "generic_name"):
        name = csharp_type(node)
        return ("", "") if name in CSHARP_NOT_CALLS else (name, "")
    if node.type == "member_access_expression":
        name = csharp_type(node.child_by_field_name("name"))
        target = node.child_by_field_name("expression")
        return name, csharp_receiver(target, variables)
    if node.type == "conditional_access_expression":
        binding = next(
            (
                one
                for one in node.named_children
                if one.type == "member_binding_expression"
            ),
            None,
        )
        name = csharp_type(binding.child_by_field_name("name")) if binding else ""
        target = node.child_by_field_name("condition")
        return name, csharp_receiver(target, variables)
    return "", ""


def csharp_calls(
    node: Node | None,
    scope: str,
    variables: dict[str, str],
    refer: Callable[[str, str, str, str, Node], None],
) -> None:
    """Refer every invocation and `new` under a node to one scope."""
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        name, receiver = "", ""
        if current.type == "invocation_expression":
            function = current.child_by_field_name("function")
            name, receiver = csharp_callee(function, variables)
        elif current.type == "object_creation_expression":
            name = csharp_type(current.child_by_field_name("type"))
            receiver = JAVA_NEW
        if name:
            refer(scope, CALLS, name, receiver, current)
        stack.extend(reversed(current.named_children))
