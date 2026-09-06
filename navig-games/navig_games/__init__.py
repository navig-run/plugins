"""navig-games — proactively find & auto-claim FREE games across storefronts.

Phase 1 ships Epic end-to-end (source → claim → notify → schedule); GOG, Amazon
Prime and the Steam suite follow. Surfaced as ``navig games``.

FREE-ONLY invariant: the claim engine only ever completes a $0 checkout. A title
with a real price is always skipped — the plugin never spends real money.
"""

__version__ = "0.9.8"
