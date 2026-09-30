import { defineConfig } from "vitest/config";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";

/**
 * Source uses NodeNext-style `./x.js` import specifiers (correct for the ESM build). Vite's
 * resolver doesn't rewrite `.js`→`.ts` by default, so this tiny plugin does it for tests only.
 */
export default defineConfig({
  plugins: [
    {
      name: "js-to-ts",
      enforce: "pre",
      resolveId(source, importer) {
        if (importer && source.startsWith(".") && source.endsWith(".js")) {
          const candidate = resolve(dirname(importer), source.replace(/\.js$/, ".ts"));
          if (existsSync(candidate)) return candidate;
        }
        return null;
      },
    },
  ],
  test: {
    include: ["test/**/*.test.ts"],
  },
});
