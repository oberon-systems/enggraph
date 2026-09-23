import { describe, expect, it } from "vitest";
import {
  assemble,
  capFor,
  classify,
  estimateTokens,
  isTestPath,
  lineFromId,
  overlaps,
  shorten,
} from "../src/context.js";
import type { Candidate, ContextEntry, Tier } from "../src/context.js";

function entry(id: string, fields: Partial<ContextEntry> = {}): ContextEntry {
  return {
    project: "alpha",
    project_type: "codebase",
    id,
    name: id,
    type: "function",
    file_path: null,
    start_line: null,
    end_line: null,
    origin: "expansion",
    why: "test",
    summary: null,
    ...fields,
  };
}

function candidate(
  id: string,
  tier: Tier | null,
  fields: Partial<Omit<Candidate, "entry" | "tier">> = {},
  entryFields: Partial<ContextEntry> = {},
): Candidate {
  return {
    key: `alpha\u0000${id}`,
    tier,
    chunk: null,
    seedOrder: 0,
    place: 0,
    entry: entry(id, entryFields),
    ...fields,
  };
}

describe("estimateTokens", () => {
  it("rounds four characters per token up", () => {
    expect(estimateTokens("")).toBe(0);
    expect(estimateTokens("abcd")).toBe(1);
    expect(estimateTokens("abcde")).toBe(2);
  });
});

describe("isTestPath", () => {
  it.each([
    ["tests/test_auth.py", true],
    ["pkg/test/helper.ts", true],
    ["src/auth.test.ts", true],
    ["src/auth.spec.ts", true],
    ["pkg/test_refund.py", true],
    ["cmd/refund_test.go", true],
    ["src/latest.ts", false],
    ["src/contest/entry.ts", false],
    [null, false],
  ])("%s -> %s", (path, expected) => {
    expect(isTestPath(path)).toBe(expected);
  });
});

describe("classify", () => {
  it("places containment by direction", () => {
    expect(classify("contains", "incoming", "tests/a.py")).toBe("container");
    expect(classify("contains", "outgoing", "src/a.py")).toBe("defines");
  });

  it("puts a test file in the test tier whatever the edge", () => {
    expect(classify("calls", "incoming", "tests/a.py")).toBe("test");
    expect(classify("imports", "outgoing", "src/a.test.ts")).toBe("test");
  });

  it("reads dependency edges by direction", () => {
    expect(classify("imports", "incoming", "src/a.ts")).toBe("caller");
    expect(classify("imports", "outgoing", "src/a.ts")).toBe("import");
    expect(classify("calls", "incoming", "src/a.ts")).toBe("caller");
    expect(classify("calls", "outgoing", "src/a.ts")).toBe("callee");
  });
});

describe("capFor", () => {
  it("decays the bulk tiers with seed rank", () => {
    expect(capFor("defines", 0)).toBe(8);
    expect(capFor("defines", 3)).toBe(2);
    expect(capFor("defines", 5)).toBe(0);
    expect(capFor("import", 2)).toBe(4);
    expect(capFor("import", 9)).toBe(0);
  });

  it("keeps the scarce tiers flat", () => {
    expect(capFor("caller", 7)).toBe(8);
    expect(capFor("test", 7)).toBe(4);
  });
});

describe("ids", () => {
  it("reads the line a symbol id carries", () => {
    expect(lineFromId("src/a.py::refund@L70")).toBe(70);
    expect(lineFromId("src/a.py")).toBeNull();
  });

  it("shortens a path to its basename and keeps the symbol", () => {
    expect(shorten("src/pay/refund.py::Refund@L3")).toBe(
      "refund.py::Refund@L3",
    );
    expect(shorten("src/pay/refund.py")).toBe("refund.py");
    expect(shorten("top.py")).toBe("top.py");
  });
});

describe("overlaps", () => {
  const kept = [
    entry("a", { file_path: "src/a.ts", start_line: 10, end_line: 20 }),
  ];

  it("finds overlapping line ranges in the same file", () => {
    const other = entry("b", {
      file_path: "src/a.ts",
      start_line: 20,
      end_line: 30,
    });
    expect(overlaps(kept, other)).toBe(true);
  });

  it("ignores disjoint ranges, other files and other projects", () => {
    expect(
      overlaps(
        kept,
        entry("b", { file_path: "src/a.ts", start_line: 21, end_line: 30 }),
      ),
    ).toBe(false);
    expect(
      overlaps(
        kept,
        entry("b", { file_path: "src/b.ts", start_line: 10, end_line: 20 }),
      ),
    ).toBe(false);
    expect(
      overlaps(
        kept,
        entry("b", {
          project: "beta",
          file_path: "src/a.ts",
          start_line: 10,
          end_line: 20,
        }),
      ),
    ).toBe(false);
  });

  it("never drops an entry without lines", () => {
    expect(overlaps(kept, entry("b", { file_path: "src/a.ts" }))).toBe(false);
  });
});

describe("assemble", () => {
  it("places seeds first, then tiers in their fixed order", () => {
    const seeds = [candidate("seed", null)];
    const expanded = [
      candidate("imp", "import"),
      candidate("def", "defines"),
      candidate("call", "caller"),
      candidate("tst", "test"),
    ];
    const out = assemble(seeds, expanded, 10000, false);
    expect(out.entries.map((e) => e.id)).toEqual([
      "seed",
      "call",
      "tst",
      "def",
      "imp",
    ]);
    expect(out.truncated).toBe(false);
  });

  it("interleaves seeds within a tier by place", () => {
    const expanded = [
      candidate("s0p1", "caller", { seedOrder: 0, place: 1 }),
      candidate("s1p0", "caller", { seedOrder: 1, place: 0 }),
      candidate("s0p0", "caller", { seedOrder: 0, place: 0 }),
    ];
    const out = assemble([], expanded, 10000, false);
    expect(out.entries.map((e) => e.id)).toEqual(["s0p0", "s1p0", "s0p1"]);
  });

  it("drops an expanded entry already covered by a kept range", () => {
    const seeds = [
      candidate(
        "seed",
        null,
        {},
        { file_path: "src/a.ts", start_line: 1, end_line: 50 },
      ),
    ];
    const expanded = [
      candidate(
        "inner",
        "defines",
        {},
        { file_path: "src/a.ts", start_line: 5, end_line: 9 },
      ),
    ];
    expect(
      assemble(seeds, expanded, 10000, false).entries.map((e) => e.id),
    ).toEqual(["seed"]);
  });

  it("keeps a seed's bare reference when its chunk does not fit", () => {
    const seeds = [candidate("seed", null, { chunk: "x".repeat(4000) })];
    const out = assemble(seeds, [], 200, true);
    expect(out.entries).toHaveLength(1);
    expect(out.entries[0].chunk).toBeUndefined();
    expect(out.truncated).toBe(true);
    expect(out.used).toBeLessThanOrEqual(200);
  });

  it("upgrades expanded entries with chunks only after everything is placed", () => {
    const expanded = [
      candidate("a", "caller", { chunk: "a".repeat(40) }),
      candidate("b", "import"),
    ];
    const out = assemble([], expanded, 10000, true);
    expect(out.entries.map((e) => e.id)).toEqual(["a", "b"]);
    expect(out.entries[0].chunk).toBe("a".repeat(40));
  });

  it("never spends more than the budget", () => {
    const expanded = Array.from({ length: 50 }, (_, i) =>
      candidate(`n${String(i).padStart(2, "0")}`, "defines", { place: i }),
    );
    const out = assemble([], expanded, 300, false);
    expect(out.used).toBeLessThanOrEqual(300);
    expect(out.truncated).toBe(true);
    expect(out.entries.length).toBeGreaterThan(0);
    expect(out.entries.length).toBeLessThan(50);
  });
});
