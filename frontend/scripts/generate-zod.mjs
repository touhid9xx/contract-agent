#!/usr/bin/env node
/**
 * Generate Zod schemas from JSON Schema.
 *
 * Reads:  ../contracts/schemas/*.json
 * Writes: src/lib/schemas/*.ts
 *
 * Why?
 *   Pydantic is the source of truth. JSON Schema is the bridge.
 *   Zod is the frontend consumer. This script is the final hop.
 *
 * Usage:
 *   node scripts/generate-zod.mjs
 *   node scripts/generate-zod.mjs --check   # CI mode
 */
import { readdir, readFile, writeFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { jsonSchemaToZod } from "json-schema-to-zod";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCHEMAS_DIR = resolve(__dirname, "../../contracts/schemas");
const OUT_DIR = resolve(__dirname, "../src/lib/schemas");

const checkMode = process.argv.includes("--check");

async function main() {
  if (!existsSync(SCHEMAS_DIR)) {
    console.error(`❌ Schemas dir not found: ${SCHEMAS_DIR}`);
    process.exit(1);
  }

  await mkdir(OUT_DIR, { recursive: true });

  const files = (await readdir(SCHEMAS_DIR)).filter((f) => f.endsWith(".json"));
  if (files.length === 0) {
    console.log("⚠️  No JSON schemas found — nothing to generate");
    return;
  }

  let drift = false;
  let written = 0;

  for (const file of files) {
    const name = file.replace(/\.json$/, "");
    const jsonRaw = await readFile(join(SCHEMAS_DIR, file), "utf-8");
    const jsonSchema = JSON.parse(jsonRaw);

    // Generate Zod code
    let zodCode = jsonSchemaToZod(jsonSchema, { module: "esm" });

    // Clean up the output: strip `export default` → named export
    zodCode = zodCode.replace(/^export default /m, `export const ${name}Schema = `);
    zodCode = zodCode.replace(/;$/, "");

    const content = [
      "/**",
      ` * AUTO-GENERATED from contracts/schemas/${file}`,
      " * DO NOT EDIT — regenerate with: npm run schemas:generate",
      " */",
      `import { z } from "zod";`,
      "",
      zodCode,
      "",
      `export type ${toPascalCase(name)} = z.infer<typeof ${name}Schema>;`,
      "",
    ].join("\n");

    const outPath = join(OUT_DIR, `${name}.ts`);

    if (existsSync(outPath)) {
      const old = await readFile(outPath, "utf-8");
      if (old === content) {
        console.log(`✅ Unchanged: ${name}.ts`);
        continue;
      }
    }

    if (checkMode) {
      console.error(`❌ DRIFT: ${name}.ts differs from generated version`);
      drift = true;
    } else {
      await writeFile(outPath, content, "utf-8");
      console.log(`📝 ${existsSync(outPath) ? "Updated" : "Created"}: ${name}.ts`);
      written++;
    }
  }

  if (checkMode && drift) {
    console.error("\n❌ Zod drift detected. Run `npm run schemas:generate` to fix.");
    process.exit(1);
  }

  console.log(`\n✅ Generated ${files.length} Zod schemas (${written} changed)`);
}

function toPascalCase(s) {
  return s
    .split(/[-_]/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join("");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
