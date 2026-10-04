import { describe, expect, it } from "vitest";

import { traceChains } from "../src/trace.js";
import type { TracePoint, TraceStep, TraceWay } from "../src/trace.js";

function step(
  from: TracePoint,
  to: TracePoint,
  way: TraceWay,
  relation: string,
  kind: string | null = null,
  name: string | null = null,
): TraceStep {
  return { from, to, way, relation, kind, name, origin: "matched" };
}

describe("traceChains", () => {
  const code = { project: "alpha", id: "tools/keeper/src/main.py" };
  const built = { project: "alpha", id: "tools/keeper/" };
  const klass = { project: "beta", id: "modules/keeper/manifests/init.pp" };
  const role = { project: "beta", id: "data/role/keeper.yaml" };
  const node = { project: "beta", id: "data/node/web-01.example.com.yaml" };
  const host = { project: "gamma", id: "live/web/config.yaml" };

  const steps = [
    step(code, built, "contains", "provides", "image", "example.com/keeper"),
    step(built, klass, "taken_by", "uses_image", "image", "example.com/keeper"),
    step(klass, role, "applied_by", "includes_class"),
    step(role, node, "applied_by", "selects"),
    step(node, host, "uses", "deploys_to", "host", "web-01.example.com"),
  ];

  it("joins the steps into one chain from the code to the host", () => {
    expect(traceChains(code, steps)).toEqual([
      "alpha:tools/keeper/src/main.py" +
        " -[contains provides image example.com/keeper]-> alpha:tools/keeper/" +
        " -[taken_by uses_image image example.com/keeper]-> beta:modules/keeper/manifests/init.pp" +
        " -[applied_by includes_class]-> beta:data/role/keeper.yaml" +
        " -[applied_by selects]-> beta:data/node/web-01.example.com.yaml" +
        " -[uses deploys_to host web-01.example.com]-> gamma:live/web/config.yaml",
    ]);
  });

  it("ends a chain at every point the walk went no further from", () => {
    const other = { project: "beta", id: "data/node/web-02.example.com.yaml" };
    const chains = traceChains(code, [
      ...steps,
      step(role, other, "applied_by", "selects"),
    ]);
    expect(chains).toHaveLength(2);
    expect(chains[1].endsWith("beta:data/node/web-02.example.com.yaml")).toBe(
      true,
    );
  });

  it("has no chain when nothing was found", () => {
    expect(traceChains(code, [])).toEqual([]);
  });
});
