export default {
  id: "dyn",
  tier: "programmatic",
  detect: () => true,
  contribute: () => ({ bannerPhrases: ["hello from mjs"], about: ["dyn plugin"] }),
  handlers: {},
};
