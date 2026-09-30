import type { MenuPlugin } from "../types.js";
import { example } from "./example.js";
import { store } from "./store/index.js";
import { ports } from "./ports.js";
import { devhost } from "./devhost.js";
import { wrangler } from "./wrangler/index.js";
import { github } from "./github/index.js";
import { envDoctor } from "./env-doctor/index.js";

/** Bundled, first-party plugins — statically imported so they work even in the compiled binary. */
export const BUILTIN_PLUGINS: MenuPlugin[] = [example, store, ports, devhost, wrangler, github, envDoctor];
