"""EPS (eBay Picture Service) image upload.

The Inventory API wants **hosted https image URLs**, not file uploads. Local
photos must therefore be uploaded to eBay Picture Service first. EPS lives on the
legacy Trading API (`UploadSiteHostedPictures`), but we can authenticate it with
the modern OAuth user token via the ``X-EBAY-API-IAF-TOKEN`` header — no
dev-name/cert-name juggling required.

``ensure_hosted_urls`` is the entry point: https URLs pass through untouched;
local paths are uploaded and replaced with the returned EPS ``FullURL``.
"""

from __future__ import annotations

import re
from pathlib import Path

import requests

from .models import EbayError, endpoints_for
from .oauth_ebay import EbayAuth

COMPAT_LEVEL = "1193"
_UPLOAD_TIMEOUT = 120

# eBay Trading API site ids keyed by Sell-API marketplace id (common markets).
_SITE_IDS = {
    "EBAY_US": "0", "EBAY_CA": "2", "EBAY_GB": "3", "EBAY_AU": "15",
    "EBAY_AT": "16", "EBAY_FR": "71", "EBAY_DE": "77", "EBAY_IT": "101",
    "EBAY_ES": "186", "EBAY_IE": "205",
}

_FULLURL_RE = re.compile(r"<FullURL>(.*?)</FullURL>", re.IGNORECASE | re.DOTALL)
_ERROR_RE = re.compile(r"<(?:LongMessage|ShortMessage)>(.*?)</", re.IGNORECASE | re.DOTALL)


def is_hosted(url: str) -> bool:
    return str(url).startswith(("http://", "https://"))


def ensure_hosted_urls(auth: EbayAuth, images: list[str]) -> list[str]:
    """Return a list of https URLs, uploading any local paths to EPS."""
    out: list[str] = []
    for img in images:
        if is_hosted(img):
            out.append(str(img))
        else:
            out.append(upload_local(auth, img))
    return out


def upload_local(auth: EbayAuth, path: str | Path) -> str:
    """Upload one local image file to EPS, returning its hosted FullURL."""
    p = Path(path)
    if not p.exists():
        raise EbayError(f"image file not found: {p}")

    token = auth.get_access_token()
    site_id = _SITE_IDS.get(auth.config.marketplace_id, "0")
    url = endpoints_for(auth.config.environment)["trading"]

    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<PictureName>{_safe_name(p.stem)}</PictureName>"
        "</UploadSiteHostedPicturesRequest>"
    )
    headers = {
        "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures",
        "X-EBAY-API-SITEID": site_id,
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPAT_LEVEL,
        "X-EBAY-API-IAF-TOKEN": token,
        "X-EBAY-API-DETAIL-LEVEL": "0",
    }
    # Multipart: the XML request part MUST precede the binary image part.
    files = {
        "XML Payload": (None, xml, "text/xml"),
        "dummy": (p.name, p.read_bytes(), "application/octet-stream"),
    }
    resp = requests.post(url, headers=headers, files=files, timeout=_UPLOAD_TIMEOUT)
    body = resp.text or ""
    if resp.status_code != 200:
        raise EbayError(f"EPS upload HTTP {resp.status_code}: {body[:400]}")
    match = _FULLURL_RE.search(body)
    if not match:
        err = _ERROR_RE.search(body)
        detail = err.group(1).strip() if err else body[:400]
        raise EbayError(f"EPS upload did not return a hosted URL: {detail}")
    return match.group(1).strip()


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", name).strip() or "navig-ebay"
    return cleaned[:60]
