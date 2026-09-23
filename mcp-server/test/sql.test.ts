import { readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import pg from "pg";
import ts from "typescript";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const DATABASE_URL = process.env.EVAL_DATABASE_URL;
const REPO = resolve(import.meta.dirname, "../..");
const SOURCES = ["mcp-server/src", "web/src/server"];
const STATEMENT = /^\s*(SELECT|WITH|INSERT|UPDATE|DELETE|SET)\s/;

interface Statement {
  where: string;
  sql: string;
}

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((item) => {
    const path = join(dir, item.name);
    if (item.isDirectory()) {
      return files(path);
    }
    return path.endsWith(".ts") ? [path] : [];
  });
}

function literal(node: ts.Node): string | null {
  return ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)
    ? node.text
    : null;
}

// A template is checked once its pieces are resolved against the file's own
// string constants; a constant used as such a piece is a fragment, not a query.
function statements(): Statement[] {
  const found: Statement[] = [];
  for (const root of SOURCES) {
    for (const path of files(join(REPO, root))) {
      const source = ts.createSourceFile(
        path,
        readFileSync(path, "utf8"),
        ts.ScriptTarget.ES2022,
        true,
      );
      const where = (node: ts.Node) =>
        `${relative(REPO, path)}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}`;
      const constants = new Map<string, ts.Node>();
      const templates: ts.TemplateExpression[] = [];
      const literals: ts.Node[] = [];
      const visit = (node: ts.Node): void => {
        if (
          ts.isVariableDeclaration(node) &&
          ts.isIdentifier(node.name) &&
          node.initializer !== undefined &&
          literal(node.initializer) !== null
        ) {
          constants.set(node.name.text, node.initializer);
        }
        if (ts.isTemplateExpression(node)) {
          templates.push(node);
        } else if (literal(node) !== null) {
          literals.push(node);
        }
        ts.forEachChild(node, visit);
      };
      visit(source);

      const fragments = new Set<ts.Node>();
      for (const template of templates) {
        let sql = template.head.text;
        let resolved = true;
        for (const span of template.templateSpans) {
          const piece = ts.isIdentifier(span.expression)
            ? constants.get(span.expression.text)
            : undefined;
          if (piece === undefined) {
            resolved = false;
            break;
          }
          fragments.add(piece);
          sql += (literal(piece) ?? "") + span.literal.text;
        }
        if (STATEMENT.test(sql)) {
          if (!resolved) {
            throw new Error(
              `${where(template)}: SQL built by interpolation cannot be checked`,
            );
          }
          found.push({ where: where(template), sql });
        }
      }
      for (const node of literals) {
        const sql = literal(node) ?? "";
        if (!fragments.has(node) && STATEMENT.test(sql)) {
          found.push({ where: where(node), sql });
        }
      }
    }
  }
  return found;
}

const found = statements();

describe("sql sources", () => {
  it("finds the statements", () => {
    expect(found.length).toBeGreaterThan(30);
  });
});

describe.skipIf(DATABASE_URL === undefined)("sql against the schema", () => {
  let client: pg.Client;

  beforeAll(async () => {
    client = new pg.Client({ connectionString: DATABASE_URL });
    await client.connect();
  });

  afterAll(async () => {
    await client.end();
  });

  // PREPARE parses and analyses without running: unknown columns, reserved
  // aliases and type errors surface here, with no rows needed.
  it.each(found.map((s) => [s.where, s.sql]))("%s", async (_where, sql) => {
    await client.query("BEGIN");
    try {
      if (/^\s*SET\b/.test(sql)) {
        await client.query(sql);
      } else {
        await client.query(`PREPARE checked AS ${sql}`);
      }
    } finally {
      await client.query("ROLLBACK");
      await client.query("DEALLOCATE ALL");
    }
  });
});
