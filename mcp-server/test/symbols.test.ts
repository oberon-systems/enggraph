import { describe, expect, it } from "vitest";
import {
  bareName,
  bucketImpact,
  callPattern,
  implementationPattern,
  importPattern,
  mergeHits,
  parseSymbol,
  wordPattern,
} from "../src/symbols.js";
import type { SymbolHit } from "../src/symbols.js";

function hit(id: string, fields: Partial<SymbolHit> = {}): SymbolHit {
  return {
    project: "alpha",
    id,
    name: id,
    type: "function",
    file_path: null,
    line: null,
    relation: "calls",
    evidence: "graph",
    confidence: "INFERRED",
    hop: 1,
    ...fields,
  };
}

function text(file: string, lines: number[], hop = 1): SymbolHit {
  return hit(file, {
    type: "file",
    file_path: file,
    line: lines[0] ?? null,
    lines,
    relation: "mentions",
    evidence: "text",
    confidence: "NAME_MATCH",
    hop,
  });
}

describe("parseSymbol", () => {
  it("splits an owner from a member", () => {
    expect(parseSymbol("Alpha.beta")).toEqual({
      owner: "Alpha",
      name: "beta",
      path: null,
    });
  });

  it("keeps only the nearest owner of a dotted chain", () => {
    expect(parseSymbol("pkg.Alpha.beta").owner).toBe("Alpha");
    expect(parseSymbol("Alpha::beta").owner).toBe("Alpha");
    expect(parseSymbol("Alpha#beta").owner).toBe("Alpha");
  });

  it("drops the extractor's decoration", () => {
    expect(parseSymbol("beta()")).toEqual({
      owner: null,
      name: "beta",
      path: null,
    });
    expect(parseSymbol(".beta()").name).toBe("beta");
    expect(bareName(".beta()")).toBe("beta");
  });

  it("reads a path as a file with its stem as the name", () => {
    expect(parseSymbol("src/alpha/beta.ts")).toEqual({
      owner: null,
      name: "beta",
      path: "src/alpha/beta.ts",
    });
    expect(parseSymbol("./beta.py").path).toBe("beta.py");
  });
});

describe("text patterns", () => {
  it("matches whole words only", () => {
    const { js, pg } = wordPattern(["beta"]);
    expect(js.test("return beta + 1")).toBe(true);
    expect(js.test("return alphabeta")).toBe(false);
    expect(pg).toBe("(?n)\\mbeta\\M");
  });

  it("joins several names into one alternation", () => {
    const { js } = wordPattern(["alpha", "beta", "alpha"]);
    expect(js.source).toBe("\\b(alpha|beta)\\b");
  });

  it("wants a call shape for a call", () => {
    const { js } = callPattern(["beta"]);
    expect(js.test("this.alpha.beta (1)")).toBe(true);
    expect(js.test("const beta = 1")).toBe(false);
  });

  it("escapes what a regex would read", () => {
    expect(wordPattern(["a$b"]).js.test("x a$b y")).toBe(true);
  });

  it("finds an implementation in either language", () => {
    const { js } = implementationPattern("Beta");
    expect(js.test("class Alpha extends Beta {")).toBe(true);
    expect(js.test("class Alpha implements Gamma, Beta {")).toBe(true);
    expect(js.test("class Alpha(Base, Beta):")).toBe(true);
    expect(js.test("const x: Beta = y")).toBe(false);
  });

  it("finds an import of a stem on one line", () => {
    const { js } = importPattern("beta");
    expect(js.test('import { Beta } from "./beta";')).toBe(true);
    expect(js.test("from alpha.beta import Beta")).toBe(true);
    expect(js.test("const beta = 1")).toBe(false);
  });
});

describe("mergeHits", () => {
  it("keeps one row per graph node", () => {
    const merged = mergeHits([hit("a"), hit("a", { hop: 2 })], []);
    expect(merged).toHaveLength(1);
  });

  it("drops text lines a graph hit already stands on", () => {
    const merged = mergeHits(
      [hit("x.ts::f()@L4", { file_path: "x.ts", line: 4 })],
      [text("x.ts", [4, 9])],
    );
    expect(merged[1].lines).toEqual([9]);
    expect(merged[1].line).toBe(9);
  });

  it("drops a text row left without lines", () => {
    const merged = mergeHits(
      [hit("x.ts::f()@L4", { file_path: "x.ts", line: 4 })],
      [text("x.ts", [4])],
    );
    expect(merged).toHaveLength(1);
  });

  it("folds two text rows of one file into one", () => {
    const merged = mergeHits([], [text("y.ts", [7, 3]), text("y.ts", [3, 1])]);
    expect(merged).toHaveLength(1);
    expect(merged[0].lines).toEqual([1, 3, 7]);
  });
});

describe("bucketImpact", () => {
  const hits = [
    hit("src/alpha/service.ts", { file_path: "src/alpha/service.ts" }),
    hit("src/routes.ts", { file_path: "src/routes.ts", hop: 2 }),
    hit("test/alpha.test.ts", { file_path: "test/alpha.test.ts" }),
    hit("deploy/compose.yml", {
      file_path: "deploy/compose.yml",
      relation: "uses_file",
      hop: 3,
    }),
  ];
  const impact = bucketImpact(hits);

  it("splits direct from indirect by hop", () => {
    expect(impact.counts.direct).toBe(2);
    expect(impact.counts.indirect).toBe(2);
  });

  it("files tests, the public API and configuration apart", () => {
    expect(impact.tests.map((h) => h.id)).toEqual(["test/alpha.test.ts"]);
    expect(impact.public_api.map((h) => h.id)).toEqual(["src/routes.ts"]);
    expect(impact.configuration.map((h) => h.id)).toEqual([
      "deploy/compose.yml",
    ]);
    expect(impact.counts.cross_project).toBe(0);
  });

  it("lists the files nearest first", () => {
    expect(impact.files[impact.files.length - 1]).toBe("deploy/compose.yml");
    expect(impact.counts.files).toBe(4);
  });
});
