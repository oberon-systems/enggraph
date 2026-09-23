import { describe, expect, it } from "vitest";
import { rerank } from "../src/rerank.js";
import type { Candidate } from "../src/rerank.js";

function row(id: string, fields: Partial<Candidate> = {}): Candidate {
  return {
    project: "alpha",
    project_type: "codebase",
    id,
    name: id,
    type: "function",
    file_path: null,
    rrf: 0.01,
    lexical_rank: 1,
    vector_rank: null,
    in_degree: 0,
    ...fields,
  };
}

function ids(ranked: { row: Candidate }[]): string[] {
  return ranked.map(({ row: r }) => r.id);
}

describe("rerank", () => {
  it("lifts an exact identifier over a better fused score", () => {
    const rows = [
      row("other", { rrf: 0.015 }),
      row("hit", { name: "readLimit", rrf: 0.01 }),
    ];
    expect(ids(rerank(rows, "where is readLimit set", 10, true))).toEqual([
      "hit",
      "other",
    ]);
  });

  it("weighs an identifier-shaped token over a plain word", () => {
    const rows = [
      row("plain", { name: "queue" }),
      row("shaped", { name: "readLimit" }),
    ];
    expect(ids(rerank(rows, "queue readLimit", 10, true))).toEqual([
      "shaped",
      "plain",
    ]);
  });

  it("rewards query words found in the path", () => {
    const rows = [
      row("elsewhere", { file_path: "src/other/thing.ts" }),
      row("matching", { file_path: "src/payment/refund.ts" }),
    ];
    expect(ids(rerank(rows, "payment refund", 10, true))[0]).toBe("matching");
  });

  it("pushes external placeholders and prose below code", () => {
    const rows = [
      row("ext", { type: "external_module" }),
      row("heading", { type: "heading" }),
      row("code"),
    ];
    expect(ids(rerank(rows, "nothing relevant", 10, true))).toEqual([
      "code",
      "heading",
      "ext",
    ]);
  });

  it("keeps prose in a docs project unpenalised", () => {
    const rows = [
      row("code", { rrf: 0.0101 }),
      row("doc", { type: "heading", project_type: "docs" }),
    ];
    expect(ids(rerank(rows, "nothing relevant", 10, true))[0]).toBe("code");
    const scores = rerank(rows, "nothing relevant", 10, true).map(
      (r) => r.score,
    );
    expect(scores[0] - scores[1]).toBeLessThan(0.001);
  });

  it("rewards rows sharing a file with other candidates", () => {
    const rows = [
      row("alone", { file_path: "src/a.ts" }),
      row("pair1", { file_path: "src/b.ts" }),
      row("pair2", { file_path: "src/b.ts" }),
    ];
    expect(ids(rerank(rows, "nothing relevant", 10, true))[2]).toBe("alone");
  });

  it("rewards in-degree", () => {
    const rows = [row("leaf"), row("hub", { in_degree: 20 })];
    expect(ids(rerank(rows, "nothing relevant", 10, true))[0]).toBe("hub");
  });

  it("sinks vendored paths below own code", () => {
    const rows = [
      row("dep", { file_path: "lib/vendor/alpha/store.php", rrf: 0.016 }),
      row("own", { file_path: "src/store.php", rrf: 0.01 }),
    ];
    expect(ids(rerank(rows, "store", 10, true))).toEqual(["own", "dep"]);
  });

  it("sinks tests unless the question asks for them", () => {
    const rows = [
      row("spec", { file_path: "graphify/tests/test_hash.py", rrf: 0.0165 }),
      row("impl", { file_path: "graphify/src/hash.py", rrf: 0.0163 }),
    ];
    expect(ids(rerank(rows, "compute the hash", 10, true))).toEqual([
      "impl",
      "spec",
    ]);
    expect(ids(rerank(rows, "tests for the hash", 10, true))).toEqual([
      "spec",
      "impl",
    ]);
  });

  it("returns the fused order and score when disabled", () => {
    const rows = [
      row("low", { name: "readLimit", rrf: 0.01 }),
      row("high", { rrf: 0.02 }),
    ];
    const ranked = rerank(rows, "readLimit", 10, false);
    expect(ids(ranked)).toEqual(["high", "low"]);
    expect(ranked.map((r) => r.score)).toEqual([0.02, 0.01]);
  });

  it("interleaves projects by their own rank", () => {
    const rows = [
      row("a1", { rrf: 0.03 }),
      row("a2", { rrf: 0.02 }),
      row("b1", { project: "beta", rrf: 0.01 }),
    ];
    expect(ids(rerank(rows, "x", 10, false))).toEqual(["a1", "b1", "a2"]);
  });

  it("breaks ties by id and cuts to the limit", () => {
    const rows = [row("c"), row("a"), row("b")];
    expect(ids(rerank(rows, "x", 2, true))).toEqual(["a", "b"]);
  });
});
