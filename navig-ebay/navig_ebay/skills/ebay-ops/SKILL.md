---
name: ebay-ops
description: Sell on eBay — create, price, publish, and manage listings and orders via the official eBay Sell APIs. Use when the user wants to list an item for sale on eBay, publish or end a listing, check active-listing prices, or view/ship eBay orders.
activation_keywords: [ebay, sell on ebay, list on ebay, ebay listing, ebay order]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
  envRequirements:
    - ebay
---

# eBay Selling Operations

This capability is provided by the **navig-ebay** plugin. Use `navig ebay
<command>` for all eBay work — the group authenticates automatically from the
vault (OAuth tokens are refreshed on expiry).

- Run `navig ebay --help` to see the full surface (draft, publish, price, list,
  orders, and more).

## Golden path (a new user)

1. `navig ebay env sandbox` — stay in sandbox until everything works.
2. `navig ebay creds set` — store the eBay keyset (Client ID / Secret / RuName).
3. `navig ebay auth login` — browser consent, then paste the returned code.
4. `navig ebay location add` and `navig ebay policies sync` — publish prerequisites.
5. `navig ebay doctor` — confirm all checks PASS before publishing.
6. `navig ebay draft item.yaml` → `navig ebay publish <sku>`.

## Rules & cautions

- **Never** drive eBay through browser automation (CDP) for listing — it risks the
  account. This plugin uses the sanctioned Sell APIs only.
- **Pricing is asking-price, not sold-price.** `navig ebay price` uses the Browse
  API (active listings). Always relay that caveat; do not present it as sold comps.
- **Restriction-prone items** (e.g. Flipper Zero): use neutral hobby/dev wording.
  `draft`/`publish` warn on risky titles — heed the warning before publishing.
- **Confirm before publishing to production.** `env production` is gated; a
  published offer is a real, public listing. Prefer sandbox for any test.
- An item is authored as a small YAML file (sku, title, condition, category_id,
  description, aspects, images, price). Local image paths are auto-uploaded to
  eBay Picture Service; https URLs pass through.
