import { describe, expect, it } from "vitest";
import { candidateIds, ROOT_ID, shape } from "../src/overview.js";

function row(
  id: string,
  parent: string | null,
  depth: number,
  type = "directory",
) {
  return {
    project: "alpha",
    id,
    parent,
    depth,
    name: id,
    type,
    summary: `Summary of ${id}`,
    summary_source: "auto",
    children: 0,
  };
}

describe("candidateIds", () => {
  it("reads every spelling of the root as the root", () => {
    for (const path of ["", ".", "/", "./", "  "]) {
      expect(candidateIds(path)).toEqual([ROOT_ID]);
    }
  });

  it("tries a directory with and without its slash", () => {
    expect(candidateIds("src/api")).toEqual(["src/api", "src/api/"]);
    expect(candidateIds("./src/api/")).toEqual(["src/api/", "src/api"]);
  });
});

describe("shape", () => {
  it("nests children under their parents, directories first", () => {
    const { trees, truncated } = shape(
      [
        row("./", null, 0),
        row("README.md", "./", 1, "file"),
        row("src/", "./", 1),
        row("src/a.ts", "src/", 2, "file"),
      ],
      10000,
    );
    expect(truncated).toBe(false);
    expect(trees).toHaveLength(1);
    const root = trees[0].root;
    expect(root.items?.map((item) => item.id)).toEqual(["src/", "README.md"]);
    expect(root.items?.[0].items?.map((item) => item.id)).toEqual(["src/a.ts"]);
  });

  it("cuts the deepest levels first when the budget runs out", () => {
    const rows = [
      row("./", null, 0),
      ...Array.from({ length: 20 }, (_, at) => row(`d${at}/`, "./", 1)),
      row("d0/deep.ts", "d0/", 2, "file"),
    ];
    const { trees, used, truncated } = shape(rows, 200);
    expect(truncated).toBe(true);
    expect(used).toBeLessThanOrEqual(200);
    expect(trees[0].root.items?.length).toBeLessThan(20);
    expect(trees[0].root.items?.[0].items).toBeUndefined();
  });
});
