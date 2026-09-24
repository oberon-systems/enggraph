"""How imports and calls the extractor drops are linked across files."""

from __future__ import annotations

from enggraph.crossfile import link_cross_file, strip_handled_imports
from enggraph.resolution import resolve_import, suffix_index


def _node(label: str, source_file: str, line: int) -> dict[str, str]:
    return {
        "id": label,
        "label": label,
        "source_file": source_file,
        "source_location": f"L{line}",
    }


SERVICE = """export class PaymentService {
  refund(id: string): void {}
}
"""
CONTROLLER = """import type { PaymentService } from "./service.js";
import express from "express";

export class RefundController {
  constructor(private readonly payments: PaymentService) {}

  refund(id: string): void {
    this.payments.refund(id);
  }
}
"""
STORE = """export function save(item: string): void {}
export function verify(item: string): boolean { return true; }
"""
AUDIT = """export function verify(item: string): boolean { return true; }
"""
RUNNER = """import { save } from "./store.js";
import { verify } from "./audit.js";

function check(item: string): boolean { return true; }

export function run(items: string[]): void {
  items.forEach((item) => save(item));
  items.forEach((item) => verify(item));
  check(items[0]);
}
"""
JOBS = """def start() -> None:
    pass
"""
MAIN = """from alpha.jobs import start

start()
"""

CONTENTS = {
    "src/pay/service.ts": SERVICE,
    "src/pay/controller.ts": CONTROLLER,
    "src/work/store.ts": STORE,
    "src/work/audit.ts": AUDIT,
    "src/work/runner.ts": RUNNER,
    "py/src/alpha/jobs.py": JOBS,
    "main.py": MAIN,
}
NODES = [
    _node("service.ts", "src/pay/service.ts", 1),
    _node("PaymentService", "src/pay/service.ts", 1),
    _node(".refund()", "src/pay/service.ts", 2),
    _node("controller.ts", "src/pay/controller.ts", 1),
    _node("RefundController", "src/pay/controller.ts", 4),
    _node(".constructor()", "src/pay/controller.ts", 5),
    _node(".refund()", "src/pay/controller.ts", 7),
    _node("store.ts", "src/work/store.ts", 1),
    _node("save()", "src/work/store.ts", 1),
    _node("verify()", "src/work/store.ts", 2),
    _node("audit.ts", "src/work/audit.ts", 1),
    _node("verify()", "src/work/audit.ts", 1),
    _node("runner.ts", "src/work/runner.ts", 1),
    _node("check()", "src/work/runner.ts", 4),
    _node("run()", "src/work/runner.ts", 6),
    _node("jobs.py", "py/src/alpha/jobs.py", 1),
    _node("start()", "py/src/alpha/jobs.py", 1),
    _node("main.py", "main.py", 1),
]


def _links() -> tuple[set[tuple[str, str, str, bool]], int]:
    links, dropped = link_cross_file(NODES, CONTENTS, set(CONTENTS))
    return {
        (link.source_id, link.target_id, link.relation, link.external) for link in links
    }, dropped


def test_an_import_reaches_the_file_it_names() -> None:
    """A `.js` specifier of a TypeScript file lands on the `.ts` file node."""
    links, _ = _links()
    assert (
        "src/pay/controller.ts",
        "src/pay/service.ts",
        "imports_from",
        False,
    ) in links


def test_a_package_import_stays_external() -> None:
    """A bare specifier names no file of the tree and keeps its placeholder."""
    links, _ = _links()
    assert ("src/pay/controller.ts", "express", "imports_from", True) in links


def test_a_call_through_a_field_reaches_the_imported_method() -> None:
    """The local `refund` does not shadow a call made through another object."""
    links, _ = _links()
    assert (
        "src/pay/controller.ts::.refund()@L7",
        "src/pay/service.ts::.refund()@L2",
        "calls",
        False,
    ) in links


def test_a_call_in_a_callback_belongs_to_the_named_function() -> None:
    """The anonymous arrow has no node, so the call is the enclosing one's."""
    links, _ = _links()
    assert (
        "src/work/runner.ts::run()@L6",
        "src/work/store.ts::save()@L1",
        "calls",
        False,
    ) in links


def test_a_name_two_imports_declare_is_dropped() -> None:
    """Picking one of the two `verify` would state a relation nobody has."""
    links, dropped = _links()
    assert not any(
        source.startswith("src/work/runner.ts") and "verify" in target
        for source, target, _, _ in links
    )
    assert dropped == 1


def test_a_local_name_is_left_to_the_extractor() -> None:
    """A bare call to a function of the same file is already linked upstream."""
    links, _ = _links()
    assert not any("check()" in target for _, target, _, _ in links)


def test_a_module_level_call_belongs_to_the_file() -> None:
    """A Python package under a source root resolves, and so does its call."""
    links, _ = _links()
    assert ("main.py", "py/src/alpha/jobs.py", "imports_from", False) in links
    assert ("main.py", "py/src/alpha/jobs.py::start()@L1", "calls", False) in links


def test_the_extractors_imports_are_stripped_only_for_handled_files() -> None:
    """Its mangled placeholders go, the edges of other files stay."""
    extraction = {
        "edges": [
            {"relation": "imports_from", "source_file": "a.ts", "target": "b_js"},
            {"relation": "calls", "source_file": "a.ts", "target": "x"},
            {"relation": "imports", "source_file": "c.go", "target": "fmt"},
        ]
    }
    assert strip_handled_imports(extraction, {"a.ts"}) == 1
    assert [edge["target"] for edge in extraction["edges"]] == ["x", "fmt"]


def test_a_suffix_resolves_only_when_one_file_ends_with_it() -> None:
    """Two packages of the same name under two roots resolve to neither."""
    known = {"one/src/alpha/util.py", "two/src/alpha/util.py", "one/src/beta.py"}
    suffixes = suffix_index(known)
    assert resolve_import("beta", "main.py", known, suffixes) == "one/src/beta.py"
    assert resolve_import("alpha.util", "main.py", known, suffixes) is None
    assert resolve_import("beta", "main.py", known) is None
