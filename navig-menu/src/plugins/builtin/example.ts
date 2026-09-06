import { definePlugin } from "../types.js";

/**
 * The canonical example plugin — inactive by default (enable it from the Plugins manager). It shows
 * every extension point a plugin has: a banner phrase, a settings toggle that changes the tagline,
 * a section with a delegated/command action + an internal handler, and an About line. Copy this into
 * `.navig/plugins/example.mjs` (drop the imports; export a `default` object) to start your own.
 */
export const example = definePlugin({
  id: "example",
  tier: "programmatic",
  version: "1.0.0",
  detect: () => false, // never auto-activates — turn it on in the Plugins manager (key `p`)
  contribute: (ctx) => ({
    // Overwrite text — only when the toggle is on (settings are resolved before contribute runs).
    text: ctx.settings.loud ? { tagline: "extended by plugins" } : {},
    bannerPhrases: ["hello from a plugin"],
    settings: [{ key: "loud", label: "Example · loud tagline", type: "toggle", default: false }],
    sections: [
      {
        group: "Example",
        meta: { title: "EXAMPLE", emoji: "🧪", unicode: "◆", ascii: "e", tone: "cyan" },
        actions: [
          { id: "example.hello", label: "Say hello", internal: "hello", description: "prints a greeting (internal handler)" },
          { id: "example.node", label: "Node version", cmd: "node --version", description: "runs `node --version`" },
        ],
      },
    ],
    about: ["example plugin — the canonical template (docs/plugins.md)"],
  }),
  handlers: {
    hello: (a) => a.notify("hello from the example plugin"),
  },
});
