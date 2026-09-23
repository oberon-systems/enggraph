import { describe, expect, it } from "vitest";
import { lexicalTerms } from "../src/search.js";

describe("lexicalTerms", () => {
  it("ORs the content words of a question", () => {
    expect(
      lexicalTerms("how does the indexer skip a file that has not changed").any,
    ).toBe("indexer | skip | file | has | not | changed");
  });

  it("keeps websearch syntax when the caller wrote operators", () => {
    expect(lexicalTerms('"file hash" -test').any).toBeNull();
    expect(lexicalTerms("queue OR lease").any).toBeNull();
  });

  it("falls back to websearch when nothing but stopwords is left", () => {
    expect(lexicalTerms("how is the").any).toBeNull();
  });

  it("turns identifier-shaped tokens into name patterns", () => {
    expect(
      lexicalTerms("where is readLimit and queue_depth set").names,
    ).toEqual(["%readlimit%", "%queue_depth%"]);
  });
});
