"""navig mstore addon -- Microsoft Store **add-on** (in-app product) verbs.

Why this module exists separately from the rest of the plugin: the `msstore` CLI
that `navig mstore publish` wraps has **no add-on support at all**. Add-ons are
only reachable through the DevCenter "Ingestion" REST API
(``https://manage.devcenter.microsoft.com/v1.0/my/inappproducts``), so this is a
direct REST client rather than another child-process wrapper.

Deliberately stdlib-only (``urllib``) -- adding ``requests`` to a publishing
plugin is not worth the dependency surface.

Credentials resolve through the SAME seam as the rest of the plugin
(``NAVIG_MSSTORE_*`` env -> vault ``partner_center``/``azure`` -> ``AZURE_*``),
with one extra, lowest-priority fallback: ``--creds <file>``, a JSON file holding
``{tenantId, clientId, clientSecret}``. That file form is what several app repos
already keep next to their packaging scripts; it is read, never written, never
logged, and never echoed.

DevCenter REST quirks this module encodes (each cost real debugging time):

1. **A payload-less POST still needs a JSON content type.** ``POST .../submissions``
   and ``.../commit`` reject the request on *mediaType* if ``Content-Type`` is
   absent, even though they take no body.
2. **``{}`` is NOT equivalent to an empty body** on submission create -- an empty
   JSON object is validated as a submission *definition* and fails.
3. **Server-owned fields must not be echoed back** on PUT (``id``, ``status``,
   ``statusDetails``, ``fileUploadUrl``).
4. **Clone-then-override beats field enumeration.** Rebuilding a submission body
   by listing the fields you know silently drops the ones you don't, and the API
   reports that only as an opaque "active validation errors which cannot be
   exposed via API". Always GET the draft, mutate it, PUT it back.
5. **A dashboard-created submission is read-only to the API.** If a submission was
   started in the Partner Center UI, every PUT fails with "Ingestion API can only
   update, delete, and commit submissions that are created through the API." The
   only API remedy is delete-and-recreate, which discards whatever a human is
   editing in the browser -- so the tool refuses to do that silently and tells the
   operator to pick a lane. (Same trap that blocks the `msstore` CLI on apps.)
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer

# ── Constants ─────────────────────────────────────────────────────────────────

_AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/token"
_RESOURCE = "https://manage.devcenter.microsoft.com"
_BASE = "https://manage.devcenter.microsoft.com/v1.0/my"

#: Add-on product types the Store accepts.
PRODUCT_TYPES = ("Durable", "Consumable", "UnmanagedConsumable")

#: Accepted `InAppProductContentType` values, established by probing ~70 candidates
#: against the live API -- this list is NOT in the docs, and the value the docs
#: suggest (`NotDownloadableContent`) is rejected outright, as is the `NotSet` that
#: a fresh draft is born with. It is a legacy media taxonomy with no
#: software/feature-unlock category, so a durable app unlock has no truly correct
#: option; `OnlineDataStorage` is the least-wrong default. The field can carry tax
#: implications, so callers should choose deliberately via --content-type.
CONTENT_TYPES = (
    "OnlineDataStorage",
    "BookDownload",
    "MusicDownload",
    "MusicStream",
    "VideoDownload",
    "VideoStream",
)
DEFAULT_CONTENT_TYPE = "OnlineDataStorage"

#: Fields the server owns; echoing them back on PUT is rejected.
_READONLY = ("id", "status", "statusDetails", "fileUploadUrl")


def addon_store_id(product: dict) -> str | None:
    """The add-on's Store ID, across the API's two spellings.

    ``POST /inappproducts`` and ``GET /inappproducts/{id}`` return it as ``id``;
    ``GET /applications/{app}/listinappproducts`` returns it as ``inAppProductId``.
    Reading only one of them is why the create verb first printed ``None``."""
    return product.get("inAppProductId") or product.get("id")


def addon_app_ids(product: dict) -> list[str]:
    """App Store IDs an add-on belongs to, across the API's two shapes.

    Create *accepts* a flat ``applicationIds`` list, but GET *returns* a paged
    ``applications: {value: [{id, resourceLocation}]}`` object."""
    flat = product.get("applicationIds")
    if isinstance(flat, list):
        return [str(x) for x in flat]
    node = product.get("applications") or {}
    return [str(a.get("id")) for a in (node.get("value") or []) if a.get("id")]

addon_app = typer.Typer(
    name="addon",
    help="Create, price, and submit Microsoft Store add-ons (in-app products).",
    no_args_is_help=True,
)


# ── HTTP ──────────────────────────────────────────────────────────────────────


class DevCenterError(RuntimeError):
    """A DevCenter API call failed. Carries the status and the decoded body so the
    caller can surface the API's own (often enum-listing) error text."""

    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}\n{body}")
        self.status = status
        self.body = body
        self.url = url


def _request(
    method: str,
    url: str,
    token: str,
    payload: Any | None = None,
    *,
    always_json_content_type: bool = True,
) -> Any:
    """One DevCenter call. Returns the decoded JSON body (or ``{}`` when empty).

    ``always_json_content_type`` encodes quirk #1: the Ingestion API rejects even a
    payload-less POST when the JSON content type is missing.
    """
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if payload is not None or (always_json_content_type and method in ("POST", "PUT")):
        req.add_header("Content-Type", "application/json; charset=utf-8")

    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:  # noqa: PERF203 - need the body
        body = exc.read().decode("utf-8", errors="replace")
        raise DevCenterError(exc.code, body, url) from None
    return json.loads(raw) if raw else {}


def _token(tenant: str, client: str, secret: str) -> str:
    """Client-credentials token for the DevCenter resource. The secret is used
    here and nowhere else -- never logged, never returned."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client,
            "client_secret": secret,
            "resource": _RESOURCE,
        }
    ).encode("utf-8")
    req = urllib.request.Request(_AUTH_URL.format(tenant=tenant), data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))["access_token"]
    except urllib.error.HTTPError as exc:
        raise DevCenterError(exc.code, exc.read().decode("utf-8", errors="replace"), "token") from None


# ── Credentials ───────────────────────────────────────────────────────────────


def _creds_from_file(path: Path) -> tuple[str, str, str] | None:
    """Read ``{tenantId, clientId, clientSecret}``. Returns None when unusable.

    Accepts the snake_case spellings too, so a vault export drops in unchanged."""
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a bad creds file is just "no creds here"
        return None

    def pick(*names: str) -> str:
        for n in names:
            v = blob.get(n)
            if v:
                return str(v)
        return ""

    tenant = pick("tenantId", "tenant_id")
    client = pick("clientId", "client_id")
    secret = pick("clientSecret", "client_secret")
    return (tenant, client, secret) if all((tenant, client, secret)) else None


def _resolve_creds(creds_file: Path | None) -> tuple[str, str, str]:
    """Plugin seam first (env -> vault -> AZURE_*), then an explicit --creds file.

    The file is LAST so a deliberately-configured vault always wins over a stray
    JSON left in a repo."""
    from navig_msstore import _FIELDS, _resolve_value  # local import: avoid a cycle

    resolved = {name: _resolve_value(name)[0] for name in _FIELDS}
    if all(resolved.values()):
        return (resolved["tenant_id"], resolved["client_id"], resolved["client_secret"])

    if creds_file is not None:
        from_file = _creds_from_file(creds_file)
        if from_file:
            return from_file

    missing = [n for n, v in resolved.items() if not v]
    raise typer.BadParameter(
        "Missing Store credentials: "
        + ", ".join(missing)
        + ".\nSet NAVIG_MSSTORE_* env, store them in the vault "
        "(navig vault set partner_center/client_secret ... --profile connector), "
        "or pass --creds <file.json> with {tenantId, clientId, clientSecret}."
    )


def _client(creds_file: Path | None) -> str:
    tenant, client, secret = _resolve_creds(creds_file)
    return _token(tenant, client, secret)


# ── Submission body helpers ───────────────────────────────────────────────────


def strip_readonly(body: dict) -> dict:
    """Drop server-owned fields (quirk #3). Returns a new dict."""
    return {k: v for k, v in body.items() if k not in _READONLY}


#: Substring the API returns when a submission was started in the Partner Center UI.
_DASHBOARD_OWNED = "created through the API"


def is_dashboard_owned(exc: DevCenterError) -> bool:
    """True when the failure is quirk #5 (submission belongs to the dashboard)."""
    return _DASHBOARD_OWNED in (exc.body or "")


def explain(exc: DevCenterError) -> str:
    """Turn a DevCenter failure into something actionable.

    Only quirk #5 gets special handling: its raw message tells you to delete the
    submission, which -- if a human has it open in Partner Center -- destroys their
    work. The operator needs to choose, so say so instead of just relaying it."""
    if is_dashboard_owned(exc):
        return (
            "This submission was created in the Partner Center dashboard, so the API "
            "cannot modify it.\n"
            "Pick one lane:\n"
            "  - finish it in the dashboard (safe if someone is editing it now), or\n"
            "  - discard it and drive it via the API:\n"
            "      navig mstore addon submission-delete --store-id <id>\n"
            "      navig mstore addon submit --store-id <id> ...\n"
            "Deleting discards whatever is open in the browser -- confirm first."
        )
    try:
        return json.loads(exc.body).get("message") or str(exc)
    except Exception:  # noqa: BLE001
        return str(exc)


def build_sale(
    name: str,
    sale_tier: str,
    start: datetime,
    end: datetime,
) -> dict:
    """A single scheduled sale entry for ``pricing.sales``.

    A sale must have a genuine end date: the app UI renders a strikethrough only
    while a sale is live, so an open-ended "sale" is just a lower base price
    wearing a discount badge."""
    if end <= start:
        raise ValueError("sale end must be after start")
    return {
        "name": name,
        "basePriceId": sale_tier,
        "startDate": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "marketSpecificPricings": {},
    }


def apply_pricing(
    body: dict,
    *,
    base_tier: str,
    sale: dict | None = None,
) -> dict:
    """Clone-then-override the pricing block (quirk #4) -- never rebuild it."""
    out = json.loads(json.dumps(body))
    pricing = out.setdefault("pricing", {})
    pricing["priceId"] = base_tier
    pricing.setdefault("marketSpecificPricings", {})
    pricing["sales"] = [sale] if sale else []
    return out


def apply_listing(body: dict, *, lang: str, title: str, description: str) -> dict:
    """Clone-then-override one language listing, preserving any others."""
    out = json.loads(json.dumps(body))
    listings = out.setdefault("listings", {})
    entry = listings.setdefault(lang, {})
    entry["title"] = title
    entry["description"] = description
    return out


# ── Verbs ─────────────────────────────────────────────────────────────────────

_CREDS_OPT = typer.Option(
    None, "--creds", exists=True, dir_okay=False,
    help="JSON file with {tenantId, clientId, clientSecret} (lowest priority).",
)


@addon_app.command("list")
def addon_list(
    app_id: str = typer.Option(..., "--app-id", "-p", help="The app's Store Product ID (9N...)."),
    creds: Path = _CREDS_OPT,
) -> None:
    """List every add-on defined for an app."""
    tok = _client(creds)
    res = _request("GET", f"{_BASE}/applications/{app_id}/listinappproducts", tok)
    items = res.get("value") or []
    if not items:
        typer.secho("No add-ons exist for this app.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)
    for it in items:
        sid = addon_store_id(it) or "?"
        # listinappproducts returns only the id, so fetch the offer token per add-on.
        # Add-on counts are single digits, so the extra GETs are cheap.
        product_id = it.get("productId")
        if not product_id and sid != "?":
            try:
                product_id = _request("GET", f"{_BASE}/inappproducts/{sid}", tok).get("productId")
            except DevCenterError:
                product_id = None
        typer.echo(f"  {sid:<16} productId={product_id or '?'}")


@addon_app.command("create")
def addon_create(
    app_id: str = typer.Option(..., "--app-id", "-p", help="The app's Store Product ID (9N...)."),
    product_id: str = typer.Option(..., "--product-id", help="In-app offer token, e.g. 'blindspot-pro'."),
    product_type: str = typer.Option("Durable", "--type", help=f"One of: {', '.join(PRODUCT_TYPES)}."),
    creds: Path = _CREDS_OPT,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Create an add-on and print its **Store ID** (what RequestPurchaseAsync needs).

    Creating an add-on is reversible (`navig mstore addon delete`) until a
    submission of it has been published."""
    if product_type not in PRODUCT_TYPES:
        raise typer.BadParameter(f"--type must be one of {', '.join(PRODUCT_TYPES)}")
    if not yes and not typer.confirm(
        f"Create {product_type} add-on '{product_id}' under app {app_id}?", default=False
    ):
        typer.echo("Cancelled.")
        raise typer.Exit(0)

    tok = _client(creds)
    res = _request(
        "POST",
        f"{_BASE}/inappproducts",
        tok,
        {"applicationIds": [app_id], "productId": product_id, "productType": product_type},
    )
    store_id = addon_store_id(res)
    typer.secho(f"Created add-on '{product_id}'", fg=typer.colors.GREEN)
    typer.echo(f"  Store ID (inAppProductId) : {store_id}")
    typer.echo(f"  productId / offer token   : {res.get('productId')}")
    typer.echo(f"  productType               : {res.get('productType')}")
    typer.echo("\nWire the Store ID into the app's purchase call, then resubmit the app.")


@addon_app.command("show")
def addon_show(
    store_id: str = typer.Option(..., "--store-id", "-i", help="The add-on's inAppProductId."),
    creds: Path = _CREDS_OPT,
    raw: bool = typer.Option(False, "--raw", help="Dump the full JSON."),
) -> None:
    """Show an add-on, including its pending/published submission ids."""
    tok = _client(creds)
    res = _request("GET", f"{_BASE}/inappproducts/{store_id}", tok)
    if raw:
        typer.echo(json.dumps(res, indent=2))
        raise typer.Exit(0)
    typer.echo(f"  Store ID      : {addon_store_id(res)}")
    typer.echo(f"  productId     : {res.get('productId')}")
    typer.echo(f"  productType   : {res.get('productType')}")
    typer.echo(f"  applications  : {', '.join(addon_app_ids(res))}")
    for key, label in (
        ("pendingInAppProductSubmission", "pending submission"),
        ("lastPublishedInAppProductSubmission", "published submission"),
    ):
        node = res.get(key)
        typer.echo(f"  {label:21}: {node.get('id') if node else '<none>'}")


@addon_app.command("price-probe")
def addon_price_probe(
    store_id: str = typer.Option(..., "--store-id", "-i", help="The add-on's inAppProductId."),
    creds: Path = _CREDS_OPT,
) -> None:
    """Discover the valid price-tier IDs for this account.

    There is no documented endpoint that lists price tiers, so this deliberately
    PUTs an invalid tier to a **draft** submission and surfaces the API's own
    validation message, which enumerates the accepted values. Nothing is
    committed, so nothing can go live."""
    tok = _client(creds)
    sub = _ensure_draft(tok, store_id)
    # A fresh draft has contentType "NotSet", which fails validation BEFORE pricing is
    # ever reached. Fill the other required fields so the probe tier is the only
    # invalid value and the error we get back is the one we came for.
    body = _seed_required(strip_readonly(sub), title="probe", description="probe")
    body = apply_pricing(body, base_tier="__NAVIG_PROBE__")
    try:
        _request("PUT", f"{_BASE}/inappproducts/{store_id}/submissions/{sub['id']}", tok, body)
        typer.secho("Probe tier was ACCEPTED -- the API is not validating priceId here.", fg=typer.colors.YELLOW)
    except DevCenterError as exc:
        typer.echo(exc.body)


def _seed_required(
    body: dict,
    *,
    title: str,
    description: str,
    lang: str = "en-us",
    content_type: str = DEFAULT_CONTENT_TYPE,
    lifetime: str = "Forever",
    publish_mode: str = "Manual",
) -> dict:
    """Fill the fields a fresh draft leaves unset/invalid.

    A newly created add-on submission comes back with ``contentType: "NotSet"``,
    which the API rejects outright -- so every path that PUTs a draft must replace
    it. See :data:`CONTENT_TYPES` for why the replacement cannot be a genuinely
    correct value for a software unlock."""
    out = json.loads(json.dumps(body))
    if out.get("contentType") in (None, "", "NotSet"):
        out["contentType"] = content_type
    out["lifetime"] = out.get("lifetime") or lifetime
    out["visibility"] = out.get("visibility") or "Public"
    out["targetPublishMode"] = out.get("targetPublishMode") or publish_mode
    return apply_listing(out, lang=lang, title=title, description=description)


def _readback_drift(stored: dict, sent: dict) -> list[str]:
    """Fields that did not survive a PUT, comparing what came back to what was sent.

    Only the fields worth failing a submission over -- a zombie submission drops
    everything, so a narrow check is enough to catch it and avoids false alarms on
    fields the server legitimately normalises."""
    drift = []
    if stored.get("contentType") != sent.get("contentType"):
        drift.append(f"contentType is {stored.get('contentType')!r}")
    sent_price = (sent.get("pricing") or {}).get("priceId")
    got_price = (stored.get("pricing") or {}).get("priceId")
    if sent_price and got_price != sent_price:
        drift.append(f"priceId is {got_price!r}")
    if sent.get("listings") and not stored.get("listings"):
        drift.append("listings are empty")
    return drift


def _ensure_draft(tok: str, store_id: str) -> dict:
    """Return a draft (pending) submission for the add-on, creating one if needed."""
    product = _request("GET", f"{_BASE}/inappproducts/{store_id}", tok)
    pending = product.get("pendingInAppProductSubmission")
    if pending:
        return _request(
            "GET", f"{_BASE}/inappproducts/{store_id}/submissions/{pending['id']}", tok
        )
    # Quirk #1/#2: no body at all, but the JSON content type must still be present.
    return _request("POST", f"{_BASE}/inappproducts/{store_id}/submissions", tok, None)


@addon_app.command("submit")
def addon_submit(
    store_id: str = typer.Option(..., "--store-id", "-i", help="The add-on's inAppProductId."),
    title: str = typer.Option(..., "--title", help="Listing title shown in the Store."),
    description: str = typer.Option("", "--description", help="Listing description."),
    base_tier: str = typer.Option(..., "--base-tier", help="Base price tier id (see price-probe)."),
    sale_tier: str = typer.Option(None, "--sale-tier", help="Discounted tier id for a scheduled sale."),
    sale_days: int = typer.Option(0, "--sale-days", help="Sale length in days (requires --sale-tier)."),
    sale_name: str = typer.Option("Launch", "--sale-name", help="Sale display name."),
    lang: str = typer.Option("en-us", "--lang", help="Listing language."),
    content_type: str = typer.Option(
        DEFAULT_CONTENT_TYPE, "--content-type",
        help=f"One of: {', '.join(CONTENT_TYPES)} (legacy taxonomy; see CONTENT_TYPES).",
    ),
    publish_mode: str = typer.Option(
        "Manual", "--publish-mode",
        help="Manual (hold for a human to publish) | Immediate | SpecificDate.",
    ),
    lifetime: str = typer.Option("Forever", "--lifetime", help="Durable lifetime (Forever, etc.)."),
    creds: Path = _CREDS_OPT,
    no_commit: bool = typer.Option(
        False, "--no-commit",
        help="Save the draft but do NOT send it to certification (leave it for review).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the body; change nothing remote."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Build + commit an add-on submission (listing, price, optional launch sale).

    Uses clone-then-override on the live draft so no server-side field is dropped.
    With ``--publish-mode Manual`` the add-on still needs a human to press Publish
    in Partner Center, so this verb never puts a product on sale by itself."""
    if sale_tier and sale_days <= 0:
        raise typer.BadParameter("--sale-tier requires a positive --sale-days")
    if sale_days > 0 and not sale_tier:
        raise typer.BadParameter("--sale-days requires --sale-tier")
    if content_type not in CONTENT_TYPES:
        raise typer.BadParameter(
            f"--content-type must be one of {', '.join(CONTENT_TYPES)}. "
            "The API rejects everything else, including 'NotSet' and the documented "
            "'NotDownloadableContent'."
        )

    tok = _client(creds)
    draft = _ensure_draft(tok, store_id)
    sub_id = draft["id"]

    body = strip_readonly(draft)
    # A draft is born with contentType "NotSet", which the API rejects, so this is a
    # replacement rather than a default-if-absent.
    body["contentType"] = content_type
    body["lifetime"] = lifetime
    body["targetPublishMode"] = publish_mode
    body["visibility"] = body.get("visibility") or "Public"
    body = apply_listing(body, lang=lang, title=title, description=description)

    sale = None
    if sale_tier:
        start = datetime.now(timezone.utc).replace(microsecond=0)
        sale = build_sale(sale_name, sale_tier, start, start + timedelta(days=sale_days))
    body = apply_pricing(body, base_tier=base_tier, sale=sale)

    if dry_run:
        typer.echo(json.dumps(body, indent=2))
        typer.secho("\n--dry-run: nothing was sent.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)

    if not yes and not typer.confirm(
        f"Submit add-on {store_id} (base {base_tier}"
        + (f", sale {sale_tier} for {sale_days}d" if sale_tier else "")
        + f", publish={publish_mode})?",
        default=False,
    ):
        typer.echo("Cancelled.")
        raise typer.Exit(0)

    url = f"{_BASE}/inappproducts/{store_id}/submissions/{sub_id}"
    try:
        _request("PUT", url, tok, body)
    except DevCenterError as exc:
        typer.secho(explain(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from None
    typer.secho("submission body PUT", fg=typer.colors.GREEN)

    # A submission created by a POST that returned 500 is a ZOMBIE: it exists, it
    # accepts PUTs with 200, and it persists NOTHING. Committing one would ship an
    # empty listing at the wrong price, so read back and refuse rather than trust
    # the 200. Observed on a real add-on, not hypothetical.
    drift = _readback_drift(_request("GET", url, tok), body)
    if drift:
        typer.secho(
            "The API accepted the update but did not store it: "
            + ", ".join(drift)
            + ".\nThis submission is unusable (it is typically one created by a POST "
            "that failed with 500). Discard and recreate it:\n"
            f"  navig mstore addon submission-delete --store-id {store_id}\n"
            "  navig mstore addon submit --store-id ... (re-run this command)",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    typer.secho("read-back verified", fg=typer.colors.GREEN)

    if no_commit:
        typer.echo(f"  submission id: {sub_id}")
        typer.secho(
            "--no-commit: saved as a DRAFT, not sent to certification. Review it in "
            "Partner Center (set anything the API cannot verify, e.g. the price tier), "
            "then submit from there.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(0)

    commit = _request(
        "POST", f"{_BASE}/inappproducts/{store_id}/submissions/{sub_id}/commit", tok, None
    )
    typer.secho(f"commit status: {commit.get('status')}", fg=typer.colors.GREEN)
    typer.echo(f"  submission id: {sub_id}")
    if publish_mode == "Manual":
        typer.secho(
            "targetPublishMode=Manual -- publish it from Partner Center when ready.",
            fg=typer.colors.YELLOW,
        )


@addon_app.command("status")
def addon_status(
    store_id: str = typer.Option(..., "--store-id", "-i", help="The add-on's inAppProductId."),
    submission_id: str = typer.Option(None, "--submission-id", "-s", help="Default: the pending one."),
    creds: Path = _CREDS_OPT,
) -> None:
    """Show an add-on submission's certification status and any errors."""
    tok = _client(creds)
    sub_id = submission_id
    if not sub_id:
        product = _request("GET", f"{_BASE}/inappproducts/{store_id}", tok)
        node = product.get("pendingInAppProductSubmission") or product.get(
            "lastPublishedInAppProductSubmission"
        )
        if not node:
            typer.secho("No submission found for this add-on.", fg=typer.colors.YELLOW)
            raise typer.Exit(1)
        sub_id = node["id"]

    res = _request("GET", f"{_BASE}/inappproducts/{store_id}/submissions/{sub_id}/status", tok)
    typer.echo(f"  submission {sub_id}: {res.get('status')}")
    details = res.get("statusDetails") or {}
    for bucket, colour in (("errors", typer.colors.RED), ("warnings", typer.colors.YELLOW)):
        for item in details.get(bucket) or []:
            typer.secho(f"  {bucket[:-1].upper()} {item.get('code')}: {item.get('details')}", fg=colour)


@addon_app.command("submission-delete")
def addon_submission_delete(
    store_id: str = typer.Option(..., "--store-id", "-i", help="The add-on's inAppProductId."),
    submission_id: str = typer.Option(None, "--submission-id", "-s", help="Default: the pending one."),
    creds: Path = _CREDS_OPT,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Discard an add-on's **pending** submission, leaving the add-on itself intact.

    Needed because a half-configured draft is not inert: it shows up in Partner
    Center with a Publish button, so a draft carrying an exploratory price is a
    live mis-pricing risk until it is removed."""
    tok = _client(creds)
    sub_id = submission_id
    if not sub_id:
        product = _request("GET", f"{_BASE}/inappproducts/{store_id}", tok)
        node = product.get("pendingInAppProductSubmission")
        if not node:
            typer.secho("No pending submission to delete.", fg=typer.colors.YELLOW)
            raise typer.Exit(0)
        sub_id = node["id"]
    if not yes and not typer.confirm(f"Delete pending submission {sub_id}?", default=False):
        typer.echo("Cancelled.")
        raise typer.Exit(0)
    _request("DELETE", f"{_BASE}/inappproducts/{store_id}/submissions/{sub_id}", tok)
    typer.secho(f"deleted pending submission {sub_id}", fg=typer.colors.GREEN)


@addon_app.command("delete")
def addon_delete(
    store_id: str = typer.Option(..., "--store-id", "-i", help="The add-on's inAppProductId."),
    creds: Path = _CREDS_OPT,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete an add-on. Refuses once a submission has ever been published."""
    tok = _client(creds)
    product = _request("GET", f"{_BASE}/inappproducts/{store_id}", tok)
    if product.get("lastPublishedInAppProductSubmission"):
        typer.secho(
            "Refusing: this add-on has a published submission. Customers may own it; "
            "retire it in Partner Center instead.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    if not yes and not typer.confirm(
        f"Delete add-on {store_id} ('{product.get('productId')}')?", default=False
    ):
        typer.echo("Cancelled.")
        raise typer.Exit(0)
    _request("DELETE", f"{_BASE}/inappproducts/{store_id}", tok)
    typer.secho("deleted.", fg=typer.colors.GREEN)
