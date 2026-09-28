import { describe, expect, it } from "vitest";
import { skillInstructions, skillPath, stamp } from "../src/skills.js";

describe("stamp", () => {
  it("adds the version to the frontmatter", () => {
    expect(stamp("---\nname: alpha\n---\n\nbody\n", "v1")).toBe(
      "---\nname: alpha\nversion: v1\n---\n\nbody\n",
    );
  });

  it("replaces a version already there", () => {
    expect(stamp("---\nversion: old\nname: alpha\n---\nbody", "v2")).toBe(
      "---\nname: alpha\nversion: v2\n---\nbody",
    );
  });

  it("adds a frontmatter to text without one", () => {
    expect(stamp("body\n", "v3")).toBe("---\nversion: v3\n---\n\nbody\n");
  });
});

describe("skillInstructions", () => {
  it("lists every skill with its version", () => {
    const text = skillInstructions([
      { name: "alpha", content: "", sha256: "aa", source: "repo", owner: null },
      { name: "beta", content: "", sha256: "bb", source: "import", owner: "x" },
    ]);
    expect(text).toContain("- alpha aa");
    expect(text).toContain("- beta bb");
    expect(text).toContain("get_skill");
  });

  it("points at list_skills when the list could not be read", () => {
    expect(skillInstructions(null)).toContain("list_skills");
  });
});

describe("skillPath", () => {
  it("is where the agents read skills from", () => {
    expect(skillPath("alpha")).toBe(".claude/skills/alpha/SKILL.md");
  });
});
