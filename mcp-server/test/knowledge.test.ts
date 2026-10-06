import { describe, expect, it } from "vitest";
import { narrowIds, parseNodeRefs } from "../src/knowledge.js";

describe("parseNodeRefs", () => {
  it("is null when nodes are not given, so stored links are kept", () => {
    expect(parseNodeRefs(undefined, "alpha")).toBeNull();
  });

  it("reads ids in the record's project and objects naming another", () => {
    expect(
      parseNodeRefs(
        [
          "src/auth/",
          { node_id: "src/auth/jwt.ts" },
          { project: "beta", node_id: "deploy/" },
          "src/auth/",
        ],
        "alpha",
      ),
    ).toEqual([
      { project: "alpha", node_id: "src/auth/" },
      { project: "alpha", node_id: "src/auth/jwt.ts" },
      { project: "beta", node_id: "deploy/" },
    ]);
  });

  it("refuses a node with no project to fall back on", () => {
    expect(() => parseNodeRefs(["src/"], null)).toThrow(/names no project/);
  });

  it("refuses what is not a node", () => {
    expect(() => parseNodeRefs("src/", "alpha")).toThrow(/array/);
    expect(() => parseNodeRefs([""], "alpha")).toThrow(/node id/);
    expect(() => parseNodeRefs([{ project: "beta" }], "alpha")).toThrow(
      /node id/,
    );
  });

  it("caps how many nodes one record names", () => {
    const many = Array.from({ length: 51 }, (_, index) => `src/${index}.ts`);
    expect(() => parseNodeRefs(many, "alpha")).toThrow(/at most/);
  });
});

describe("narrowIds", () => {
  it("keeps the filter open without a node, and intersects with one", () => {
    expect(narrowIds(null, null)).toBeNull();
    expect(narrowIds(["a", "b"], null)).toEqual(["a", "b"]);
    expect(narrowIds(null, ["b"])).toEqual(["b"]);
    expect(narrowIds(["a", "b"], ["b", "c"])).toEqual(["b"]);
  });
});
