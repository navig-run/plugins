"""navig ebay — sell on eBay via the official Sell APIs, wired natively into navig.

The eBay engine (`navig_ebay.engine`) runs in-process. Secrets live in the navig
vault; non-secret settings in an atomic YAML under the navig config dir. Every
API call resolves (and refreshes) the OAuth token through `EbayAuth`.

Token resolution: EBAY_ACCESS_TOKEN env → navig vault (refresh on expiry).
"""

from __future__ import annotations

import secrets as _secrets
import webbrowser
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import parse_qs, unquote, urlparse

import typer

from navig_ebay.engine import (
    images,
    inventory,
    item_schema,
    locations,
    offers,
    offers_negotiation,
    orders,
    policies,
    pricing,
)
from navig_ebay.engine.api import EbayClient
from navig_ebay.engine.config import VALID_ENVIRONMENTS, EbayConfigManager
from navig_ebay.engine.models import EbayError
from navig_ebay.engine.oauth_ebay import EbayAuth, build_authorize_url, exchange_code
from navig_ebay.engine.rich_utils import err, ok, out, table, warn

ebay_app = typer.Typer(
    name="ebay",
    help="🛒 eBay: draft · publish · price · orders (official Sell APIs, vault-wired)",
    no_args_is_help=True,
)

auth_app = typer.Typer(name="auth", help="Authenticate with eBay (OAuth).", no_args_is_help=True)
creds_app = typer.Typer(name="creds", help="Manage eBay app credentials.", no_args_is_help=True)
policies_app = typer.Typer(name="policies", help="Business policies.", no_args_is_help=True)
location_app = typer.Typer(name="location", help="Merchant inventory locations.", no_args_is_help=True)

ebay_app.add_typer(auth_app)
ebay_app.add_typer(creds_app)
ebay_app.add_typer(policies_app)
ebay_app.add_typer(location_app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _auth() -> EbayAuth:
    return EbayAuth()


def _client(token_kind: str = "user") -> EbayClient:
    return EbayClient(_auth(), token_kind=token_kind)


def _fail(message: str) -> None:
    err(message)
    raise typer.Exit(1)


def _extract_code(pasted: str) -> str:
    """Accept either a raw auth code or the full redirected URL and return the code."""
    pasted = pasted.strip()
    if "code=" in pasted and ("://" in pasted or pasted.startswith("?")):
        query = urlparse(pasted).query or pasted.lstrip("?")
        codes = parse_qs(query).get("code")
        if codes:
            return unquote(codes[0])
    return unquote(pasted)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
@ebay_app.command("env")
def env_cmd(
    environment: Annotated[Optional[str], typer.Argument(help="sandbox | production")] = None,
) -> None:
    """Show or set the active eBay environment (sandbox is the safe default)."""
    mgr = EbayConfigManager()
    if environment is None:
        cfg = mgr.load()
        out(f"environment: [cyan]{cfg.environment}[/cyan]  ·  marketplace: {cfg.marketplace_id}")
        return
    environment = environment.lower()
    if environment not in VALID_ENVIRONMENTS:
        _fail(f"environment must be one of {list(VALID_ENVIRONMENTS)}")
    if environment == "production":
        warn("Switching to PRODUCTION — listings and offers will be REAL.")
        if not typer.confirm("Continue?", default=False):
            raise typer.Abort()
    mgr.update(environment=environment)
    ok(f"environment set to {environment}")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
@creds_app.command("set")
def creds_set(
    client_id: Annotated[Optional[str], typer.Option("--client-id", help="eBay app Client ID")] = None,
    client_secret: Annotated[Optional[str], typer.Option("--client-secret", help="eBay app Client Secret")] = None,
    ru_name: Annotated[Optional[str], typer.Option("--ru-name", help="Registered RuName (redirect)")] = None,
) -> None:
    """Store the eBay keyset (Client ID / Secret / RuName) in the navig vault."""
    client_id = client_id or typer.prompt("Client ID")
    client_secret = client_secret or typer.prompt("Client Secret", hide_input=True)
    ru_name = ru_name or typer.prompt("RuName (redirect URL name)")
    try:
        _auth().store_app_credentials(client_id.strip(), client_secret.strip(), ru_name.strip())
    except EbayError as exc:
        _fail(str(exc))
    ok("eBay app credentials stored in the vault. Next: `navig ebay auth login`.")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@auth_app.command("login")
def auth_login(
    code: Annotated[Optional[str], typer.Option("--code", help="Paste the auth code/redirect URL non-interactively")] = None,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Print the URL instead of opening a browser")] = False,
) -> None:
    """Run the eBay OAuth consent flow and store user tokens in the vault."""
    auth = _auth()
    try:
        client_id, client_secret, ru = auth.load_app_credentials()
    except EbayError as exc:
        _fail(f"{exc}")

    state = _secrets.token_urlsafe(16)
    url = build_authorize_url(
        client_id=client_id, ru_name=ru, environment=auth.config.environment, state=state
    )
    out("Open this URL, sign in, and approve access:")
    out(f"[cyan]{url}[/cyan]")
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    out("")
    out("After approving, eBay redirects to your accepted URL with a `code=...` parameter.")
    if code is None:
        code = typer.prompt("Paste the full redirected URL (or just the code)")
    auth_code = _extract_code(code)
    if not auth_code:
        _fail("no authorization code provided")
    try:
        tokens = exchange_code(
            environment=auth.config.environment,
            client_id=client_id,
            client_secret=client_secret,
            code=auth_code,
            ru_name=ru,
        )
        auth.store_tokens(tokens)
    except EbayError as exc:
        _fail(f"token exchange failed: {exc}")
    ok(f"authenticated ({auth.config.environment}). Access token stored; refresh token saved.")


@auth_app.command("status")
def auth_status() -> None:
    """Show connection state, environment, and token expiry."""
    auth = _auth()
    cfg = auth.config
    rows = [
        ("environment", cfg.environment),
        ("marketplace", cfg.marketplace_id),
        ("app credentials", "yes" if auth.has_app_credentials() else "MISSING"),
    ]
    tokens = None
    try:
        tokens = auth.load_tokens()
    except EbayError:
        pass
    if tokens is None:
        rows.append(("connection", "NOT authenticated — run `navig ebay auth login`"))
    else:
        state = "expired (will refresh)" if tokens.is_expired else "valid"
        rows.append(("connection", "connected"))
        rows.append(("access token", state))
        rows.append(("refresh token", "present" if tokens.refresh else "MISSING"))
    table("eBay auth", ["field", "value"], rows)


@auth_app.command("logout")
def auth_logout() -> None:
    """Remove stored eBay OAuth tokens (app credentials are kept)."""
    if _auth().logout():
        ok("eBay tokens removed.")
    else:
        warn("no eBay tokens were stored.")


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
@location_app.command("add")
def location_add(
    key: Annotated[str, typer.Option("--key", help="Location key, e.g. home-1")] = "home-1",
    country: Annotated[str, typer.Option("--country")] = "US",
    postal_code: Annotated[Optional[str], typer.Option("--postal")] = None,
    city: Annotated[Optional[str], typer.Option("--city")] = None,
    state: Annotated[Optional[str], typer.Option("--state")] = None,
    address: Annotated[Optional[str], typer.Option("--address", help="Street address line 1")] = None,
    make_default: Annotated[bool, typer.Option("--default/--no-default", help="Use as default location")] = True,
) -> None:
    """Create (or ensure) a merchant inventory location — required to publish."""
    try:
        client = _client()
        locations.create(
            client, key, country=country, postal_code=postal_code, city=city,
            state=state, address_line1=address,
        )
    except EbayError as exc:
        _fail(str(exc))
    if make_default:
        EbayConfigManager().update(default_location_key=key)
    ok(f"location '{key}' ready{' (set as default)' if make_default else ''}.")


@location_app.command("list")
def location_list() -> None:
    """List merchant inventory locations."""
    try:
        locs = locations.list_locations(_client())
    except EbayError as exc:
        _fail(str(exc))
    if not locs:
        out("no locations yet — add one with `navig ebay location add`.")
        return
    table(
        "Locations",
        ["key", "name", "status"],
        [(loc.get("merchantLocationKey"), loc.get("name"), loc.get("merchantLocationStatus")) for loc in locs],
    )


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------
@policies_app.command("sync")
def policies_sync(
    set_defaults: Annotated[bool, typer.Option("--set-defaults/--no-set-defaults", help="Persist the first of each as default")] = True,
) -> None:
    """Fetch payment/return/fulfillment business policies and optionally set defaults."""
    try:
        client = _client()
        allp = policies.fetch_all(client)
    except EbayError as exc:
        _fail(str(exc))

    updates: dict[str, str] = {}
    for kind in ("payment", "return", "fulfillment"):
        rows = []
        id_key = policies.id_key(kind)
        for p in allp[kind]:
            rows.append((p.get(id_key), p.get("name"), p.get("description", "")[:40]))
        if rows:
            table(f"{kind.title()} policies", ["id", "name", "description"], rows)
        else:
            warn(f"no {kind} policies found — create one in eBay account settings (business policies).")
        if set_defaults and allp[kind]:
            first_id = allp[kind][0].get(id_key)
            if first_id:
                updates[f"default_{kind}_policy_id"] = str(first_id)

    if updates:
        EbayConfigManager().update(**updates)
        ok(f"defaults set: {', '.join(sorted(updates))}")


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------
@ebay_app.command("doctor")
def doctor() -> None:
    """Verify prerequisites before publishing (creds · token · location · policies)."""
    auth = _auth()
    cfg = auth.config
    checks: list[tuple[str, bool, str]] = []

    has_creds = auth.has_app_credentials()
    checks.append(("app credentials", has_creds, "run `navig ebay creds set`"))

    connected = False
    try:
        connected = auth.is_connected()
    except EbayError:
        connected = False
    checks.append(("authenticated", connected, "run `navig ebay auth login`"))

    has_location = bool(cfg.default_location_key)
    checks.append(("default location", has_location, "run `navig ebay location add`"))

    has_policies = all([
        cfg.default_payment_policy_id,
        cfg.default_return_policy_id,
        cfg.default_fulfillment_policy_id,
    ])
    checks.append(("business policies", has_policies, "run `navig ebay policies sync` (create them on eBay first)"))

    rows = []
    for name, passed, hint in checks:
        status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        rows.append((name, status, "" if passed else hint))
    table(f"eBay readiness ({cfg.environment})", ["check", "status", "fix"], rows)

    if all(p for _n, p, _h in checks):
        ok("all prerequisites met — you can publish.")
    else:
        warn("some prerequisites are missing (see 'fix' column).")
        if cfg.environment == "production":
            out("Note: production keys also require an eBay Marketplace Account "
                "Deletion notification endpoint before eBay grants access.")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def _draft_one(auth: EbayAuth, client: EbayClient, path: Path, *, publish: bool = False) -> dict:
    spec = item_schema.load_item_file(path)
    errors = item_schema.validate(spec)
    if errors:
        raise EbayError(f"{path.name} invalid: " + "; ".join(errors))
    for w in item_schema.restriction_warnings(spec):
        warn(w)

    hosted = images.ensure_hosted_urls(auth, list(spec.get("images") or []))
    item_payload = item_schema.to_inventory_item_payload(spec, image_urls=hosted)
    offer_payload = item_schema.to_offer_payload(spec, auth.config)

    sku = str(spec["sku"])
    inventory.create_or_replace(client, sku, item_payload)

    existing = offers.get_for_sku(client, sku)
    if existing:
        offer_id = str(existing[0].get("offerId"))
        offers.update(client, offer_id, offer_payload)
    else:
        offer_id = offers.create(client, offer_payload)

    result = {"sku": sku, "offerId": offer_id, "listingId": None}
    if publish:
        result["listingId"] = offers.publish(client, offer_id)
    return result


@ebay_app.command("draft")
def draft(
    item_file: Annotated[Path, typer.Argument(help="Path to an item YAML file")],
    publish_now: Annotated[bool, typer.Option("--publish", help="Publish immediately after drafting")] = False,
) -> None:
    """Create/replace an inventory item and its (unpublished) offer from a YAML file."""
    auth = _auth()
    try:
        res = _draft_one(auth, _client(), item_file, publish=publish_now)
    except EbayError as exc:
        _fail(str(exc))
    ok(f"drafted SKU '{res['sku']}' — offerId {res['offerId']}")
    if res["listingId"]:
        ok(f"published — listingId {res['listingId']}")
    else:
        out(f"next: `navig ebay publish {res['sku']}`")


@ebay_app.command("publish")
def publish(
    sku: Annotated[str, typer.Argument(help="SKU to publish")],
    offer_id: Annotated[Optional[str], typer.Option("--offer-id", help="Publish a specific offerId")] = None,
) -> None:
    """Publish an offer → live listing."""
    try:
        client = _client()
        if offer_id is None:
            existing = offers.get_for_sku(client, sku)
            if not existing:
                _fail(f"no offer found for SKU '{sku}' — run `navig ebay draft` first")
            offer_id = str(existing[0].get("offerId"))
        listing_id = offers.publish(client, offer_id)
    except EbayError as exc:
        _fail(str(exc))
    ok(f"published SKU '{sku}' — listingId {listing_id}")


@ebay_app.command("list")
def list_items(
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> None:
    """List your inventory items."""
    try:
        resp = inventory.list_items(_client(), limit=limit)
    except EbayError as exc:
        _fail(str(exc))
    items = resp.get("inventoryItems", []) if isinstance(resp, dict) else []
    if not items:
        out("no inventory items yet — create one with `navig ebay draft <file>`.")
        return
    rows = []
    for it in items:
        product = it.get("product", {})
        qty = (it.get("availability", {}).get("shipToLocationAvailability", {}) or {}).get("quantity")
        rows.append((it.get("sku"), (product.get("title") or "")[:48], qty, it.get("condition")))
    table("Inventory items", ["sku", "title", "qty", "condition"], rows)


@ebay_app.command("show")
def show(sku: Annotated[str, typer.Argument(help="SKU to show")]) -> None:
    """Show an inventory item and its offers."""
    try:
        client = _client()
        item = inventory.get(client, sku)
        offs = offers.get_for_sku(client, sku)
    except EbayError as exc:
        _fail(str(exc))
    product = item.get("product", {})
    out(f"[bold]{sku}[/bold] — {product.get('title', '')}")
    out(f"condition: {item.get('condition')}")
    if offs:
        rows = [(o.get("offerId"), o.get("status"), o.get("listingId"),
                 (o.get("pricingSummary", {}).get("price", {}) or {}).get("value")) for o in offs]
        table("Offers", ["offerId", "status", "listingId", "price"], rows)
    else:
        out("no offers for this SKU yet.")


@ebay_app.command("edit")
def edit(
    sku: Annotated[str, typer.Argument(help="SKU to edit")],
    price: Annotated[Optional[float], typer.Option("--price")] = None,
    quantity: Annotated[Optional[int], typer.Option("--quantity")] = None,
) -> None:
    """Quick-edit price and/or quantity of an existing SKU's offer."""
    if price is None and quantity is None:
        _fail("nothing to change — pass --price and/or --quantity")
    try:
        client = _client()
        offs = offers.get_for_sku(client, sku)
        if not offs:
            _fail(f"no offer found for SKU '{sku}'")
        offer = offs[0]
        offer_id = str(offer.get("offerId"))
        if price is not None:
            offer.setdefault("pricingSummary", {}).setdefault("price", {})
            offer["pricingSummary"]["price"]["value"] = f"{price:.2f}"
        if quantity is not None:
            offer["availableQuantity"] = quantity
        # Strip read-only fields eBay rejects on update.
        for ro in ("offerId", "listingId", "status", "listing"):
            offer.pop(ro, None)
        offers.update(client, offer_id, offer)
    except EbayError as exc:
        _fail(str(exc))
    ok(f"updated SKU '{sku}'.")


@ebay_app.command("end")
def end(target: Annotated[str, typer.Argument(help="SKU (or --offer-id) to end")],
        offer_id: Annotated[Optional[str], typer.Option("--offer-id")] = None) -> None:
    """End a live listing (withdraw the offer)."""
    try:
        client = _client()
        if offer_id is None:
            offs = offers.get_for_sku(client, target)
            if not offs:
                _fail(f"no offer found for SKU '{target}'")
            offer_id = str(offs[0].get("offerId"))
        listing_id = offers.withdraw(client, offer_id)
    except EbayError as exc:
        _fail(str(exc))
    ok(f"ended listing {listing_id or '(offer withdrawn)'}.")


@ebay_app.command("relist")
def relist(sku: Annotated[str, typer.Argument(help="SKU to relist (re-publish its offer)")]) -> None:
    """Relist an ended item by republishing its offer."""
    try:
        client = _client()
        offs = offers.get_for_sku(client, sku)
        if not offs:
            _fail(f"no offer found for SKU '{sku}' — draft it first")
        listing_id = offers.publish(client, str(offs[0].get("offerId")))
    except EbayError as exc:
        _fail(str(exc))
    ok(f"relisted SKU '{sku}' — listingId {listing_id}")


@ebay_app.command("bulk")
def bulk(
    folder: Annotated[Path, typer.Argument(help="Folder of item *.yaml files")],
    publish_now: Annotated[bool, typer.Option("--publish", help="Publish each after drafting")] = False,
) -> None:
    """Draft (and optionally publish) every item YAML in a folder."""
    if not folder.is_dir():
        _fail(f"not a folder: {folder}")
    files = sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")])
    if not files:
        _fail(f"no *.yaml files in {folder}")
    auth = _auth()
    client = _client()
    done, failed = 0, 0
    for f in files:
        try:
            res = _draft_one(auth, client, f, publish=publish_now)
            done += 1
            suffix = f" → listingId {res['listingId']}" if res["listingId"] else ""
            ok(f"{f.name}: SKU {res['sku']} offer {res['offerId']}{suffix}")
        except EbayError as exc:
            failed += 1
            err(f"{f.name}: {exc}")
    out("")
    out(f"bulk complete: {done} ok, {failed} failed, of {len(files)} files.")


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
@ebay_app.command("price")
def price(
    query: Annotated[str, typer.Argument(help="What to price, e.g. \"Flipper Zero\"")],
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> None:
    """Active-listing price proxy via the Browse API (asking prices, NOT sold)."""
    try:
        client = _client(token_kind="app")
        items = pricing.search_active(client, query, limit=limit)
    except EbayError as exc:
        _fail(str(exc))
    stats = pricing.price_stats(items)
    if not stats["count"]:
        out(f"no active listings matched '{query}'.")
        return
    cur = stats["currency"]
    table(
        f"'{query}' — {stats['count']} active listings",
        ["low", "median", "high"],
        [(f"{cur} {stats['low']:.2f}", f"{cur} {stats['median']:.2f}", f"{cur} {stats['high']:.2f}")],
    )
    warn("These are ACTIVE ASKING prices, not sold prices. Real sold-comps need "
         "eBay's approval-gated Marketplace Insights API.")


# ---------------------------------------------------------------------------
# Orders / fulfilment
# ---------------------------------------------------------------------------
@ebay_app.command("orders")
def orders_cmd(limit: Annotated[int, typer.Option("--limit")] = 50) -> None:
    """List your recent orders."""
    try:
        ords = orders.list_orders(_client(), limit=limit)
    except EbayError as exc:
        _fail(str(exc))
    if not ords:
        out("no orders.")
        return
    rows = []
    for o in ords:
        total = (o.get("pricingSummary", {}).get("total", {}) or {})
        rows.append((
            o.get("orderId"),
            o.get("orderFulfillmentStatus"),
            f"{total.get('currency', '')} {total.get('value', '')}",
            o.get("creationDate", "")[:10],
        ))
    table("Orders", ["orderId", "status", "total", "created"], rows)


@ebay_app.command("ship")
def ship(
    order_id: Annotated[str, typer.Argument(help="Order ID to mark shipped")],
    tracking: Annotated[str, typer.Option("--tracking", help="Tracking number")],
    carrier: Annotated[str, typer.Option("--carrier", help="Carrier code, e.g. USPS, FEDEX, UPS")],
) -> None:
    """Mark an order shipped with tracking (creates a shipping fulfillment)."""
    try:
        fid = orders.create_shipping_fulfillment(
            _client(), order_id, tracking_number=tracking, carrier=carrier
        )
    except EbayError as exc:
        _fail(str(exc))
    ok(f"order {order_id} marked shipped ({carrier} {tracking}){f' — fulfillment {fid}' if fid else ''}.")


@ebay_app.command("offers")
def offers_cmd(limit: Annotated[int, typer.Option("--limit")] = 20) -> None:
    """Show listings eligible for a seller-initiated 'Offer to buyers'."""
    try:
        eligible = offers_negotiation.find_eligible_items(_client(), limit=limit)
    except EbayError as exc:
        _fail(str(exc))
    if not eligible:
        out("no listings currently eligible for a seller offer.")
        return
    rows = [(e.get("listingId"), (e.get("title") or "")[:50]) for e in eligible]
    table("Best-offer eligible", ["listingId", "title"], rows)


def main() -> None:  # console-script convenience
    ebay_app()
