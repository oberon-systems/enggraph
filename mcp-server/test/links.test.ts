import { describe, expect, it } from "vitest";
import {
  ancestorIds,
  edgesAlongWalk,
  nameKey,
  normalizeName,
} from "../src/links.js";
import type { ProjectEdge } from "../src/links.js";

describe("normalizeName", () => {
  it.each([
    ["image", "alpha-worker:latest", "alpha-worker"],
    [
      "image",
      "example.com:5000/alpha/worker:1.2",
      "example.com:5000/alpha/worker",
    ],
    ["image", "docker.io/library/postgres:16@sha256:abc", "postgres"],
    ["image", "$WORKER_IMAGE", ""],
    ["pypi", "Alpha_Worker>=1.0", "alpha-worker"],
    ["pypi", "beta.client[extra] ; python_version > '3'", "beta-client"],
    ["role", "../roles/alpha", "alpha"],
    ["npm", "@alpha/api", "@alpha/api"],
    ["go", "  ", ""],
    ["host", "Web-01.Example.com.", "web-01.example.com"],
    [
      "tfmodule",
      "git::https://example.com/alpha/infra.git//modules/vpc?ref=v1",
      "example.com/alpha/infra//modules/vpc",
    ],
    ["tfmodule", "git@example.com:alpha/infra.git", "example.com/alpha/infra"],
    ["tfmodule", "alpha/network/openstack", "alpha/network/openstack"],
    ["bucket", "Repo", "repo"],
    ["package", "%{name}-devel", ""],
  ])("%s %s is %s", (kind, raw, expected) => {
    expect(normalizeName(kind, raw)).toBe(expected);
  });
});

describe("nameKey", () => {
  it("drops case and separators, so every spelling of a name agrees", () => {
    expect(nameKey("Alpha_Web_01_example_com")).toBe(
      nameKey("alpha-web-01.example.com"),
    );
    expect(nameKey(" -._/ ")).toBe("");
  });
});

describe("ancestorIds", () => {
  it("climbs from a symbol to the root, nearest first", () => {
    expect(ancestorIds("src/auth/jwt.ts::sign@L4")).toEqual([
      "src/auth/",
      "src/",
      "./",
    ]);
  });

  it("climbs from a directory and stops at the root", () => {
    expect(ancestorIds("roles/alpha/")).toEqual(["roles/", "./"]);
    expect(ancestorIds("./")).toEqual([]);
    expect(ancestorIds("README.md")).toEqual(["./"]);
  });
});

describe("edgesAlongWalk", () => {
  function edge(from: string, to: string): ProjectEdge {
    return {
      from,
      to,
      relation: "uses_image",
      kind: "image",
      origin: "matched",
      count: 1,
      samples: [],
    };
  }
  const edges = [
    edge("alpha", "beta"),
    edge("beta", "gamma"),
    edge("beta", "delta"),
    edge("gamma", "delta"),
  ];

  it("keeps the edges the outgoing walk took", () => {
    const hops = { alpha: 0, beta: 1, gamma: 2, delta: 2 };
    expect(
      edgesAlongWalk(edges, hops, "outgoing").map((e) => `${e.from}>${e.to}`),
    ).toEqual(["alpha>beta", "beta>gamma", "beta>delta"]);
  });

  it("reads an incoming walk the other way round", () => {
    const hops = { delta: 0, beta: 1, gamma: 1, alpha: 2 };
    expect(
      edgesAlongWalk(edges, hops, "incoming").map((e) => `${e.from}>${e.to}`),
    ).toEqual(["alpha>beta", "beta>delta", "gamma>delta"]);
  });

  it("keeps an edge between two members of an organization", () => {
    const hops = { alpha: 0, beta: 0, gamma: 1 };
    expect(
      edgesAlongWalk(edges, hops, "outgoing").map((e) => `${e.from}>${e.to}`),
    ).toEqual(["alpha>beta", "beta>gamma"]);
  });

  it("drops an edge to a project the walk never reached", () => {
    expect(edgesAlongWalk(edges, { alpha: 0, beta: 1 }, "both")).toHaveLength(
      1,
    );
  });
});
