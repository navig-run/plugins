"""navig-ebay — sell on eBay via the official Sell APIs, wired into ``navig ebay``.

The eBay engine (``navig_ebay.engine``) is bundled and runs in-process: every
selling verb (draft · publish · price · orders · policies …) is a ``navig ebay``
subcommand. A group callback resolves the eBay OAuth token from the navig vault
(refreshing it when expired) and injects it into ``EBAY_ACCESS_TOKEN`` so the
whole surface is authenticated with zero per-command glue — the same pattern as
navig-github.

Design notes:
  * Sanctioned Sell APIs only — never browser automation against eBay (bot
    detection risk to a good account).
  * Sandbox-first: the environment (sandbox|production) lives in config; publish
    is gated behind ``doctor`` prerequisite checks.
  * Secrets (client id/secret, access+refresh tokens) live in the encrypted
    navig vault; non-secret settings in an atomic YAML under the navig config dir.
"""

from __future__ import annotations

__version__ = "0.1.0"
