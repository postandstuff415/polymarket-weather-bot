"""
polymarket_weather_bot.py - Polymarket US daily-high temperature bot. PAPER ONLY:
it places no orders and needs no API keys (market data is public).

    python polymarket_weather_bot.py            scan every 5 minutes, record paper trades
    python polymarket_weather_bot.py --once     one scan, print the table, exit
    python polymarket_report.py                 how the paper trades are doing

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

Live trading is not built yet. It needs a verified Polymarket US account and API
keys (polymarket.us/developer), and should wait until the paper record shows an edge.
"""

import argparse
import csv
import json
import os
import re
import time
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_EVEN

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
# =============================================================================

GATEWAY = "https://gateway.polymarket.us/v1"   # public market data, no key needed
MIN_REQUEST_GAP = 1.1                          # public API allows 60 requests/minute
KNOWN_CITY_CODES = ["nyc", "mdw", "mia", "lax", "sfo"]   # event slugs: temp-<code>high-YYYY-MM-DD
EVENT_SLUG = re.compile(r"^temp-([a-z]+)high-(\d{4})-(\d{2})-(\d{2})$")

# Settlement station -> (standard-time UTC offset, name), from the Kalshi city table
STATIONS = {st: (offset, name) for st, offset, name in wm.CITIES.values()}

TRADES_CSV = "polymarket_paper_trades.csv"
SNAPSHOT_CSV = "polymarket_model_log.csv"
TRADE_FIELDS = ["time_utc", "mode", "market", "event", "city", "station", "bracket", "side", "price",
                "fee", "count", "model_win_prob", "edge", "yes_bid", "yes_ask", "model_center_f",
                "obs_max_f", "future_max_f", "status", "result", "pnl"]
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
def load_trades():
    if not os.path.exists(TRADES_CSV):
        return []
    with open(TRADES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_trades(rows):
    tmp = TRADES_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TRADE_FIELDS})
    os.replace(tmp, TRADES_CSV)


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


# ---- One scan -------------------------------------------------------------------------
def scan(rows, take_snapshot, verbose=False):
    traded = {r["market"] for r in rows}
    signals, snapshots, problems = [], [], []
    events = {}
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
                if take_snapshot:
                    snapshots.append({"time_utc": now.isoformat(), "market": m["slug"], "bracket": label,
                                      "model_p_yes": f"{p:.4f}", "yes_bid": bid, "yes_ask": ask,
                                      "model_center_f": f"{model.center():.1f}",
                                      "obs_max_f": "" if obs is None else f"{obs:.1f}",
                                      "future_max_f": "" if fut is None else f"{fut:.1f}"})
                if m["slug"] in traded or not b["open"] or not gap_ok or not tradable_day:
                    continue
                best = best_trade(p, bid, ask, float(m.get("feeCoefficient") or DEFAULT_FEE_RATE))
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
                    "status": "open", "result": "", "pnl": ""})
        if verbose:
            print(f"  [{i:>2}/{len(codes)}] {code:<5} {found} open event(s)", flush=True)

    rows.extend(signals)
    return signals, snapshots, problems, events


def print_cycle(signals, problems, rows):
    stamp = now_utc().astimezone().strftime("%H:%M:%S")
    print(f"\n================ POLYMARKET WEATHER SCAN {stamp} ================")
    for s in signals:
        print(f"  [PAPER] {s['side']:<3} {s['city'][:22]:<22} {s['event'][-10:]} {s['bracket']:<12}"
              f" @ ${float(s['price']):.2f}  model {float(s['model_win_prob']):.0%}"
              f"  edge {float(s['edge'])*100:+.1f}¢  (model high {s['model_center_f']}°F)")
    if not signals:
        print("  No new trades this scan (nothing beat the edge threshold).")
    for p in problems[:15]:
        print(f"  [!] {p}")
    settled = [r for r in rows if r.get("status") == "settled"]
    net = sum(float(r["pnl"]) for r in settled)
    open_n = sum(1 for r in rows if r.get("status") == "open")
    print(f"  Paper record: {len(settled)} settled, net ${net:+.2f} | {open_n} open")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one scan and exit")
    args = ap.parse_args()

    print("=" * 66)
    print("  POLYMARKET US WEATHER BOT  -  PAPER MODE: no orders are placed")
    print("=" * 66)

    last_snapshot = 0.0
    first_scan = True
    while True:
        try:
            rows = load_trades()
            take_snap = time.time() - last_snapshot >= SNAPSHOT_EVERY_SECONDS
            print("[*] Scanning Polymarket US temperature markets...", flush=True)
            signals, snaps, problems, events = scan(rows, take_snap, verbose=first_scan)
            first_scan = False
            settled_now = settle_trades(rows, events)
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
