# Custom menus — the DSL

Most projects need **zero** configuration: `navig-menu` builds the menu from detection. When you want
a bespoke menu (or an AI authors one), use the data-driven DSL — define a menu in under 30 lines with
no terminal-internals knowledge.

```ts
import { createMenu, action } from "navig-menu/menu-builder";

const model = createMenu({ title: "SCHEMA", purpose: "Civic network · maps · missions", accent: "cyan" })
  .section("Development", [
    action("dev", { label: "Dev server", cmd: "pnpm dev", longRunning: true }),
    action("preview", { label: "Preview build", cmd: "pnpm preview", longRunning: true }),
  ])
  .section("Database", [
    action("migrate", { label: "Run migrations", cmd: "pnpm db:migrate", risk: "confirm" }),
    action("reset", { label: "Reset database", cmd: "pnpm db:reset", risk: "dangerous" }),
  ])
  .section("Deploy", [
    action("deploy", { label: "Deploy to Cloudflare", cmd: "pnpm wrangler deploy", risk: "dangerous" }),
  ])
  .toModel();
```

`createMenu(...).toModel()` returns the same `MenuModel` shape the tool renders internally — use it to
author or transform a menu programmatically. For the everyday path, you don't call this at all: write
the equivalent [`.navig/menu.json`](schema.md) and `navig-menu` renders it for you.

### Rules

- `cmd` is tokenized into an argv array and run **without a shell** (no interpolation).
- `risk` drives confirmation: `safe` runs directly, `confirm` asks once, `dangerous` requires a typed
  confirmation.
- `longRunning: true` streams output and handles Ctrl+C cleanly (dev servers, watchers).
- Sections render in canonical order (`Development`, `Quality`, `Database`, `Build`, `Deploy`,
  `Desktop & Store`, `i18n`, `Assets`, `Operations`, `Misc`); custom / plugin group names append after.

For the persisted, human/AI-editable form, see [`schema.md`](schema.md) (`.navig/menu.json`). To extend
the menu without touching this DSL — add banner lines, sections, or claim scripts — write a plugin
(see [`plugins.md`](plugins.md)).
