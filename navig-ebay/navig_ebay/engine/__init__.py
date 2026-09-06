"""In-process eBay engine for navig-ebay.

Modules:
  config       — non-secret settings (environment, marketplace, RuName, defaults)
  oauth_ebay   — eBay OAuth (user token exchange/refresh, app token) + vault storage
  api          — authenticated REST client for the Sell/Browse APIs
  inventory    — Inventory API (inventory items / SKUs)
  offers       — Offer API (create / publish / withdraw offers)
  locations    — Location API (merchant inventory locations)
  policies     — Account API (payment / return / fulfillment business policies)
  item_schema  — the item YAML <-> eBay payload mapping + validation
  images       — EPS image upload (local file -> eBay-hosted URL)
  pricing      — Browse API active-listing price proxy
  orders       — Fulfillment API (orders + shipping fulfillment)
  offers_negotiation — Negotiation API (best-offer eligibility)
"""

from __future__ import annotations
