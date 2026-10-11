"""
weather_research.py - Who wins in Kalshi daily-high weather markets? (read-only, places no orders)

    python weather_research.py               download (cached) + analyze
    python weather_research.py --refresh     re-download trades
    python weather_research.py --lows        same analysis for the daily-LOW markets (KXLOWT...)

Same method as Becker's "Microstructure of Wealth Transfer" (72M Kalshi trades), restricted
to the daily-high temperature markets weather_bot.py trades. For every settled market in
Kalshi's live data set, every trade: price, which side the TAKER bought (the one hitting
the order book), and the result. The MAKER (resting limit order) is on the other side.

Per contract:  taker excess return = (1 if taker's side won else 0) - price paid
               maker excess return = -(taker excess)        (before fees)
Taker fee (Kalshi): 7% x price x (1 - price) per contract.

Cached raw data: research_data/weather_trades.csv.gz (lows: weather_low_trades.csv.gz)
"""

import argparse
import csv
import gzip
import math
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

import weather_model as wm
from auth import KalshiAuth

BASE = "https://external-api.kalshi.com/trade-api/v2"
PREFIX = "/trade-api/v2"
DATA_DIR = "research_data"
TRADES_FILE = os.path.join(DATA_DIR, "weather_trades.csv.gz")
LOW_TRADES_FILE = os.path.join(DATA_DIR, "weather_low_trades.csv.gz")
SERIES = list(wm.CITIES)          # --lows switches these to the daily-low series
KIND = "HIGH"
FIELDS = ["series", "ticker", "result", "time", "yes_price", "taker_side", "count"]

_auth = KalshiAuth()


def _get(path, params=None, tries=5):
    for i in range(tries):
        try:
            res = requests.get(BASE + path, headers=_auth.get_headers("GET", PREFIX + path),
                               params=params, timeout=30)
            if res.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            res.raise_for_status()
            return res.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))
    return {}


def settled_markets(series):
    out, cursor = [], None
    for _ in range(50):
        q = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        d = _get("/markets", q)
        out += [m for m in d.get("markets", []) if m.get("result") in ("yes", "no")]
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def market_trades(market):
    rows, cursor = [], None
    for _ in range(500):
        q = {"ticker": market["ticker"], "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        d = _get("/markets/trades", q)
        for t in d.get("trades", []):
            side = t.get("taker_outcome_side") or t.get("taker_side")
            if side not in ("yes", "no"):
                continue
            rows.append([market["ticker"].split("-")[0], market["ticker"], market["result"], t["created_time"],
                         t.get("yes_price_dollars"), side, t.get("count_fp")])
        cursor = d.get("cursor")
        if not cursor:
            break
    return rows


def download():
    os.makedirs(DATA_DIR, exist_ok=True)
    markets = []
    for series in SERIES:
        ms = settled_markets(series)
        markets += ms
        print(f"  {series:<14} {len(ms):>4} settled markets")
    print(f"Downloading trades for {len(markets)} markets (cached to {TRADES_FILE})...")
    n = 0
    with gzip.open(TRADES_FILE, "wt", newline="", encoding="utf-8") as f, ThreadPoolExecutor(max_workers=3) as ex:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for i, rows in enumerate(ex.map(market_trades, markets), 1):
            w.writerows(rows)
            n += len(rows)
            if i % 500 == 0:
                print(f"    {i}/{len(markets)} markets, {n:,} trades")
    print(f"  done: {n:,} trades")


# ---- analysis -----------------------------------------------------------------
def bucket(p, edges=(0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95)):
    lo = 0.0
    for e in edges:
        if p < e:
            return f"{int(lo*100):>2}-{int(e*100):<2}c"
        lo = e
    return f"{int(lo*100):>2}-99c"


def timing(series, ticker, t):
    """When the trade happened vs the climate day the market is about (local standard time)."""
    city = wm.city_for(series)
    day = wm.event_date(ticker.rsplit("-", 1)[0])
    if not city or not day:
        return "?"
    start, _ = wm.climate_day_window_utc(day, city[1])
    h = (t - start).total_seconds() / 3600.0
    if h < -24:
        return "a) 2+ days before"
    if h < 0:
        return "b) day before"
    if h < 12:
        return "c) same day, before noon"
    return "d) same day, after noon"


class Acc:
    __slots__ = ("c", "cost", "pnl", "fee", "wins", "n")

    def __init__(self):
        self.c = self.cost = self.pnl = self.fee = self.wins = 0.0
        self.n = 0

    def add(self, count, price, won, fee):
        self.c += count
        self.cost += count * price
        self.pnl += count * ((1.0 if won else 0.0) - price)
        self.fee += fee
        self.wins += count * (1.0 if won else 0.0)
        self.n += 1


def load():
    with gzip.open(TRADES_FILE, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                yield (r["series"], r["ticker"], r["result"],
                       datetime.fromisoformat(r["time"].replace("Z", "+00:00")),
                       float(r["yes_price"]), r["taker_side"], float(r["count"]))
            except (ValueError, KeyError):
                continue


def analyze():
    calib = defaultdict(lambda: [0.0, 0.0, 0.0])      # yes-price bucket -> [contracts, yes wins, sum price]
    taker = defaultdict(Acc)                          # (side, own-price bucket) -> taker results
    maker = defaultdict(Acc)                          # (side maker holds, own-price bucket)
    by_time = defaultdict(Acc)                        # (timing, taker side)
    by_time_m = defaultdict(Acc)
    by_city = defaultdict(lambda: [Acc(), Acc()])     # series -> [taker, maker]
    total_t, total_m = Acc(), Acc()
    first = last = None
    for series, ticker, result, t, yp, side, cnt in load():
        if not (0.0 < yp < 1.0) or cnt <= 0:
            continue
        first = t if first is None or t < first else first
        last = t if last is None or t > last else last
        b = bucket(yp)
        calib[b][0] += cnt
        calib[b][1] += cnt * (1.0 if result == "yes" else 0.0)
        calib[b][2] += cnt * yp
        t_price = yp if side == "yes" else 1.0 - yp
        t_won = side == result
        fee = wm.TAKER_FEE_RATE * cnt * yp * (1.0 - yp)
        m_side = "no" if side == "yes" else "yes"
        m_price = 1.0 - t_price
        for acc in (taker[(side, bucket(t_price))], by_time[(timing(series, ticker, t), side)],
                    by_city[series][0], total_t):
            acc.add(cnt, t_price, t_won, fee)
        for acc in (maker[(m_side, bucket(m_price))], by_time_m[(timing(series, ticker, t), m_side)],
                    by_city[series][1], total_m):
            acc.add(cnt, m_price, not t_won, 0.0)

    if first is None:
        print("No trades in the cache.")
        return
    line = "=" * 92
    print("\n" + line)
    print(f"  KALSHI DAILY-{KIND} WEATHER MARKETS: {total_t.n:,} trades, {total_t.c:,.0f} contracts, "
          f"{first:%Y-%m-%d} to {last:%Y-%m-%d}")
    print(line)
    tr = lambda a: a.pnl / a.cost if a.cost else 0.0
    print(f"  All takers: {tr(total_t):+.2%} return before fees, {(total_t.pnl - total_t.fee) / total_t.cost:+.2%} after "
          f"| all makers: {tr(total_m):+.2%} (before any maker fees)")
    print(f"  Taker P&L ${total_t.pnl:+,.0f} before fees, fees ${total_t.fee:,.0f}  (makers: ${total_m.pnl:+,.0f})")

    print("\n  1) CALIBRATION: does a YES priced at X win X% of the time?")
    print(f"     {'YES price':<10}{'contracts':>13}{'avg price':>11}{'YES won':>10}{'gap':>8}")
    for b in sorted(calib):
        c, w, s = calib[b]
        print(f"     {b:<10}{c:>13,.0f}{s / c:>11.1%}{w / c:>10.1%}{(w - s) / c * 100:>+7.1f}c")

    def side_table(title, data):
        print(f"\n  {title}")
        print(f"     {'side':<5}{'price paid':<12}{'contracts':>13}{'won':>8}{'return':>9}{'after fee':>11}{'per ct':>9}")
        for (side, b) in sorted(data, key=lambda k: (k[0] != "yes", k[1])):
            a = data[(side, b)]
            if a.c < 1000:
                continue
            after = (a.pnl - a.fee) / a.cost if a.fee else tr(a)
            print(f"     {side.upper():<5}{b:<12}{a.c:>13,.0f}{a.wins / a.c:>8.1%}{tr(a):>+9.2%}"
                  f"{after:>+11.2%}{a.pnl / a.c * 100:>+8.2f}c")

    side_table("2) TAKERS (bought at the ask): return by side and price paid", taker)
    side_table("3) MAKERS (resting limit orders): return by side held and price", maker)

    print("\n  4) TIMING: taker vs maker by when the trade happened (relative to the local climate day)")
    print(f"     {'when':<28}{'side':<5}{'contracts':>13}{'taker ret':>11}{'after fee':>11}{'maker ret':>11}")
    for (when, side) in sorted(by_time):
        a = by_time[(when, side)]
        m = by_time_m.get((when, "no" if side == "yes" else "yes"))
        if a.c < 1000:
            continue
        print(f"     {when:<28}{side.upper():<5}{a.c:>13,.0f}{tr(a):>+11.2%}{(a.pnl - a.fee) / a.cost:>+11.2%}"
              f"{tr(m) if m else 0:>+11.2%}")

    print("\n  5) BY CITY: taker return after fees (negative = takers lose to makers)")
    print(f"     {'city':<28}{'contracts':>13}{'taker after fee':>17}{'maker ret':>11}")
    for s, (a, m) in sorted(by_city.items(), key=lambda kv: (kv[1][0].pnl - kv[1][0].fee) / max(kv[1][0].cost, 1)):
        print(f"     {wm.city_for(s)[2][:28]:<28}{a.c:>13,.0f}{(a.pnl - a.fee) / a.cost:>+17.2%}{tr(m):>+11.2%}")
    print("\n  Note: averages over ALL traders. A model that picks its trades can beat (or trail) these.")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--lows", action="store_true", help="daily-low markets instead of daily highs")
    args = ap.parse_args()
    if args.lows:
        global TRADES_FILE, SERIES, KIND
        TRADES_FILE, SERIES, KIND = LOW_TRADES_FILE, list(wm.LOW_SERIES), "LOW"
    if args.refresh or not os.path.exists(TRADES_FILE):
        download()
    analyze()


if __name__ == "__main__":
    main()
