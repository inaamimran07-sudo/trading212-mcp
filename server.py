"""Trading 212 MCP server for Claude.

Environment variables (set these in the Render dashboard, never in code):
  T212_API_KEY      - Trading 212 API key
  T212_API_SECRET   - Trading 212 API secret
  T212_ENV          - "live" (default) or "demo"
  MCP_PATH_SECRET   - long random string; the connector URL is
                      https://<host>/<MCP_PATH_SECRET>/mcp
  TRADING_ENABLED   - "true" to allow order tools. Anything else = read-only.
  MAX_ORDER_VALUE   - largest BUY allowed, in the instrument's currency (default 100)
  REDIS_URL         - optional Render Key Value URL so watchlists survive restarts.
                      Without it, watchlists live in a local JSON file.
"""

import base64
import json
import os
import secrets
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
TRADING_ENABLED = os.environ.get("TRADING_ENABLED", "").strip().lower() == "true"
MAX_ORDER_VALUE = float(os.environ.get("MAX_ORDER_VALUE", "100") or 100)
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "watchlists.json")

if len(PATH_SECRET) < 24:
    raise SystemExit("MCP_PATH_SECRET must be set to a random string of at least 24 characters")

BASE_URL = f"https://{'demo' if ENV == 'demo' else 'live'}.trading212.com/api/v0"

mcp = FastMCP(
    name="Trading 212",
    instructions=(
        "Access to the user's Trading 212 account: cash and account summary, open positions, "
        "pending orders, order/dividend/transaction history, and the user's watchlists "
        "(stored by this connector, not in the Trading 212 app). "
        "Orders are a two-step process: preview_order returns a summary and a confirmation "
        "code; show the summary to the user and call place_order ONLY after the user "
        "explicitly says yes to that exact order in the conversation. Never place an order "
        "on your own initiative. Amounts are in the account currency unless stated."
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    streamable_http_path=f"/{PATH_SECRET}/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _auth_header() -> str:
    if not API_SECRET:
        # Older Trading 212 keys have no secret and are sent as-is.
        return API_KEY
    token = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    return f"Basic {token}"


async def _request(method: str, path: str, params: dict[str, Any] | None = None,
                   body: dict[str, Any] | None = None) -> Any:
    if not API_KEY:
        return {"error": "T212_API_KEY is not set on the server."}
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.request(
                method, BASE_URL + path, params=clean, json=body,
                headers={"Authorization": _auth_header()},
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
            f"{ENV} account, and that it has the needed permissions."
        }
    if r.status_code >= 400:
        return {"error": f"Trading 212 returned HTTP {r.status_code}", "body": r.text[:500]}
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return {"body": r.text[:500]}


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    return await _request("GET", path, params=params)


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


async def _instruments() -> Any:
    if _instrument_cache["data"] is None or time.time() - _instrument_cache["at"] > 6 * 3600:
        data = await _get("/equity/metadata/instruments")
        if isinstance(data, dict) and "error" in data:
            return data
        _instrument_cache.update(at=time.time(), data=data)
    return _instrument_cache["data"]


@mcp.tool()
async def search_instruments(query: str, max_results: int = 20) -> Any:
    """Find Trading 212 instruments by name, ticker or ISIN (e.g. 'apple', 'TSLA').
    Returns the Trading 212 ticker (e.g. AAPL_US_EQ) needed by orders, watchlists
    and the history filters."""
    data = await _instruments()
    if isinstance(data, dict) and "error" in data:
        return data
    q = query.strip().lower()
    hits = [
        i for i in data
        if q in str(i.get("ticker", "")).lower()
        or q in str(i.get("name", "")).lower()
        or q in str(i.get("shortName", "")).lower()
        or q == str(i.get("isin", "")).lower()
    ]
    return hits[: max(1, min(max_results, 100))]


async def _find_instrument(ticker: str) -> dict[str, Any] | None:
    data = await _instruments()
    if not isinstance(data, list):
        return None
    t = ticker.strip()
    return next((i for i in data if str(i.get("ticker", "")) == t), None)


# ---------------------------------------------------------------- orders

_pending: dict[str, dict[str, Any]] = {}
CONFIRM_TTL = 600  # seconds a preview stays valid


async def _held_position(ticker: str) -> dict[str, Any] | None:
    data = await _get("/equity/positions")
    if not isinstance(data, list):
        return None
    for p in data:
        inst = p.get("instrument") or {}
        if inst.get("ticker") == ticker or p.get("ticker") == ticker:
            return p
    return None


async def _yahoo_price(ticker: str) -> float | None:
    if not ticker.endswith("_US_EQ"):
        return None
    sym = ticker[: -len("_US_EQ")].replace("_", "-")
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                            params={"range": "1d", "interval": "1d"})
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None


def _trading_off() -> dict[str, str] | None:
    if TRADING_ENABLED:
        return None
    return {"error": "Trading is switched off on this connector. Set TRADING_ENABLED=true "
            "in the Render environment variables to allow orders."}


@mcp.tool()
async def preview_order(
    ticker: str,
    side: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
    stop_price: float | None = None,
    time_validity: str = "DAY",
) -> Any:
    """Step 1 of placing an order. Checks the order and returns a summary plus a
    confirmation_code. Nothing is sent to Trading 212.

    ticker: Trading 212 ticker such as AAPL_US_EQ (use search_instruments).
    side: "buy" or "sell". quantity: number of shares (fractions allowed), always positive.
    order_type: "market", "limit", "stop" or "stop_limit".
    limit_price / stop_price: required for those order types, in the instrument's currency.
    time_validity: "DAY" or "GOOD_TILL_CANCEL" (ignored for market orders).

    Show the summary to the user and only call place_order after they explicitly
    confirm this exact order."""
    if (off := _trading_off()):
        return off
    side = side.strip().lower()
    order_type = order_type.strip().lower()
    time_validity = time_validity.strip().upper()
    if side not in ("buy", "sell"):
        return {"error": "side must be 'buy' or 'sell'."}
    if order_type not in ("market", "limit", "stop", "stop_limit"):
        return {"error": "order_type must be market, limit, stop or stop_limit."}
    if quantity <= 0:
        return {"error": "quantity must be a positive number of shares."}
    if order_type in ("limit", "stop_limit") and not limit_price:
        return {"error": f"{order_type} orders need limit_price."}
    if order_type in ("stop", "stop_limit") and not stop_price:
        return {"error": f"{order_type} orders need stop_price."}
    if time_validity not in ("DAY", "GOOD_TILL_CANCEL"):
        return {"error": "time_validity must be DAY or GOOD_TILL_CANCEL."}

    inst = await _find_instrument(ticker)
    if inst is None:
        return {"error": f"Unknown ticker {ticker}. Use search_instruments to find the right one."}
    currency = inst.get("currencyCode", "")

    position = await _held_position(ticker)
    warnings: list[str] = []

    if side == "sell":
        held = float((position or {}).get("quantityAvailableForTrading")
                     or (position or {}).get("quantity") or 0)
        if held <= 0:
            return {"error": f"You hold no {ticker} shares available to sell."}
        if quantity > held + 1e-9:
            return {"error": f"You can sell at most {held} {ticker} shares."}

    price = limit_price or stop_price
    price_source = "your limit/stop price"
    if price is None:
        price = (position or {}).get("currentPrice")
        price_source = "Trading 212 position price"
    if price is None:
        price = await _yahoo_price(ticker)
        price_source = "latest market price (Yahoo Finance, may be delayed)"
    if price is None:
        if side == "buy":
            return {"error": "Could not find a current price to check the order size. "
                    "Use a limit order with a limit_price instead."}
        warnings.append("Could not price this sale; value unknown.")
        est_value = None
    else:
        est_value = round(float(price) * quantity, 2)

    if side == "buy" and est_value is not None and est_value > MAX_ORDER_VALUE:
        return {"error": f"Estimated value {est_value} {currency} is above the per-order cap "
                f"of {MAX_ORDER_VALUE} {currency}. Lower the quantity, or raise MAX_ORDER_VALUE "
                "in Render if the user wants a bigger cap."}

    if order_type == "market":
        warnings.append("Market orders fill at whatever price is available; outside US market "
                        "hours they queue until the open.")

    signed_qty = quantity if side == "buy" else -quantity
    body: dict[str, Any] = {"ticker": ticker, "quantity": signed_qty}
    if limit_price:
        body["limitPrice"] = limit_price
    if stop_price:
        body["stopPrice"] = stop_price
    if order_type != "market":
        body["timeValidity"] = time_validity

    now = time.time()
    for code in [c for c, v in _pending.items() if v["expires"] < now]:
        _pending.pop(code, None)
    code = secrets.token_hex(3).upper()
    _pending[code] = {"order_type": order_type, "body": body, "expires": now + CONFIRM_TTL}

    return {
        "confirmation_code": code,
        "expires_in_minutes": CONFIRM_TTL // 60,
        "summary": {
            "action": f"{side.upper()} {quantity} x {inst.get('name', ticker)} ({ticker})",
            "order_type": order_type,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "time_validity": None if order_type == "market" else time_validity,
            "estimated_value": est_value,
            "currency": currency,
            "price_used_for_estimate": price,
            "price_source": price_source if price is not None else None,
            "account": ENV,
        },
        "warnings": warnings,
        "next_step": "Show this to the user. Call place_order with this code only after "
                     "they explicitly confirm.",
    }


@mcp.tool()
async def place_order(confirmation_code: str) -> Any:
    """Step 2: sends an order previewed with preview_order to Trading 212.
    ONLY call this after the user has explicitly confirmed that exact order in the
    conversation. Each code works once and expires after 10 minutes."""
    if (off := _trading_off()):
        return off
    entry = _pending.pop(confirmation_code.strip().upper(), None)
    if entry is None or entry["expires"] < time.time():
        return {"error": "That confirmation code is unknown or expired. Run preview_order again."}
    path = {"market": "/equity/orders/market", "limit": "/equity/orders/limit",
            "stop": "/equity/orders/stop", "stop_limit": "/equity/orders/stop_limit"}[entry["order_type"]]
    result = await _request("POST", path, body=entry["body"])
    if isinstance(result, dict) and "error" in result:
        return result
    return {"status": "submitted", "order": result}


@mcp.tool()
async def cancel_order(order_id: int) -> Any:
    """Cancel a pending (unfilled) order by its id from get_pending_orders.
    Confirm with the user before cancelling."""
    if (off := _trading_off()):
        return off
    result = await _request("DELETE", f"/equity/orders/{int(order_id)}")
    if isinstance(result, dict) and "error" in result:
        return result
    return {"status": "cancelled", "order_id": order_id}


# ------------------------------------------------------------ watchlists

_redis = None


async def _load_watchlists() -> dict[str, Any]:
    global _redis
    if REDIS_URL:
        if _redis is None:
            import redis.asyncio as redis_async
            _redis = redis_async.from_url(REDIS_URL, decode_responses=True)
        raw = await _redis.get("t212:watchlists")
        return json.loads(raw) if raw else {}
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


async def _save_watchlists(data: dict[str, Any]) -> None:
    if REDIS_URL:
        await _load_watchlists()  # ensures client exists
        await _redis.set("t212:watchlists", json.dumps(data))
        return
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _key(name: str) -> str:
    return name.strip().lower()


@mcp.tool()
async def list_watchlists() -> Any:
    """All of the user's watchlists with their tickers and notes. These are stored by
    this connector; they do not appear in the Trading 212 app."""
    data = await _load_watchlists()
    return list(data.values()) or {"message": "No watchlists yet."}


@mcp.tool()
async def add_to_watchlist(watchlist: str, items: list[dict[str, str]],
                           description: str | None = None) -> Any:
    """Create a watchlist if needed and add stocks to it.
    items: list of {"ticker": "AAPL_US_EQ", "note": "why it's on the list"}; note optional.
    Tickers should be Trading 212 tickers (use search_instruments). Re-adding a ticker
    updates its note."""
    data = await _load_watchlists()
    k = _key(watchlist)
    wl = data.setdefault(k, {"name": watchlist.strip(), "description": "", "items": [],
                             "created": time.strftime("%Y-%m-%d")})
    if description is not None:
        wl["description"] = description
    added, unknown = [], []
    for it in items:
        t = str(it.get("ticker", "")).strip()
        if not t:
            continue
        inst = await _find_instrument(t)
        if inst is None:
            unknown.append(t)
        existing = next((x for x in wl["items"] if x["ticker"] == t), None)
        entry = {"ticker": t, "name": (inst or {}).get("name", ""), "note": it.get("note", ""),
                 "added": time.strftime("%Y-%m-%d")}
        if existing:
            existing.update({k2: v for k2, v in entry.items() if k2 != "added"})
        else:
            wl["items"].append(entry)
        added.append(t)
    await _save_watchlists(data)
    out: dict[str, Any] = {"watchlist": wl}
    if unknown:
        out["warning"] = f"Not found in Trading 212's instrument list: {', '.join(unknown)}"
    return out


@mcp.tool()
async def remove_from_watchlist(watchlist: str, tickers: list[str]) -> Any:
    """Remove tickers from a watchlist."""
    data = await _load_watchlists()
    wl = data.get(_key(watchlist))
    if wl is None:
        return {"error": f"No watchlist called {watchlist}."}
    wl["items"] = [x for x in wl["items"] if x["ticker"] not in set(tickers)]
    await _save_watchlists(data)
    return {"watchlist": wl}


@mcp.tool()
async def delete_watchlist(watchlist: str) -> Any:
    """Delete a whole watchlist. Confirm with the user first."""
    data = await _load_watchlists()
    if data.pop(_key(watchlist), None) is None:
        return {"error": f"No watchlist called {watchlist}."}
    await _save_watchlists(data)
    return {"status": "deleted", "watchlist": watchlist}


@mcp.custom_route("/", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
