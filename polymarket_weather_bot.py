"""
polymarket_weather_bot.py - Polymarket US daily-high temperature bot.
Paper by default: places no orders and needs no API keys (market data is public).

    python polymarket_weather_bot.py               scan every 5 minutes, record paper trades
    python polymarket_weather_bot.py --once        one scan, print the table, exit
    python polymarket_weather_bot.py --check-live  test the API keys; previews orders, places none
    python polymarket_weather_bot.py --live        REAL MONEY, within the LIVE_ limits below
    python polymarket_report.py                    how the trades are doing

Same idea as weather_bot.py (Kalshi), pointed at Polymarket US
----------------------------------------------------------------
Polymarket US lists "Highest temperature in <city> on <date>?" events: six 2°F
brackets per city-day, settled on the NWS Daily Climate Report at the same airport
stations Kalshi uses (KNYC, KMDW, KMIA, KLAX, KSFO). So the model is shared:
weather_model.py turns station observations + the NWS forecast into a probability
for every bracket, and build_model() in weather_bot.py gathers those inputs.

Every scan:
1. Finds open temperature events for today and the next DAYS_AHEAD days.
2. Reads each bracket's range and settlement station from its rules text, and skips
   anything that doesn't parse or doesn't add up to ~100%.
3. If buying YES or NO at the current best price beats MIN_EDGE after Polymarket
   US's taker fee, records a PAPER trade in polymarket_paper_trades.csv. One per market.
4. Fills in each paper trade's result and profit once its market resolves.

Live mode (--live)
-----------------
Needs POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY in a .env file next to this script
(never commit it; .gitignore blocks it). Signals become real orders, one per market:
  * limit order at the current best price, immediate-or-cancel, so it never rests on
    the book and never fills worse than the price the edge was computed at
  * LIVE_DOLLARS_PER_TRADE per order, whole contracts
  * never more than LIVE_MAX_OPEN_DOLLARS in unsettled live trades, and never more
    than LIVE_MAX_TRADES_PER_DAY new live trades per day
  * at most LIVE_MAX_PER_EVENT live trades per city/day, and at most one YES: brackets are
    exclusive, so two YES bets on one event can't both win (best edge is placed first)
  * no YES long shots: nothing under LIVE_MIN_YES_PRICE, and YES under LIVE_YES_LONGSHOT_BELOW
    needs extra edge (long-shot bias; see the settings)
  * kill switch: once settled live trades are down LIVE_KILL_LOSS, live stops for good
  * live test: after the first LIVE_TEST_TRADES live trades settle, if they didn't
    make money after fees, live stops for good
Signals that can't go live (caps, no fill, kill switch) are still logged as paper.

Selling early (EXIT_MODE)
-------------------------
Every scan re-prices each open live bet. If selling at the current bid pays at least
EXIT_MIN_EDGE per contract more (after the fee) than the bet is worth, it is sold with an
immediate-or-cancel limit: that cuts bets the forecast turned against and banks winners the
market overpays for. It never sells a bet worth more than the market pays. "Worth" is the
model's probability, except on the settlement day, when the market prices live readings
faster than the model can, so it is blended with the market mid (SAME_DAY_MODEL_WEIGHT).
Never on bad data (model and market too far apart) or closed markets. Runs even while new
live buys are paused, since selling only reduces risk. A sold market is never bought again.
Every sale (or, in "monitor" mode, would-be sale) is logged to polymarket_exit_log.csv.
"""

import argparse
import csv
import json
import os
import re
import time
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN

import requests

import weather_model as wm
from weather_bot import build_model, now_utc  # also makes ¢ / °F printable on Windows

# =============================================================================
# SETTINGS
# =============================================================================
SCAN_INTERVAL_SECONDS = 300      # one full scan every 5 minutes
MIN_EDGE = 0.07                  # need 7¢+ expected profit per contract after fees
MAX_EDGE = 0.30                  # an "edge" above 30¢ is almost always a data problem
MIN_PRICE, MAX_PRICE = 0.03, 0.97  # skip prices where 1¢ ticks dominate
SUM_TOLERANCE = 0.04             # an event's brackets must add to ~100% or it's skipped
MAX_CENTER_GAP_F = 2.0           # model and market more than 2°F apart = likely bad data, skip
TRADE_SAME_DAY = False           # same-day markets are priced off live readings we can't beat; log only
DAYS_AHEAD = 2                   # look for events up to this many days past today
SNAPSHOT_EVERY_SECONDS = 3600    # log every bracket's model vs market once an hour
DEFAULT_FEE_RATE = 0.0695        # Polymarket US taker fee: rate x C x p x (1-p); per-market feeCoefficient wins

# Live limits (only used with --live). Sized for a ~$130 account.
LIVE_DOLLARS_PER_TRADE = 3.00    # spend about this much per trade (whole contracts, at least 1)
LIVE_MAX_OPEN_DOLLARS = 60.00    # most money ever tied up in unsettled live trades
LIVE_MAX_TRADES_PER_DAY = 10     # new live trades per UTC day
LIVE_MAX_PER_EVENT = 2           # live trades per city/day (one event); correlated bets
LIVE_MAX_YES_PER_EVENT = 1       # only one bracket can win, so a second YES is a sure partial loss
# Live YES long shots. Becker, "Microstructure of Wealth Transfer in Prediction Markets" (72M
# Kalshi trades, 2021-2025): contracts under 20c win less often than their price implies, by far
# the most for YES under 10c, and NO beats YES at most prices. Live only; paper still logs them.
LIVE_MIN_YES_PRICE = 0.10        # no live YES bought below this
LIVE_YES_LONGSHOT_BELOW = 0.20   # live YES below this price...
LIVE_YES_LONGSHOT_EXTRA_EDGE = 0.03  # ...needs this much more than MIN_EDGE
LIVE_KILL_LOSS = 40.00           # settled live trades down this much -> live stops for good
LIVE_TEST_TRADES = 30            # once this many live trades settle, they must be net positive...
                                 # ...or live stops for good

# Selling early (only with --live). See "Selling early" above.
EXIT_MODE = "live"               # "live" = really sell; "monitor" = only log what it would sell; "off"
EXIT_MIN_EDGE = 0.03             # sell only when that beats holding by 3¢+ per contract after the fee
SAME_DAY_MODEL_WEIGHT = 0.5      # settlement day: worth = 50% model + 50% market mid
# =============================================================================

GATEWAY = "https://gateway.polymarket.us/v1"   # public market data, no key needed
MIN_REQUEST_GAP = 1.1                          # public API allows 60 requests/minute
KNOWN_CITY_CODES = ["nyc", "mdw", "mia", "lax", "sfo"]   # event slugs: temp-<code>high-YYYY-MM-DD
EVENT_SLUG = re.compile(r"^temp-([a-z]+)high-(\d{4})-(\d{2})-(\d{2})$")

# Settlement station -> (standard-time UTC offset, name), from the Kalshi city table
STATIONS = {st: (offset, name) for st, offset, name in wm.CITIES.values()}

TRADES_CSV = "polymarket_paper_trades.csv"
SNAPSHOT_CSV = "polymarket_model_log.csv"
EXIT_LOG_CSV = "polymarket_exit_log.csv"
EXIT_LOG_FIELDS = ["time_utc", "mode", "market", "side", "count", "entry_price", "sell_price", "sell_net",
                   "fair_value", "edge", "pnl_if_sold", "filled", "order_id"]
TRADE_FIELDS = ["time_utc", "mode", "market", "event", "city", "station", "bracket", "side", "price",
                "fee", "count", "model_win_prob", "edge", "yes_bid", "yes_ask", "model_center_f",
                "obs_max_f", "future_max_f", "status", "result", "pnl", "order_id", "fill_px_raw",
                "exit_px", "exit_fee", "exit_time_utc", "exit_order_id"]
SNAP_FIELDS = ["time_utc", "market", "bracket", "model_p_yes", "yes_bid", "yes_ask",
               "model_center_f", "obs_max_f", "future_max_f"]


# ---- Polymarket US (public) -----------------------------------------------------
_last_request = 0.0


def gateway_get(path, params=None):
    """GET from the public gateway, spaced to stay under the rate limit. None on 404."""
    global _last_request
    wait = MIN_REQUEST_GAP - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()
    res = requests.get(GATEWAY + path, params=params, timeout=20)
    if res.status_code == 404:
        return None
    if res.status_code != 200:
        raise RuntimeError(f"Polymarket GET {path} -> {res.status_code}: {res.text[:200]}")
    return res.json()


def get_event(slug):
    data = gateway_get(f"/events/slug/{slug}")
    if not data:
        return None
    return data.get("event", data)


def city_codes():
    """Known city codes, plus any new temperature series the search turns up."""
    codes = set(KNOWN_CITY_CODES)
    try:
        for ev in (gateway_get("/search", {"query": "highest temperature"}) or {}).get("events", []):
            m = EVENT_SLUG.match(str(ev.get("slug", "")))
            if m:
                codes.add(m.group(1))
    except Exception:
        pass
    return sorted(codes)


def quote(obj, key):
    try:
        return float((obj.get(key) or {}).get("value"))
    except (TypeError, ValueError):
        return 0.0


def parse_bracket(description):
    """Rules text -> (station, low_f, high_f), whole degrees inclusive, None = open end."""
    st = re.search(r"\((K[A-Z]{3})\)", description or "")
    station = st.group(1) if st else None
    m = re.search(r"between (-?\d+)F and (-?\d+)F", description or "")
    if m:
        return station, int(m.group(1)), int(m.group(2))
    m = re.search(r"less than or equal to (-?\d+)F", description or "")
    if m:
        return station, None, int(m.group(1))
    m = re.search(r"greater than or equal to (-?\d+)F", description or "")
    if m:
        return station, int(m.group(1)), None
    return station, None, None


def bracket_label(lo, hi):
    if lo is None:
        return f"{hi}F or below"
    if hi is None:
        return f"{lo}F or above"
    return f"{lo}-{hi}F"


def bracket_prob(model, lo, hi):
    """P(reported whole-degree high is in lo..hi): true high in [lo-0.5, hi+0.5)."""
    upper = 1.0 if hi is None else model.cdf(hi + 0.5)
    lower = 0.0 if lo is None else model.cdf(lo - 0.5)
    return max(0.0, upper - lower)


def market_center_f(brackets):
    """Temperature the market is centred on (price-weighted bracket midpoints)."""
    num = den = 0.0
    for b in brackets:
        lo, hi = b["lo"], b["hi"]
        if lo is not None and hi is not None:
            x = (lo + hi) / 2.0
        elif lo is not None:
            x = lo + 1.0
        elif hi is not None:
            x = hi - 1.0
        else:
            continue
        mid = (b["bid"] + b["ask"]) / 2.0
        num += mid * x
        den += mid
    return num / den if den > 0 else None


# ---- Fees and edge ----------------------------------------------------------------
def taker_fee(price, rate=DEFAULT_FEE_RATE, count=1, rounded=False):
    """
    Polymarket US taker fee: rate x C x p x (1-p), symmetric for YES and NO buys.
    The exchange rounds each order's fee to the cent (banker's rounding). Edge math
    uses the unrounded fee: at 1 contract, rounding would make cheap contracts look
    free (0.3¢ -> 0¢), which isn't what they cost at real size.
    """
    exact = Decimal(str(rate)) * Decimal(str(count)) * Decimal(str(price)) * (1 - Decimal(str(price)))
    if rounded:
        return float(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
    return float(exact)


def best_trade(p_yes, yes_bid, yes_ask, rate):
    """
    Best of: buy YES at the ask, or buy NO at (1 - YES bid).
    Returns (side, cost_per_contract, fee, win_prob, edge) or None.
    """
    options = []
    if 0.01 <= yes_ask <= 0.99:
        fee = taker_fee(yes_ask, rate)
        options.append(("YES", yes_ask, fee, p_yes, p_yes - yes_ask - fee))
    if 0.01 <= yes_bid <= 0.99:
        cost = round(1.0 - yes_bid, 4)
        fee = taker_fee(cost, rate)
        options.append(("NO", cost, fee, 1.0 - p_yes, (1.0 - p_yes) - cost - fee))
    if not options:
        return None
    return max(options, key=lambda o: o[4])


# ---- Trade log ----------------------------------------------------------------------
_unsaved_live = {}   # market -> live trade row placed but not yet written to TRADES_CSV


def load_trades():
    rows = []
    if os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    # Real orders whose save failed are kept in memory so they're never placed twice
    on_file = {r.get("market") for r in rows}
    rows.extend(r for m, r in _unsaved_live.items() if m not in on_file)
    return rows


def save_trades(rows):
    tmp = TRADES_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TRADE_FIELDS})
    os.replace(tmp, TRADES_CSV)
    saved = {r.get("market") for r in rows}
    for m in [m for m in _unsaved_live if m in saved]:
        del _unsaved_live[m]


def append_snapshots(rows):
    new = not os.path.exists(SNAPSHOT_CSV)
    with open(SNAPSHOT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SNAP_FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def market_result(m):
    """'yes' / 'no' once a market has resolved, else None."""
    if m.get("status") != "MARKET_STATUS_RESOLVED":
        return None
    try:
        yes_px = float(json.loads(m.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError):
        return None
    if yes_px >= 0.99:
        return "yes"
    if yes_px <= 0.01:
        return "no"
    return None


def settle_trades(rows, events):
    """Fill in results for trades whose markets have resolved. events: slug -> event cache."""
    changed = 0
    for r in rows:
        if r.get("status") != "open":
            continue
        if r["event"] not in events:
            try:
                events[r["event"]] = get_event(r["event"])
            except Exception:
                continue
        ev = events[r["event"]] or {}
        m = next((m for m in ev.get("markets", []) if m.get("slug") == r["market"]), None)
        result = market_result(m) if m else None
        if result is None:
            continue
        won = (result == "yes") == (r["side"] == "YES")
        price, fee, count = float(r["price"]), float(r["fee"]), float(r.get("count") or 1)
        r["result"] = result
        r["pnl"] = f"{((1.0 - price) if won else -price) * count - fee:.4f}"
        r["status"] = "settled"
        changed += 1
    return changed


# ---- Live (--live only) ---------------------------------------------------------------
_client = None


def live_client():
    """Authenticated Polymarket US client, keys from .env next to this script."""
    global _client
    if _client is None:
        from dotenv import load_dotenv
        from polymarket_us import PolymarketUS
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
        key_id = os.getenv("POLYMARKET_KEY_ID", "").strip()
        secret = os.getenv("POLYMARKET_SECRET_KEY", "").strip()
        if not key_id or not secret:
            raise RuntimeError("POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY missing from .env")
        _client = PolymarketUS(key_id=key_id, secret_key=secret)
    return _client


def account_balance(client):
    """The USD balance entry: {'currentBalance', 'buyingPower', ...}."""
    res = client.account.balances()
    entries = res.get("balances") if isinstance(res.get("balances"), list) else [res]
    return next((e for e in entries if e.get("currency", "USD") == "USD"), entries[0] if entries else {})


def buying_power(client):
    return float(account_balance(client).get("buyingPower") or 0.0)


def held_markets(client):
    """Markets the account already has a position in (never add to them)."""
    pos = client.portfolio.positions().get("positions") or {}
    if isinstance(pos, dict):
        return set(pos)
    return {p.get("marketSlug") for p in pos if isinstance(p, dict)}


def live_cost(r):
    return float(r["price"]) * float(r.get("count") or 1) + float(r.get("fee") or 0)


def live_allowed(rows):
    """Returns (allowed, reason). Checked once per scan; place_live re-checks per order."""
    live = [r for r in rows if r.get("mode") == "live"]
    settled = [r for r in live if r.get("status") == "settled"]
    net = sum(float(r["pnl"]) for r in settled)
    if net <= -LIVE_KILL_LOSS:
        return False, f"KILL SWITCH: live trades lost ${-net:.2f} (limit ${LIVE_KILL_LOSS:.2f}); paper only"
    # Live test: the first LIVE_TEST_TRADES live trades placed decide it. No live trade
    # beyond them until all have settled; then they must be net positive after fees.
    test = live[:LIVE_TEST_TRADES]
    test_net = sum(float(r["pnl"]) for r in test if r.get("status") == "settled")
    if len(test) >= LIVE_TEST_TRADES:
        if any(r.get("status") != "settled" for r in test):
            return False, (f"live test: all {LIVE_TEST_TRADES} trades placed, waiting for them to settle "
                           f"(net ${test_net:+.2f} so far)")
        if test_net <= 0:
            return False, f"LIVE TEST FAILED: first {LIVE_TEST_TRADES} live trades net ${test_net:+.2f}; paper only"
    open_cost = sum(live_cost(r) for r in live if r.get("status") == "open")
    phase = "test passed" if len(test) >= LIVE_TEST_TRADES else f"test {len(test)}/{LIVE_TEST_TRADES}"
    return True, (f"LIVE ({phase}): {len(settled)} settled, net ${net:+.2f} | "
                  f"${open_cost:.2f} open of ${LIVE_MAX_OPEN_DOLLARS:.0f} cap")


def longshot_reason(trade):
    """Why a YES long shot shouldn't go live, or None."""
    if trade["side"] != "YES":
        return None
    price, edge = float(trade["price"]), float(trade["edge"])
    if price < LIVE_MIN_YES_PRICE:
        return f"YES at {price * 100:.0f}¢ is under the {LIVE_MIN_YES_PRICE * 100:.0f}¢ live floor (long-shot bias)"
    need = MIN_EDGE + LIVE_YES_LONGSHOT_EXTRA_EDGE
    if price < LIVE_YES_LONGSHOT_BELOW and edge < need:
        return f"YES at {price * 100:.0f}¢ needs {need * 100:.0f}¢ edge, has {edge * 100:.1f}¢ (long-shot bias)"
    return None


def place_live(trade, rows, client, held):
    """Send one real order for a signal. Returns (order_id, filled, count, raw_fill_px) or None."""
    live = [r for r in rows if r.get("mode") == "live"]
    if trade["market"] in held:
        print(f"    [live] {trade['market']}: account already holds this market, skipped")
        return None
    why = longshot_reason(trade)
    if why:
        print(f"    [live] {trade['market']}: {why}; kept as paper")
        return None
    if len(live) >= LIVE_TEST_TRADES and any(r.get("status") != "settled" for r in live[:LIVE_TEST_TRADES]):
        return None
    today = now_utc().date().isoformat()
    if sum(1 for r in live if str(r.get("time_utc", ""))[:10] == today) >= LIVE_MAX_TRADES_PER_DAY:
        print(f"    [live] daily limit of {LIVE_MAX_TRADES_PER_DAY} live trades reached")
        return None
    same_event = [r for r in live if r.get("event") == trade["event"]]
    if len(same_event) >= LIVE_MAX_PER_EVENT:
        print(f"    [live] {trade['market']}: already {len(same_event)} live trades on {trade['event']}, skipped")
        return None
    if trade["side"] == "YES" and sum(r.get("side") == "YES" for r in same_event) >= LIVE_MAX_YES_PER_EVENT:
        print(f"    [live] {trade['market']}: already hold a YES on {trade['event']} (only one bracket can win), skipped")
        return None

    cost = float(trade["price"])
    count = max(1, int(LIVE_DOLLARS_PER_TRADE // cost))
    fee = taker_fee(cost, trade["_rate"], count, rounded=True)
    need = cost * count + fee
    open_cost = sum(live_cost(r) for r in live if r.get("status") == "open")
    if open_cost + need > LIVE_MAX_OPEN_DOLLARS:
        print(f"    [live] skipped: ${open_cost:.2f} already open (cap ${LIVE_MAX_OPEN_DOLLARS:.2f})")
        return None
    bp = buying_power(client)
    if bp < need:
        print(f"    [live] skipped: buying power ${bp:.2f} < ${need:.2f} needed")
        return None

    # Polymarket US prices every order in YES terms. Buying NO = BUY_SHORT at the YES bid
    # (NO cost = 1 - bid). IOC limit: fills at this price or better, or not at all.
    yes_px = float(trade["yes_ask"]) if trade["side"] == "YES" else float(trade["yes_bid"])
    params = {"marketSlug": trade["market"],
              "intent": "ORDER_INTENT_BUY_LONG" if trade["side"] == "YES" else "ORDER_INTENT_BUY_SHORT",
              "type": "ORDER_TYPE_LIMIT",
              "price": {"value": f"{yes_px:.2f}", "currency": "USD"},
              "quantity": count,
              "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
              "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
              "synchronousExecution": True,
              "maxBlockTime": "5"}
    oid, filled, raw_px = order_fills(client, client.orders.create(params))
    if filled <= 0:
        print(f"    [live] {trade['market']}: not filled (price moved); kept as paper")
        return None
    return oid, filled, count, raw_px


def order_fills(client, res):
    """(order_id, contracts filled, raw fill prices) for an immediate-or-cancel order."""
    oid = res.get("id", "")
    fills = [e for e in res.get("executions") or []
             if e.get("type") in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL")]
    filled = sum(float(e.get("lastShares") or 0) for e in fills)
    if not res.get("executions") and oid:
        time.sleep(1.0)   # no synchronous result: ask for the order's final state
        o = (client.orders.retrieve(oid) or {}).get("order", {})
        filled = float(o.get("cumQuantity") or 0)
    raw_px = ";".join(str((e.get("lastPx") or {}).get("value", "")) for e in fills)
    return oid, filled, raw_px


def go_live(signals, rows, client):
    """Turn this scan's signals into real orders, within the limits. Unplaced ones stay paper."""
    try:
        held = held_markets(client)
    except Exception as e:
        print(f"[!] Live paused this scan: couldn't read positions ({e})")
        return
    for s in sorted(signals, key=lambda s: -float(s["edge"])):  # best edge first when caps bind
        try:
            placed = place_live(s, rows, client, held)
        except Exception as e:
            # The order may or may not have reached the exchange. The positions check
            # next scan stops it being placed twice if it did.
            print(f"[!] LIVE ORDER ERROR on {s['market']}: {e}. Stopping live orders this scan.")
            return
        if not placed:
            continue
        oid, filled, count, raw_px = placed
        s["mode"], s["count"], s["order_id"], s["fill_px_raw"] = "live", f"{filled:g}", oid, raw_px
        s["fee"] = f"{taker_fee(float(s['price']), s['_rate'], filled, rounded=True):.2f}"
        held.add(s["market"])
        _unsaved_live[s["market"]] = s
        print(f"    [LIVE] {s['side']} {filled:g} x {s['market']} @ ${float(s['price']):.2f} (order {oid})")
        # Record every real order the moment it fills; if the file can't be written
        # (e.g. open in Excel), stop placing orders until it can.
        try:
            save_trades(rows)
        except Exception as e:
            print(f"[!!] LIVE ORDER {oid} could not be saved to {TRADES_CSV} ({e}). "
                  f"Live paused this scan. Close the file if it is open in Excel.")
            return


def check_live():
    """Prove the keys work and show how the exchange reads our orders. Places nothing."""
    client = live_client()
    bal = account_balance(client)
    print(f"[ok] Keys work. Balance ${float(bal.get('currentBalance') or 0):.2f}, "
          f"buying power ${float(bal.get('buyingPower') or 0):.2f}")
    print(f"[ok] Markets with a position: {len(held_markets(client))}")
    tomorrow = now_utc().date() + timedelta(days=1)
    for code in KNOWN_CITY_CODES:
        ev = get_event(f"temp-{code}high-{tomorrow:%Y-%m-%d}")
        for m in (ev or {}).get("markets", []):
            bid, ask = quote(m, "bestBidQuote"), quote(m, "bestAskQuote")
            if 0.05 <= bid < ask <= 0.95:
                print(f"\nPreviewing orders on {m['slug']} (YES bid {bid:.2f} / ask {ask:.2f}). NOT placed:")
                for intent, px, meaning in (("ORDER_INTENT_BUY_LONG", ask, f"buy YES at ${ask:.2f}"),
                                            ("ORDER_INTENT_BUY_SHORT", bid, f"buy NO at ${1 - bid:.2f}")):
                    req = {"marketSlug": m["slug"], "intent": intent, "type": "ORDER_TYPE_LIMIT",
                           "price": {"value": f"{px:.2f}", "currency": "USD"}, "quantity": 1,
                           "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"}
                    try:
                        o = client.orders.preview({"request": req}).get("order", {})
                        print(f"  want {meaning:<18} -> exchange reads: side={o.get('side')} "
                              f"price={(o.get('price') or {}).get('value')} intent={o.get('intent')}")
                    except Exception as e:
                        print(f"  want {meaning:<18} -> preview failed: {e}")
                print("\nExpected: buy YES -> ORDER_SIDE_BUY at the ask; buy NO -> ORDER_SIDE_SELL at the YES bid.")
                print("No orders were placed.")
                return
    print("No open temperature market with a two-sided book found to preview.")


# ---- Selling early (--live only) -------------------------------------------------------
def fair_value(side, mark):
    """What the held side is worth now. On the settlement day the market prices live readings
    faster than our model, so the model gets SAME_DAY_MODEL_WEIGHT and the market mid the rest."""
    p_yes = mark["p_yes"]
    if mark["same_day"] and 0 < mark["bid"] < mark["ask"] < 1:
        mid = (mark["bid"] + mark["ask"]) / 2
        p_yes = SAME_DAY_MODEL_WEIGHT * p_yes + (1 - SAME_DAY_MODEL_WEIGHT) * mid
    return p_yes if side == "YES" else 1.0 - p_yes


def exit_signal(side, mark):
    """(sell_price, net_per_contract, fair_value) if selling beats holding by EXIT_MIN_EDGE, else None.
    Prices are for the side held: a YES sells at the YES bid, a NO at 1 - YES ask."""
    px = mark["bid"] if side == "YES" else round(1.0 - mark["ask"], 4)
    if not 0.01 <= px <= 0.99:
        return None   # no buyer at a real price
    value = fair_value(side, mark)
    net = px - taker_fee(px, mark["rate"])
    if net - value < EXIT_MIN_EDGE:
        return None
    return px, net, value


def cents(value, up):
    """Limit price to the cent, rounded so an immediate-or-cancel order can still fill."""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_CEILING if up else ROUND_FLOOR))


def sell_live(client, row, mark, qty):
    """Sell `qty` contracts of a live bet at the current bid. Returns (order_id, filled)."""
    # Prices are in YES terms: a YES sells at the YES bid; a NO is closed by buying YES at the ask.
    is_yes = row["side"] == "YES"
    params = {"marketSlug": row["market"],
              "intent": "ORDER_INTENT_SELL_LONG" if is_yes else "ORDER_INTENT_SELL_SHORT",
              "type": "ORDER_TYPE_LIMIT",
              "price": {"value": cents(mark["bid"] if is_yes else mark["ask"], up=not is_yes), "currency": "USD"},
              "quantity": qty,
              "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
              "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
              "synchronousExecution": True,
              "maxBlockTime": "5"}
    oid, filled, _ = order_fills(client, client.orders.create(params))
    return oid, filled


def record_exit(rows, row, filled, px, rate, oid):
    """Book a sale: the sold contracts become a settled row (result 'sold'); any rest stays open."""
    count, entry_fee = float(row["count"]), float(row.get("fee") or 0)
    share = min(filled / count, 1.0)
    sell_fee = taker_fee(px, rate, filled, rounded=True)
    sold = dict(row)
    sold.update(count=f"{filled:g}", fee=f"{entry_fee * share:.4f}", status="settled", result="sold",
                pnl=f"{(px - float(row['price'])) * filled - entry_fee * share - sell_fee:.4f}",
                exit_px=f"{px:.4f}", exit_fee=f"{sell_fee:.4f}", exit_time_utc=now_utc().isoformat(),
                exit_order_id=oid)
    if share >= 1.0:
        row.update(sold)
    else:
        row.update(count=f"{count - filled:g}", fee=f"{entry_fee * (1 - share):.4f}")
        rows.append(sold)


def pnl_if_sold(row, qty, net):
    """Estimated P&L of selling qty contracts at `net` per contract, including their share of the entry fee."""
    count = float(row.get("count") or qty)
    return (net - float(row["price"])) * qty - float(row.get("fee") or 0) * qty / count


def log_exit(row, qty, px, net, value, filled="", oid=""):
    new = not os.path.exists(EXIT_LOG_CSV)
    with open(EXIT_LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EXIT_LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"time_utc": now_utc().isoformat(), "mode": EXIT_MODE, "market": row["market"],
                    "side": row["side"], "count": qty, "entry_price": row["price"], "sell_price": f"{px:.4f}",
                    "sell_net": f"{net:.4f}", "fair_value": f"{value:.4f}", "edge": f"{net - value:.4f}",
                    "pnl_if_sold": f"{pnl_if_sold(row, qty, net):.4f}", "filled": filled, "order_id": oid})


def manage_exits(rows, marks, client):
    """Sell open live bets the market now pays more for than they're worth. Returns rows changed."""
    if EXIT_MODE not in ("monitor", "live"):
        return 0
    try:
        positions = client.portfolio.positions().get("positions") or {}
    except Exception as e:
        print(f"[!] Selling early skipped this scan: couldn't read positions ({e})")
        return 0
    changed = 0
    for row in [r for r in rows if r.get("mode") == "live" and r.get("status") == "open"]:
        mark = marks.get(row["market"])
        if not mark or not mark["open"] or not mark["gap_ok"]:
            continue   # closed market or untrustworthy model: hold
        held = abs(float((positions.get(row["market"]) or {}).get("qtyAvailable") or 0))
        qty = int(min(float(row.get("count") or 0), held))
        sig = exit_signal(row["side"], mark)
        if qty < 1 or not sig:
            continue
        px, net, value = sig
        desc = (f"{row['side']} {qty}x {row['market']} @ ${px:.2f} (worth ${value:.2f}, "
                f"bought ${float(row['price']):.2f}, P&L about ${pnl_if_sold(row, qty, net):+.2f})")
        if EXIT_MODE == "monitor":
            log_exit(row, qty, px, net, value)
            print(f"    [exit monitor] would sell {desc}")
            continue
        try:
            oid, filled = sell_live(client, row, mark, qty)
        except Exception as e:
            # Like buys: if it reached the exchange, next scan's positions check sees the smaller size.
            print(f"[!] SELL ORDER ERROR on {row['market']}: {e}. Stopping sells this scan.")
            break
        log_exit(row, qty, px, net, value, f"{filled:g}", oid)
        if filled <= 0:
            print(f"    [sell] {row['market']}: not filled (price moved); holding")
            continue
        sold_pnl = pnl_if_sold(row, filled, net)
        record_exit(rows, row, filled, px, mark["rate"], oid)
        changed += 1
        print(f"    [SOLD] {row['side']} {filled:g} of {qty} x {row['market']} @ ${px:.2f} "
              f"(worth ${value:.2f}), P&L about ${sold_pnl:+.2f} (order {oid})")
        try:
            save_trades(rows)
        except Exception as e:
            print(f"[!!] SALE {oid} could not be saved to {TRADES_CSV} ({e}). "
                  f"Sells paused this scan. Close the file if it is open in Excel.")
            break
    return changed


# ---- One scan -------------------------------------------------------------------------
def scan(rows, take_snapshot, verbose=False):
    traded = {r["market"] for r in rows}
    signals, snapshots, problems = [], [], []
    events, marks = {}, {}   # marks: market -> latest model price and quotes, for selling early
    now = now_utc()
    today = now.date()
    days = [today + timedelta(days=d) for d in range(-1, DAYS_AHEAD + 1)]

    codes = city_codes()
    for i, code in enumerate(codes, 1):
        found = 0
        for day in days:
            slug = f"temp-{code}high-{day:%Y-%m-%d}"
            try:
                ev = get_event(slug)
            except Exception as e:
                problems.append(f"{slug}: {e}")
                continue
            events[slug] = ev
            if not ev or ev.get("closed") or not ev.get("active", True):
                continue

            brackets, bad = [], False
            for m in ev.get("markets", []):
                station, lo, hi = parse_bracket(m.get("description"))
                if lo is None and hi is None:
                    bad = True
                    break
                brackets.append({"m": m, "station": station, "lo": lo, "hi": hi,
                                 "bid": quote(m, "bestBidQuote"), "ask": quote(m, "bestAskQuote"),
                                 "open": m.get("status") == "MARKET_STATUS_OPEN" and not m.get("closed")})
            stations = {b["station"] for b in brackets}
            if bad or not brackets:
                problems.append(f"{slug}: couldn't read the bracket rules, skipped")
                continue
            if len(stations) != 1 or next(iter(stations)) not in STATIONS:
                problems.append(f"{slug}: settlement station {sorted(map(str, stations))} not in the model, skipped")
                continue
            station = next(iter(stations))
            offset, city = STATIONS[station]
            if not any(b["open"] for b in brackets):
                continue
            found += 1

            try:
                model, obs, fut = build_model(station, offset, day)
            except Exception as e:
                problems.append(f"{city}: weather data unavailable ({e})")
                continue
            if model is None:
                continue

            probs = [bracket_prob(model, b["lo"], b["hi"]) for b in brackets]
            total = sum(probs)
            if abs(total - 1.0) > SUM_TOLERANCE:
                problems.append(f"{slug}: brackets add to {total:.0%}, not 100% - skipped")
                continue
            mkt_c = market_center_f(brackets)
            gap_ok = mkt_c is None or abs(model.center() - mkt_c) <= MAX_CENTER_GAP_F
            if not gap_ok:
                problems.append(f"{slug}: model {model.center():.1f}°F vs market {mkt_c:.1f}°F - too far apart, no trades")
            day_start, _ = wm.climate_day_window_utc(day, offset)
            tradable_day = TRADE_SAME_DAY or now < day_start

            for b, p in zip(brackets, probs):
                m, bid, ask = b["m"], b["bid"], b["ask"]
                label = bracket_label(b["lo"], b["hi"])
                rate = float(m.get("feeCoefficient") or DEFAULT_FEE_RATE)
                marks[m["slug"]] = {"p_yes": p, "bid": bid, "ask": ask, "rate": rate,
                                    "open": b["open"], "gap_ok": gap_ok, "same_day": not now < day_start}
                if take_snapshot:
                    snapshots.append({"time_utc": now.isoformat(), "market": m["slug"], "bracket": label,
                                      "model_p_yes": f"{p:.4f}", "yes_bid": bid, "yes_ask": ask,
                                      "model_center_f": f"{model.center():.1f}",
                                      "obs_max_f": "" if obs is None else f"{obs:.1f}",
                                      "future_max_f": "" if fut is None else f"{fut:.1f}"})
                if m["slug"] in traded or not b["open"] or not gap_ok or not tradable_day:
                    continue
                best = best_trade(p, bid, ask, rate)
                if not best:
                    continue
                side, cost, fee, win_p, edge = best
                if edge < MIN_EDGE or edge > MAX_EDGE or not (MIN_PRICE <= cost <= MAX_PRICE):
                    continue
                signals.append({
                    "time_utc": now.isoformat(), "mode": "paper", "market": m["slug"], "event": slug,
                    "city": city, "station": station, "bracket": label, "side": side,
                    "price": f"{cost:.4f}", "fee": f"{fee:.4f}", "count": 1,
                    "model_win_prob": f"{win_p:.4f}", "edge": f"{edge:.4f}", "yes_bid": bid, "yes_ask": ask,
                    "model_center_f": f"{model.center():.1f}",
                    "obs_max_f": "" if obs is None else f"{obs:.1f}",
                    "future_max_f": "" if fut is None else f"{fut:.1f}",
                    "status": "open", "result": "", "pnl": "", "_rate": rate})
        if verbose:
            print(f"  [{i:>2}/{len(codes)}] {code:<5} {found} open event(s)", flush=True)

    rows.extend(signals)
    return signals, snapshots, problems, events, marks


def print_cycle(signals, problems, rows):
    stamp = now_utc().astimezone().strftime("%H:%M:%S")
    print(f"\n================ POLYMARKET WEATHER SCAN {stamp} ================")
    for s in signals:
        size = f"{s['count']}x " if s["mode"] == "live" else ""
        print(f"  [{s['mode'].upper()}] {s['side']:<3} {size}{s['city'][:22]:<22} {s['event'][-10:]} {s['bracket']:<12}"
              f" @ ${float(s['price']):.2f}  model {float(s['model_win_prob']):.0%}"
              f"  edge {float(s['edge'])*100:+.1f}¢  (model high {s['model_center_f']}°F)")
    if not signals:
        print("  No new trades this scan (nothing beat the edge threshold).")
    for p in problems[:15]:
        print(f"  [!] {p}")
    for mode in ("paper", "live"):
        mine = [r for r in rows if r.get("mode", "paper") == mode]
        if mode == "live" and not mine:
            continue
        settled = [r for r in mine if r.get("status") == "settled"]
        net = sum(float(r["pnl"]) for r in settled)
        open_n = sum(1 for r in mine if r.get("status") == "open")
        print(f"  {mode.capitalize()} record: {len(settled)} settled, net ${net:+.2f} | {open_n} open")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one scan and exit")
    ap.add_argument("--live", action="store_true", help="REAL MONEY: place orders within the LIVE_ limits")
    ap.add_argument("--check-live", action="store_true", help="test API keys and preview orders; places none")
    args = ap.parse_args()

    if args.check_live:
        check_live()
        return

    client = None
    print("=" * 66)
    if args.live:
        client = live_client()
        print("  POLYMARKET US WEATHER BOT  -  LIVE: REAL MONEY")
        print(f"  ${LIVE_DOLLARS_PER_TRADE:.2f}/trade, max ${LIVE_MAX_OPEN_DOLLARS:.0f} open, "
              f"max {LIVE_MAX_TRADES_PER_DAY} trades/day, stops at -${LIVE_KILL_LOSS:.0f}")
        print(f"  Buying power: ${buying_power(client):.2f}")
        print(f"  Selling early: {EXIT_MODE} (when the market pays {EXIT_MIN_EDGE * 100:.0f}¢+ more than a bet is worth)")
    else:
        print("  POLYMARKET US WEATHER BOT  -  PAPER MODE: no orders are placed")
    print("=" * 66)

    last_snapshot = 0.0
    first_scan = True
    while True:
        try:
            rows = load_trades()
            take_snap = time.time() - last_snapshot >= SNAPSHOT_EVERY_SECONDS
            print("[*] Scanning Polymarket US temperature markets...", flush=True)
            signals, snaps, problems, events, marks = scan(rows, take_snap, verbose=first_scan)
            first_scan = False
            settled_now = settle_trades(rows, events)
            if client is not None:
                # Selling early runs even when new buys are paused: it only reduces risk.
                settled_now += manage_exits(rows, marks, client)
                live_ok, why = live_allowed(rows)
                if live_ok:
                    # Never place real orders unless the trade record can be written.
                    try:
                        save_trades(rows)
                    except Exception as e:
                        live_ok, why = False, f"live paused: cannot write {TRADES_CSV} ({e}); close it if open in Excel"
                print(f"[*] {why}")
                if live_ok and signals:
                    go_live(signals, rows, client)
            if signals or settled_now:
                save_trades(rows)
            if snaps:
                append_snapshots(snaps)
                last_snapshot = time.time()
            print_cycle(signals, problems, rows)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[!] Scan failed: {e}. Retrying next cycle.")
        if args.once:
            return
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] Polymarket weather bot stopped.")
