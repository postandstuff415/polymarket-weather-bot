"""
pnl_report.py - Where does the market maker make and lose money? (read-only, places no orders)

    python pnl_report.py              all history Kalshi still has
    python pnl_report.py --since 2026-10-08     only markets first bought on/after this date

Per market: net = realized P&L - fees + (open position at today's bid - its cost).
Kalshi's realized P&L already includes settlement results.

Then grouped by:
  * sport / category          (NFL, NBA, commodities, weather...)
  * bet type                  (NFL receiving yards, NBA totals...)
  * entry timing              bought how long before the event started (in play = after)
  * entry price               what we paid per contract
  * day                       first-buy date, to compare before/after bot changes
"""

import argparse
from collections import defaultdict
from datetime import datetime, timezone

import requests

from auth import KalshiAuth

BASE = "https://external-api.kalshi.com/trade-api/v2"
PREFIX = "/trade-api/v2"

# Order matters: longer/more specific names first (WNBA before NBA, NCAAF before NFL...)
SPORTS = ["NCAAF", "NCAAB", "WNBA", "NBA", "NFL", "NHL", "MLB", "MLS", "EUROLEAGUE", "NBL",
          "KLEAGUE", "KBO", "VLEAGUE", "BRASILEIRO", "PERLIGA1", "ESOCCER", "AUTBSL", "NASCAR",
          "ATP", "WTA", "ITF", "UFC", "PGA", "F1"]
GROUPS = {"WTI": "Commodities", "SILVER": "Commodities", "GOLD": "Commodities",
          "NATGAS": "Commodities", "COPPER": "Commodities", "TEMP": "Weather (hourly temp)",
          "HIGH": "Weather (daily high)", "LOW": "Weather (daily low)", "TRUMP": "Politics",
          "RT": "Entertainment"}


def _get(auth, path, params=None):
    res = requests.get(BASE + path, headers=auth.get_headers("GET", PREFIX + path), params=params, timeout=20)
    res.raise_for_status()
    return res.json()


def _pages(auth, path, key, params):
    out, cursor = [], None
    for _ in range(100):
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        data = _get(auth, path, q)
        out.extend(data.get(key, []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts(raw):
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def classify(ticker):
    """(category, bet type) from the series ticker, e.g. KXNFLRECYDS -> ('NFL', 'RECYDS')."""
    series = ticker.split("-")[0]
    name = series[2:] if series.startswith("KX") else series
    for s in SPORTS:
        if name.startswith(s):
            return s, name[len(s):] or "GAME"
    for g, label in GROUPS.items():
        if name.startswith(g):
            return label, name
    return "Other", name


def timing_bucket(hours_before):
    if hours_before is None:
        return "no event time"
    if hours_before < 0:
        return "a) in play (after start)"
    if hours_before < 3:
        return "b) 0-3h before start"
    if hours_before < 24:
        return "c) 3-24h before start"
    if hours_before < 72:
        return "d) 1-3 days before"
    return "e) 3+ days before"


def price_bucket(p):
    for hi, label in ((0.15, "a) under 15c"), (0.30, "b) 15-30c"), (0.50, "c) 30-50c"),
                      (0.70, "d) 50-70c"), (0.85, "e) 70-85c")):
        if p < hi:
            return label
    return "f) 85c+"


def market_info(auth, tickers):
    """ticker -> market dict (occurrence time, current bid), 50 tickers per call."""
    info, tickers = {}, sorted(tickers)
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        try:
            for m in _get(auth, "/markets", {"tickers": ",".join(chunk), "limit": 1000}).get("markets", []):
                info[m["ticker"]] = m
        except Exception:
            pass
    return info


def table(title, rows, min_markets=1, limit=None):
    """rows: name -> list of per-market dicts."""
    print(f"\n  {title}")
    print(f"    {'':<34}{'mkts':>6}{'net $':>10}{'per mkt':>9}{'win%':>6}{'fees $':>8}")
    items = [(k, v) for k, v in rows.items() if len(v) >= min_markets]
    items.sort(key=lambda kv: kv[0] if title.startswith(("By entry", "By day")) else sum(m["net"] for m in kv[1]))
    if limit and len(items) > 2 * limit:
        items = items[:limit] + [("...", [])] + items[-limit:]
    for k, ms in items:
        if not ms:
            print(f"    {k}")
            continue
        net = sum(m["net"] for m in ms)
        won = sum(1 for m in ms if m["net"] > 0)
        fees = sum(m["fees"] for m in ms)
        print(f"    {str(k)[:34]:<34}{len(ms):>6}{net:>+10.2f}{net / len(ms) * 100:>+8.1f}c{won / len(ms):>6.0%}{fees:>8.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only markets first bought on/after this date (YYYY-MM-DD)")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None

    auth = KalshiAuth()
    print("Reading positions and fills from Kalshi (read-only)...")
    positions = _pages(auth, "/portfolio/positions", "market_positions", {"settlement_status": "all", "limit": 1000})
    fills = _pages(auth, "/portfolio/fills", "fills", {"limit": 1000})

    buys = defaultdict(list)   # ticker -> [(time, price, count)]
    for f in fills:
        side = f.get("book_side") or ("bid" if f.get("action") == "buy" else "ask")
        if side == "bid":
            buys[f["ticker"]].append((_ts(f.get("created_time")), _f(f.get("yes_price_dollars")), _f(f.get("count_fp"))))

    open_tickers = [p["ticker"] for p in positions if _f(p.get("position_fp")) != 0]
    info = market_info(auth, set(buys) | set(open_tickers))

    markets = []
    for p in positions:
        t = p["ticker"]
        if t not in buys:
            continue   # nothing bought here (e.g. opened by selling); not the market maker's pattern
        qty = _f(p.get("position_fp"))
        cost = _f(p.get("market_exposure_dollars"))
        m = info.get(t, {})
        open_val = qty * _f(m.get("yes_bid_dollars")) if qty > 0 else 0.0
        if qty < 0:
            open_val = abs(qty) * (1.0 - _f(m.get("yes_ask_dollars"), 1.0))
        net = _f(p.get("realized_pnl_dollars")) - _f(p.get("fees_paid_dollars")) + (open_val - cost if qty else 0.0)
        bl = sorted((b for b in buys[t] if b[0]), key=lambda b: b[0])
        first = bl[0][0] if bl else None
        if since and (first is None or first < since):
            continue
        n = sum(b[2] for b in bl) or 1.0
        avg_px = sum(b[1] * b[2] for b in bl) / n
        occ = _ts(m.get("occurrence_datetime"))
        hrs = (occ - first).total_seconds() / 3600.0 if occ and first else None
        cat, kind = classify(t)
        markets.append({"ticker": t, "net": net, "fees": _f(p.get("fees_paid_dollars")), "cat": cat,
                        "kind": f"{cat} {kind}", "timing": timing_bucket(hrs), "price": price_bucket(avg_px),
                        "day": first.strftime("%Y-%m-%d") if first else "?", "open": bool(qty)})

    if not markets:
        print("No markets with buys found.")
        return
    total = sum(m["net"] for m in markets)
    line = "=" * 74
    print("\n" + line)
    print(f"  MARKET MAKER P&L BY MARKET TYPE  ({len(markets)} markets"
          + (f", first bought since {args.since}" if args.since else "") + ")")
    print(line)
    print(f"  Net after fees: ${total:+,.2f}   | fees ${sum(m['fees'] for m in markets):,.2f}"
          f"   | still open: {sum(m['open'] for m in markets)} (valued at today's bid)")

    group = lambda key: {k: [m for m in markets if m[key] == k] for k in {m[key] for m in markets}}
    table("By sport / category", group("cat"))
    table("By bet type (worst and best 10, 3+ markets)", group("kind"), min_markets=3, limit=10)
    table("By entry timing (when we first bought vs event start)", group("timing"))
    table("By entry price", group("price"))
    table("By day (first buy)", group("day"))

    worst = sorted(markets, key=lambda m: m["net"])[:8]
    print("\n  Biggest single losers:")
    for m in worst:
        print(f"    {m['ticker'][:52]:<52}{m['net']:>+8.2f}  {m['timing'][3:]}")
    print("\n  Note: one market can mean several trades. 'win%' = share of markets that ended up positive.")
    print()


if __name__ == "__main__":
    main()
