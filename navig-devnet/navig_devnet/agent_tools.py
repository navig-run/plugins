"""Agent tools — let the NAVIG agent run DevNet lookups.

All three are READ-ONLY (resolve a name / read a server's own certificate), so
they are SAFE and not owner_only. They register under the ``devnet`` toolset,
which per-space activation gates: a space that turns the ``devnet`` module off
stops these from being offered to the model (the agent drops the toolset via the
registry's ``disabled_surfaces("toolset")``).

The navig import is guarded (optional-import shim): agent tools are part of
navig's agent, so this module is only ever imported inside navig — standalone
`navig-devnet` never loads it (plugin.register imports it lazily).
"""

from __future__ import annotations

import asyncio
import logging

try:
    from navig.tools.registry import BaseTool, ToolResult
except ImportError as exc:  # pragma: no cover — only navig loads this
    raise ImportError("navig_devnet.agent_tools needs navig (pip install navig)") from exc

from navig_devnet.crawl import crawl as _crawl
from navig_devnet.crawl import discover_api as _discover_api
from navig_devnet.http_repeater import send as _http_send
from navig_devnet.lookups import dns_lookup, ssl_inspect, whois_lookup

_log = logging.getLogger(__name__)


class NetDnsTool(BaseTool):
    name = "net_dns"
    description = (
        "Resolve DNS records for a domain (A, AAAA, MX, TXT, NS, CNAME, SOA, CAA, "
        "PTR, SRV). Read-only; does not send any payload."
    )
    owner_only = False
    parameters = [
        {"name": "domain", "type": "string", "required": True,
         "description": "The domain or host to resolve."},
        {"name": "type", "type": "string", "required": False,
         "description": "Record type (default A). MX/TXT/NS/… need dnspython."},
        {"name": "resolver", "type": "string", "required": False,
         "description": "Optional custom DNS resolver (IP or hostname)."},
    ]

    async def run(self, args, on_status=None) -> ToolResult:
        try:
            out = await asyncio.to_thread(
                dns_lookup, args["domain"],
                record_type=args.get("type", "A"),
                resolver=args.get("resolver") or None,
            )
            return ToolResult(self.name, True, output=out)
        except Exception as exc:  # noqa: BLE001 — tools must never raise
            return ToolResult(self.name, False, error=str(exc))


class NetSslTool(BaseTool):
    name = "net_ssl"
    description = (
        "Inspect a server's TLS certificate: issuer, validity window, days until "
        "expiry, and subject-alternative names. Read-only."
    )
    owner_only = False
    parameters = [
        {"name": "host", "type": "string", "required": True,
         "description": "Host whose TLS certificate to read."},
        {"name": "port", "type": "integer", "required": False,
         "description": "TLS port (default 443)."},
    ]

    async def run(self, args, on_status=None) -> ToolResult:
        try:
            out = await asyncio.to_thread(
                ssl_inspect, args["host"], int(args.get("port", 443) or 443)
            )
            return ToolResult(self.name, True, output=out)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(self.name, False, error=str(exc))


class NetWhoisTool(BaseTool):
    name = "net_whois"
    description = (
        "Look up a domain's WHOIS registration record (registrar, dates, "
        "nameservers). Read-only; bare-TCP, no system whois binary needed."
    )
    owner_only = False
    parameters = [
        {"name": "domain", "type": "string", "required": True,
         "description": "The domain to look up."},
    ]

    async def run(self, args, on_status=None) -> ToolResult:
        try:
            out = await asyncio.to_thread(whois_lookup, args["domain"])
            return ToolResult(self.name, True, output=out)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(self.name, False, error=str(exc))


class NetHttpTool(BaseTool):
    name = "net_http"
    description = (
        "Send an HTTP request (GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS) with custom "
        "headers and body, and read the full response — a repeater. SSRF-guarded: "
        "private/internal targets and redirects to them are blocked."
    )
    #: Sends an arbitrary request (incl. POST/DELETE with a body) to a URL the model
    #: chose, on the operator's behalf — an outward action, so it asks first. `safety`
    #: is what the approval gate reads (owner_only is authorization, not confirmation).
    safety = "dangerous"
    owner_only = False
    parameters = [
        {"name": "url", "type": "string", "required": True,
         "description": "Absolute URL to send the request to."},
        {"name": "method", "type": "string", "required": False,
         "description": "HTTP method (default GET)."},
        {"name": "headers", "type": "object", "required": False,
         "description": "Request headers as a {name: value} object."},
        {"name": "body", "type": "string", "required": False,
         "description": "Request body (string)."},
        {"name": "allow_private", "type": "boolean", "required": False,
         "description": "Allow a private/internal target (SSRF guard off). Default false."},
    ]

    async def run(self, args, on_status=None) -> ToolResult:
        try:
            result = await _http_send(
                args.get("method", "GET"), args["url"],
                headers=args.get("headers") or None,
                body=args.get("body"),
                allow_private=bool(args.get("allow_private", False)),
            )
            return ToolResult(self.name, True, output=result)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(self.name, False, error=f"{type(exc).__name__}: {exc}")


class NetCrawlTool(BaseTool):
    name = "net_crawl"
    description = (
        "Crawl a website and return a map of its pages (url, status, title, "
        "outbound links). Read-only GET-only, same-origin and robots-respecting "
        "by default, with small page/depth caps. SSRF-guarded: private/internal "
        "targets are blocked unless allow_private is set."
    )
    owner_only = False
    parameters = [
        {"name": "url", "type": "string", "required": True,
         "description": "Start URL to crawl."},
        {"name": "max_pages", "type": "integer", "required": False,
         "description": "Max pages to fetch (default 25, hard cap 200)."},
        {"name": "max_depth", "type": "integer", "required": False,
         "description": "Max link depth from the start (default 2, hard cap 6)."},
        {"name": "same_origin", "type": "boolean", "required": False,
         "description": "Only follow links on the start URL's origin (default true)."},
        {"name": "respect_robots", "type": "boolean", "required": False,
         "description": "Honour robots.txt (default true)."},
        {"name": "allow_private", "type": "boolean", "required": False,
         "description": "Allow a private/internal target (SSRF guard off). Default false."},
    ]

    async def run(self, args, on_status=None) -> ToolResult:
        try:
            out = await _crawl(
                args["url"],
                max_pages=int(args.get("max_pages", 25) or 25),
                max_depth=int(args.get("max_depth", 2) or 2),
                same_origin=bool(args.get("same_origin", True)),
                respect_robots=bool(args.get("respect_robots", True)),
                allow_private=bool(args.get("allow_private", False)),
            )
            return ToolResult(self.name, True, output=out)
        except Exception as exc:  # noqa: BLE001 — tools must never raise
            return ToolResult(self.name, False, error=f"{type(exc).__name__}: {exc}")


class NetApiDiscoverTool(BaseTool):
    name = "net_api_discover"
    description = (
        "Find and parse an OpenAPI/Swagger spec for a base URL (tries the "
        "well-known locations), returning the API's title, servers and endpoints "
        "(method, path, summary). Read-only; SSRF-guarded."
    )
    owner_only = False
    parameters = [
        {"name": "url", "type": "string", "required": True,
         "description": "Base URL of the service, or a direct URL to the spec."},
        {"name": "allow_private", "type": "boolean", "required": False,
         "description": "Allow a private/internal target (SSRF guard off). Default false."},
    ]

    async def run(self, args, on_status=None) -> ToolResult:
        try:
            out = await _discover_api(
                args["url"], allow_private=bool(args.get("allow_private", False))
            )
            return ToolResult(self.name, True, output=out)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(self.name, False, error=f"{type(exc).__name__}: {exc}")


def register_devnet_tools() -> None:
    """Register the DevNet tools on the agent registry (toolset ``devnet``)."""
    try:
        from navig.agent.agent_tool_registry import get_agent_tool_registry

        reg = get_agent_tool_registry()
    except Exception:  # noqa: BLE001 — agent subsystem absent
        return
    try:
        reg.register(NetDnsTool(), toolset="devnet")
        reg.register(NetSslTool(), toolset="devnet")
        reg.register(NetWhoisTool(), toolset="devnet")
        reg.register(NetHttpTool(), toolset="devnet")
        reg.register(NetCrawlTool(), toolset="devnet")
        reg.register(NetApiDiscoverTool(), toolset="devnet")
    except Exception as exc:  # noqa: BLE001 — never block boot
        _log.warning("navig-devnet: agent tool registration skipped (%s)", exc)
