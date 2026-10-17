"""Live gold price, locked for each chat.

The first time a chat needs a price, the latest world price is fetched and
then kept for the rest of that chat, so the customer sees one price. A new
chat gets a fresh price. A price fetched less than FRESH_SECONDS ago is
reused by new chats too.

Price services: goldapi.io first. When its monthly checks are used up
(GOLDAPI_MONTHLY_LIMIT, default 100, or when it answers 429), the app switches
to metals.dev until the next calendar month. If goldapi.io is down, metals.dev
is used for that check.

If both are down, the last price is used for up to GOLD_FALLBACK_MINUTES
(default 15). After that, no price is given ("Price on request").

Price per gram for each karat:
  world price (USD per troy ounce) / 31.1034768 g x 3.75 SAR per USD = SAR per gram of pure gold
  x karat / 24 (21K is 21/24 = 87.5% gold)
  + the shop's own margin (gold_markup_percent in settings.json)
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

GRAMS_PER_TROY_OUNCE = 31.1034768
SAR_PER_USD = 3.75  # official fixed exchange rate
RIYADH = timezone(timedelta(hours=3))
CACHE_FILE = Path(__file__).parent / "gold_price_cache.json"
USAGE_FILE = Path(__file__).parent / "gold_api_usage.json"
GOLDAPI_URL = "https://www.goldapi.io/api/price/XAU/USD"
METALS_DEV_URL = "https://api.metals.dev/v1/latest?api_key={key}&currency=USD&unit=toz"

FRESH_SECONDS = 60          # metals.dev updates at most once a minute
RETRY_AFTER_FAILURE = 60    # seconds to wait before asking again after an error

_lock = threading.Lock()
_cache: dict | None = None  # {"usd_per_toz": float, "fetched_at": unix time}
_last_failure = 0.0


class GoldRateUnavailable(Exception):
    """No gold price that is recent enough."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def is_configured() -> bool:
    return bool(os.environ.get("GOLDAPI_KEY") or os.environ.get("METALS_DEV_API_KEY"))


def _load_cache() -> dict | None:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if data.get("usd_per_toz", 0) > 0 else None
    except (OSError, ValueError):
        return None


def _get_json(name: str, request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            message = body.get("error_message") or body.get("error") or ""
        except ValueError:
            message = ""
        raise GoldRateUnavailable(f"{name} error {e.code}: {message}", e.code) from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise GoldRateUnavailable(f"{name} not reachable: {e}") from e


def _check_price(name: str, ok: bool, price, data: dict) -> dict:
    if not ok or not isinstance(price, (int, float)) or price <= 0:
        raise GoldRateUnavailable(f"{name} unexpected answer: {str(data)[:200]}")
    return {"usd_per_toz": float(price), "fetched_at": time.time(), "source": name}


def _fetch_goldapi() -> dict:
    request = urllib.request.Request(GOLDAPI_URL, headers={
        "x-access-token": os.environ["GOLDAPI_KEY"], "User-Agent": "jewelry-agent"})
    data = _get_json("goldapi.io", request)
    return _check_price("goldapi.io", data.get("metal") == "XAU" and data.get("currency") == "USD",
                        data.get("price"), data)


def _fetch_metals_dev() -> dict:
    data = _get_json("metals.dev", urllib.request.Request(
        METALS_DEV_URL.format(key=os.environ["METALS_DEV_API_KEY"])))
    return _check_price("metals.dev", data.get("status") == "success", data.get("metals", {}).get("gold"), data)


# ---------- goldapi.io monthly checks ----------

def _usage() -> dict:
    """This calendar month's goldapi.io usage (goldapi.io counts per calendar month)."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        usage = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        usage = {}
    if usage.get("month") != month:
        usage = {"month": month, "goldapi_checks": 0, "goldapi_used_up": False}
    return usage


def _save_usage(usage: dict) -> None:
    USAGE_FILE.write_text(json.dumps(usage), encoding="utf-8")


def goldapi_available() -> bool:
    if not os.environ.get("GOLDAPI_KEY"):
        return False
    usage = _usage()
    limit = int(os.environ.get("GOLDAPI_MONTHLY_LIMIT", "100"))
    return not usage["goldapi_used_up"] and usage["goldapi_checks"] < limit


def _fetch() -> dict:
    """World gold price in USD per troy ounce: goldapi.io first, metals.dev when it is used up or down."""
    problems = []
    if goldapi_available():
        usage = _usage()
        price = None
        try:
            price = _fetch_goldapi()
            usage["goldapi_checks"] += 1
        except GoldRateUnavailable as e:
            problems.append(str(e))
            if e.status is not None:  # goldapi.io answered, so the check counts
                usage["goldapi_checks"] += 1
            if e.status == 429:  # goldapi.io says the checks are used up
                usage["goldapi_used_up"] = True
        if usage["goldapi_checks"] >= int(os.environ.get("GOLDAPI_MONTHLY_LIMIT", "100")):
            usage["goldapi_used_up"] = True
        if usage["goldapi_used_up"]:
            print(f"  [gold] goldapi.io checks for {usage['month']} are used up "
                  f"({usage['goldapi_checks']} used). Using metals.dev until next month.")
        _save_usage(usage)
        if price:
            return price
    if os.environ.get("METALS_DEV_API_KEY"):
        try:
            return _fetch_metals_dev()
        except GoldRateUnavailable as e:
            problems.append(str(e))
    elif os.environ.get("GOLDAPI_KEY"):
        problems.append("no metals.dev key to switch to (METALS_DEV_API_KEY is empty)")
    raise GoldRateUnavailable("; ".join(problems) or "no gold price service set up")


def world_price() -> dict:
    """The latest world gold price, fetched fresh unless it is under a minute old."""
    global _cache, _last_failure
    fallback = float(os.environ.get("GOLD_FALLBACK_MINUTES", "15")) * 60
    with _lock:
        if _cache is None:
            _cache = _load_cache()
        now = time.time()
        is_fresh = _cache is not None and now - _cache["fetched_at"] < FRESH_SECONDS
        if not is_fresh and now - _last_failure > RETRY_AFTER_FAILURE:
            try:
                _cache = _fetch()
                CACHE_FILE.write_text(json.dumps(_cache), encoding="utf-8")
                print(f"  [gold] world price {_cache['usd_per_toz']:.2f} USD/oz from {_cache['source']}")
            except GoldRateUnavailable as e:
                _last_failure = now
                print(f"  [gold] {e} (using the last price for up to {fallback / 60:.0f} minutes)")
        if _cache is None or now - _cache["fetched_at"] > fallback:
            raise GoldRateUnavailable("No recent gold price")
        return dict(_cache)


def price_for_chat(price_lock: dict | None) -> tuple[dict, bool]:
    """The world price for one chat: fetched fresh the first time, then kept for the chat.

    price_lock is the chat's own dict (None = no chat, always fresh). A lock older than
    GOLD_CHAT_LOCK_HOURS (default 3) is renewed. Returns (price, renewed_now).
    """
    if price_lock is None:
        return world_price(), False
    max_hours = float(os.environ.get("GOLD_CHAT_LOCK_HOURS", "3"))
    locked = price_lock.get("price")
    if locked and time.time() - price_lock["locked_at"] < max_hours * 3600:
        return locked, False
    price_lock["price"] = world_price()
    price_lock["locked_at"] = time.time()
    return price_lock["price"], locked is not None


def shop_rates(markup_percent: float, price_lock: dict | None = None) -> dict:
    """SAR per gram for each karat, including the shop's margin."""
    price, renewed = price_for_chat(price_lock)
    pure_sar_per_gram = price["usd_per_toz"] / GRAMS_PER_TROY_OUNCE * SAR_PER_USD
    factor = 1 + markup_percent / 100
    rates = {k: round(pure_sar_per_gram * int(k) / 24 * factor, 2) for k in ("24", "22", "21", "18")}
    fetched = datetime.fromtimestamp(price["fetched_at"], RIYADH).strftime("%Y-%m-%d %H:%M")
    if price_lock is None:
        return {"rates": rates, "updated_at": f"Live price, taken {fetched} Riyadh time"}
    note = f"Live price taken {fetched} Riyadh time, fixed for this whole chat"
    if renewed:
        note += (". NEW: the chat was open for a long time, so the price was just updated. "
                 "Tell the customer that prices are updated now.")
    return {"rates": rates, "updated_at": note}
