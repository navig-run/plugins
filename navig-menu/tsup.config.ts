import { defineConfig } from "tsup";

// `cli` doubles as the `navig-menu` bin (the shebang banner lets dist/cli.js run via npx).
// `plugin` + `menu-builder` are the public authoring APIs (`navig-menu/plugin` etc.),
// emitted with types. Node strips the hashbang from imported modules, so the shared banner is safe.
export default defineConfig({
  entry: {
    cli: "src/cli.ts",
    plugin: "src/plugins/types.ts",
    "menu-builder": "src/ui/menu-builder.ts",
    model: "src/model.ts",
  },
  format: ["esm"],
  target: "node18",
  platform: "node",
  clean: true,
  sourcemap: true,
  dts: {
    entry: {
      plugin: "src/plugins/types.ts",
      "menu-builder": "src/ui/menu-builder.ts",
      model: "src/model.ts",
    },
  },
  splitting: false,
  shims: true,
  banner: { js: "#!/usr/bin/env node" },
});
