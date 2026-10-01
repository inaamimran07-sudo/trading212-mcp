"""Read-only Trading 212 MCP server for Claude.

Environment variables (set these in the Render dashboard, never in code):
  T212_API_KEY      - Trading 212 API key
  T212_API_SECRET   - Trading 212 API secret
  T212_ENV          - "live" (default) or "demo"
  MCP_PATH_SECRET   - long random string; the connector URL is
                      https://<host>/<MCP_PATH_SECRET>/mcp
"""

import base64
import os
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse

API_KEY = os.environ.get("T212_API_KEY", "")
API_SECRET = os.environ.get("T212_API_SECRET", "")
ENV = os.environ.get("T212_ENV", "live").strip().lower()
PATH_SECRET = os.environ.get("MCP_PATH_SECRET", "").strip("/")

if len(PATH_SECRET) < 24:
    raise SystemExit("MCP_PATH_SECRET must be set to a random string of at least 24 characters")

BASE_URL = f"https://{'demo' if ENV == 'demo' else 'live'}.trading212.com/api/v0"

mcp = FastMCP(
    name="Trading 212",
    instructions=(
        "Read-only access to the user's Trading 212 account: cash and account summary, "
        "open positions, pending orders, and order, dividend and transaction history. "
        "This server cannot place, change or cancel orders. Amounts are in the account currency."
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    streamable_http_path=f"/{PATH_SECRET}/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _auth_header() -> str:
    token = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    return f"Basic {token}"


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    if not API_KEY or not API_SECRET:
        return {"error": "T212_API_KEY and T212_API_SECRET are not set on the server."}
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                BASE_URL + path, params=clean, headers={"Authorization": _auth_header()}
            )
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach Trading 212: {exc}"}
    if r.status_code == 429:
        reset = r.headers.get("x-ratelimit-reset")
        wait = max(0, int(reset) - int(time.time())) if reset and reset.isdigit() else None
        return {
            "error": "Trading 212 rate limit reached for this endpoint.",
            "retry_after_seconds": wait,
        }
    if r.status_code in (401, 403):
        return {
            "error": f"Trading 212 rejected the credentials (HTTP {r.status_code}). "
            "Check the API key/secret, that it was made for the "
            f"{ENV} account, and that it has the needed read permissions."
        }
    if r.status_code >= 400:
        return {"error": f"Trading 212 returned HTTP {r.status_code}", "body": r.text[:500]}
    return r.json() if r.content else {}


@mcp.tool()
async def get_account_summary() -> Any:
    """Account overview: cash (free, invested, blocked), total value and profit/loss."""
    return await _get("/equity/account/summary")


@mcp.tool()
async def get_positions() -> Any:
    """All open positions with quantity, average price, current price and profit/loss."""
    return await _get("/equity/positions")


@mcp.tool()
async def get_pending_orders() -> Any:
    """Active orders that have not yet filled, been cancelled or expired."""
    return await _get("/equity/orders")


@mcp.tool()
async def get_order_history(limit: int = 50, cursor: str | None = None, ticker: str | None = None) -> Any:
    """Filled/cancelled order history, newest first. Max 50 per page.
    Pass the cursor from a previous response's nextPagePath to get the next page.
    Optionally filter by a Trading 212 ticker such as AAPL_US_EQ."""
    return await _get(
        "/equity/history/orders",
        {"limit": min(max(limit, 1), 50), "cursor": cursor, "ticker": ticker},
    )


@mcp.tool()
async def get_dividends(limit: int = 50, cursor: str | None = None, ticker: str | None = None) -> Any:
    """Dividends received, newest first. Max 50 per page; use cursor for the next page."""
    return await _get(
        "/equity/history/dividends",
        {"limit": min(max(limit, 1), 50), "cursor": cursor, "ticker": ticker},
    )


@mcp.tool()
async def get_transactions(limit: int = 50, cursor: str | None = None) -> Any:
    """Cash movements (deposits, withdrawals, fees, interest), newest first.
    Max 50 per page; use cursor for the next page."""
    return await _get(
        "/equity/history/transactions",
        {"limit": min(max(limit, 1), 50), "cursor": cursor},
    )


_instrument_cache: dict[str, Any] = {"at": 0.0, "data": None}


@mcp.tool()
async def search_instruments(query: str, max_results: int = 20) -> Any:
    """Find Trading 212 instruments by name, ticker or ISIN (e.g. 'apple', 'TSLA').
    Returns the Trading 212 ticker needed by the history filters."""
    if _instrument_cache["data"] is None or time.time() - _instrument_cache["at"] > 6 * 3600:
        data = await _get("/equity/metadata/instruments")
        if isinstance(data, dict) and "error" in data:
            return data
        _instrument_cache.update(at=time.time(), data=data)
    q = query.strip().lower()
    hits = [
        i for i in _instrument_cache["data"]
        if q in str(i.get("ticker", "")).lower()
        or q in str(i.get("name", "")).lower()
        or q in str(i.get("shortName", "")).lower()
        or q == str(i.get("isin", "")).lower()
    ]
    return hits[: max(1, min(max_results, 100))]


@mcp.custom_route("/", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
