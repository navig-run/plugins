import { resolve } from "node:path";
import { TOOL_NAME, TOOL_VERSION } from "./config/constants.js";
import {
  cmdMenu,
  cmdScan,
  cmdList,
  cmdBuild,
  cmdGenerate,
  cmdRun,
  cmdDoctor,
  cmdSetup,
  cmdImport,
  cmdOrganize,
  type GlobalOpts,
} from "./commands/index.js";

const HELP = `${TOOL_NAME} v${TOOL_VERSION} — AI-first project menu builder

Usage:
  navig-menu                 open the menu (auto-builds on first run)
  navig-menu build [--ai]    (re)generate .navig/menu.json from detection
  navig-menu generate        create a pro .navig/menu.json contract
  navig-menu list            print a static menu preview (ls/preview aliases)
  navig-menu scan            detect + report (refresh cache), no UI
  navig-menu setup           guided config (offers the \`menu\` npm script)
  navig-menu doctor [--fix]  diagnose environment + detection (--fix repairs, with a backup)
  navig-menu import <file>   import a curated command catalog into .navig/menu.json (--write)
  navig-menu organize        AI: describe every command + report gaps (--write, needs a key)
  navig-menu run <action>    run a canonical action (dev/build/test/…) or script id
  navig-menu dev | test      shortcuts for \`run dev\` / \`run test\`

Flags:
  --cwd <path>   operate on a directory (default: cwd)
  --json         emit the machine manifest (no UI)
  --deep         full recursive scan (monorepo packages, nested workspaces)
  --plain        ASCII, no color (also respects NO_COLOR / non-TTY)
  --yes          skip the \`confirm\` tier (never the typed \`dangerous\` prompt)
  --relay        emit a JSON action-request for NAVIG instead of running
  --host <name>  target host (with --relay)
  --ai           AI assist: enrich a build, or diagnose a failed run (needs an
                 ANTHROPIC_API_KEY/OPENAI_API_KEY; falls back to a paste-ready prompt)
  --no-cache     ignore the cached manifest
  -v, --version  print version
  -h, --help     this help
`;

interface Parsed {
  command: string;
  positionals: string[];
  opts: GlobalOpts;
}

function parse(argv: string[]): Parsed {
  const positionals: string[] = [];
  let cwd = process.cwd();
  let json = false,
    deep = false,
    plain = false,
    yes = false,
    relay = false,
    ai = false,
    noCache = false,
    write = false,
    fix = false;
  let host: string | undefined;

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]!;
    switch (a) {
      case "--cwd":
        cwd = resolve(argv[++i] ?? ".");
        break;
      case "--host":
        host = argv[++i];
        break;
      case "--json":
        json = true;
        break;
      case "--deep":
        deep = true;
        break;
      case "--plain":
        plain = true;
        break;
      case "--yes":
      case "-y":
        yes = true;
        break;
      case "--relay":
        relay = true;
        break;
      case "--no-relay":
        relay = false;
        break;
      case "--ai":
        ai = true;
        break;
      case "--write":
        write = true;
        break;
      case "--fix":
        fix = true;
        break;
      case "--no-cache":
        noCache = true;
        break;
      default:
        if (!a.startsWith("-")) positionals.push(a);
        // unknown flags are ignored (forward-compatible)
        break;
    }
  }

  const command = positionals.shift() ?? "menu";
  return { command, positionals, opts: { cwd, json, deep, plain, yes, relay, host, ai, noCache, write, fix } };
}

async function main(): Promise<number> {
  const argv = process.argv.slice(2);
  if (argv.includes("-h") || argv.includes("--help")) {
    process.stdout.write(HELP);
    return 0;
  }
  if (argv.includes("-v") || argv.includes("--version")) {
    process.stdout.write(TOOL_VERSION + "\n");
    return 0;
  }

  const { command, positionals, opts } = parse(argv);

  switch (command) {
    case "menu":
      return cmdMenu(opts);
    case "scan":
      return cmdScan(opts);
    case "list":
    case "ls":
    case "preview":
      return cmdList(opts);
    case "build":
      return cmdBuild(opts);
    case "generate":
    case "gen":
    case "create":
      return cmdGenerate(opts);
    case "setup":
      return cmdSetup(opts);
    case "doctor":
      return cmdDoctor(opts);
    case "import":
      return cmdImport(opts, positionals[0]);
    case "organize":
      return cmdOrganize(opts);
    case "run":
      return cmdRun(opts, positionals[0] ?? "dev");
    case "dev":
    case "test":
      return cmdRun(opts, command);
    default:
      process.stderr.write(`Unknown command "${command}".\n\n${HELP}`);
      return 2;
  }
}

/**
 * Graceful exit. Forcing process.exit() right after async work races libuv handle teardown on
 * Windows ("Assertion failed: !(handle->flags & UV_HANDLE_CLOSING), src\win\async.c") — plain
 * `fetch(...).then(() => process.exit(0))` reproduces it on Node 24. A natural event-loop drain
 * exits cleanly and fast, so: set the exit code, release the TTY (a prompt may have left stdin
 * in raw/keypress mode, which would otherwise pin the loop), and only force-exit via an unref'd
 * watchdog if something still holds the loop after a grace period.
 */
function shutdown(code: number): void {
  process.exitCode = code;
  try {
    if (process.stdin.isTTY) process.stdin.setRawMode?.(false);
    process.stdin.pause();
    process.stdin.unref?.();
  } catch {
    /* stdin may already be closed */
  }
  setTimeout(() => process.exit(code), 2000).unref();
}

main()
  .then(shutdown)
  .catch((err) => {
    process.stderr.write(`navig-menu: ${err?.stack ?? err}\n`);
    shutdown(1);
  });
