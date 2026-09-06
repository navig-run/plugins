# navig-ebay

Sell on eBay from navig, using the **official eBay Sell APIs** — not browser
automation (which fights eBay's bot detection and risks your account). A
first-party navig plugin mounted as `navig ebay`.

```
navig ebay draft ./items/flipper.yaml   # inventory item + offer (draft)
navig ebay publish flipper-01           # → live listing
navig ebay price "Flipper Zero"         # active asking prices (not sold)
navig ebay orders                       # your orders
```

## One-time setup (eBay side — only you can do this)

1. Create an **eBay Developer account** and an application **keyset** (Client ID +
   Client Secret) at <https://developer.ebay.com>. Make a **sandbox** keyset first.
2. Create a **RuName** (redirect URL name) for the keyset; note its value.
3. Create **business policies** (payment, return, fulfillment) and opt in — the
   Offer API refuses to publish without them.
4. Production keys additionally require an eBay **Marketplace Account Deletion
   notification endpoint** before eBay grants production access (sandbox does not).

`navig ebay doctor` tells you which prerequisites are still missing.

## Auth

```
navig ebay env sandbox                  # sandbox (default) or production
navig ebay creds set                    # store Client ID / Secret / RuName in the vault
navig ebay auth login                    # opens eBay consent; paste the returned code
navig ebay auth status                   # connection + token expiry + marketplace
```

Secrets live in the encrypted navig vault; non-secret settings in
`~/.navig/config/navig-ebay/config.yaml`.

> **eBay OAuth quirk:** eBay requires an HTTPS redirect registered as a RuName,
> so login is browser-consent → paste-the-code (not a localhost callback). The
> access token lasts ~2h and is auto-refreshed from the ~18-month refresh token.

## Item file

A listing is described by a small YAML file:

```yaml
sku: flipper-01
title: "Flipper Zero Portable Multi-Tool Development Board — Boxed"   # <= 80 chars
condition: NEW                       # eBay condition enum
category_id: "175673"                # eBay leaf category id
description: |
  <p>Boxed Flipper Zero multi-tool development board. Tested working.</p>
aspects:
  Brand: ["Flipper Devices"]
  Type: ["Development Board"]
images:                              # local paths (EPS-uploaded) or https URLs
  - ./photos/flipper-1.jpg
price: 149.00
currency: USD
quantity: 1
format: FIXED_PRICE                  # or AUCTION
best_offer: true
# policy / location default from config if omitted
```

`navig ebay draft <file>` validates it, uploads local photos to eBay Picture
Service, upserts the inventory item, and creates a (still unpublished) offer.
`navig ebay publish <sku>` makes it live.

## Command reference

| Group | Commands |
|---|---|
| Auth / setup | `creds set` · `env` · `auth login/status/logout` · `location add` · `policies sync` · `doctor` |
| Listing | `draft` · `publish` · `list` · `show` · `edit` · `end` · `relist` · `bulk` |
| Pricing | `price "<query>"` |
| Orders | `orders` · `ship` · `offers` |

## Notes & limits

- **Pricing shows *asking* prices, not sold prices.** eBay's sold-comps data
  (Marketplace Insights API) is approval-gated; the freely-available Browse API
  only exposes active listings. Output is labelled accordingly.
- **Flipper Zero** is periodically restricted on eBay. Use neutral, hobby/dev
  wording; `publish` warns on restriction-prone titles.
- Prove everything in **sandbox** before switching to `env production`.
