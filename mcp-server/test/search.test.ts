import { describe, expect, it } from "vitest";
import { lexicalTerms, stem } from "../src/search.js";

describe("stem", () => {
  it("strips a plural or a tense down to a searchable root", () => {
    expect(stem("refunds")).toBe("refund");
    expect(stem("changed")).toBe("chang");
    expect(stem("queries")).toBe("quer");
    expect(stem("stopped")).toBe("stop");
  });

  it("keeps a word whose root would be shorter than four letters", () => {
    expect(stem("uses")).toBe("uses");
    expect(stem("files")).toBe("file");
  });

  it("undoubles a consonant only after a verb ending", () => {
    expect(stem("calls")).toBe("call");
  });
});

describe("lexicalTerms", () => {
  it("ORs the content words of a question as prefixes", () => {
    expect(
      lexicalTerms("how does the indexer skip a file that has not changed")
        .terms,
    ).toEqual(["indexer:*", "skip:*", "file:*", "has", "not", "chang:*"]);
  });

  it("splits camelCase and snake_case the way lexical_words does", () => {
    expect(lexicalTerms("where is PendingRefund handled").terms).toEqual([
      "pend:*",
      "refund:*",
      "handl:*",
    ]);
    expect(lexicalTerms("queue_depth of HTTPServer").terms).toEqual([
      "queue:*",
      "depth:*",
      "http:*",
      "server:*",
    ]);
  });

  it("keeps websearch syntax when the caller wrote operators", () => {
    expect(lexicalTerms('"file hash" -test').terms).toBeNull();
    expect(lexicalTerms("queue OR lease").terms).toBeNull();
  });

  it("falls back to websearch when nothing but stopwords is left", () => {
    expect(lexicalTerms("how is the").terms).toBeNull();
  });

  it("turns identifier-shaped tokens into name patterns", () => {
    expect(
      lexicalTerms("where is readLimit and queue_depth set").names,
    ).toEqual(["%readlimit%", "%queue_depth%"]);
  });
});
