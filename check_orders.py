"""
check_orders.py - What open orders does Kalshi say you have?

    python check_orders.py            list them (read-only)
    python check_orders.py --cancel   also try to cancel every one (both cancel endpoints)

Use this when the bot shows "CANCEL REJECT ... 404 not_found": it shows whether
those orders are real (still holding money on the book) or stale listings.
"""
import argparse
from collections import Counter

import requests
from auth import KalshiAuth

BASE = "https://external-api.kalshi.com/trade-api/v2"
auth = KalshiAuth()


def get(sub, params=None):
    r = requests.get(BASE + sub, headers=auth.get_headers("GET", "/trade-api/v2" + sub), params=params, timeout=15)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)


def delete(sub, params=None):
    r = requests.delete(BASE + sub, headers=auth.get_headers("DELETE", "/trade-api/v2" + sub), params=params, timeout=15)
    return r.status_code


def resting_orders():
    out, cursor = [], None
    for _ in range(20):
        params = {"status": "resting", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        code, data = get("/portfolio/orders", params)
        if code != 200:
            raise SystemExit(f"Could not list orders: {code} {str(data)[:200]}")
        out.extend(data.get("orders", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cancel", action="store_true")
    args = ap.parse_args()

    orders = resting_orders()
    print(f"\nKalshi lists {len(orders)} resting order(s).\n")
    by_day = Counter(str(o.get("created_time", "?"))[:10] for o in orders)
    for day, n in sorted(by_day.items()):
        print(f"  created {day}: {n}")
    print()
    for o in orders[:15]:
        price = o.get("price_dollars") or o.get("yes_price_dollars") or o.get("yes_price") or o.get("price")
        count = o.get("remaining_count_fp") or o.get("remaining_count") or o.get("count")
        print(f"  {str(o.get('order_id'))[:18]}  {str(o.get('created_time'))[:19]}  {o.get('ticker','?')[:40]:<40} "
              f"{o.get('side','?')}/{o.get('action','?')}  {count} @ {price}  status={o.get('status')}")
    if len(orders) > 15:
        print(f"  ... and {len(orders) - 15} more")

    if args.cancel:
        # 1) Kalshi's cancel-all: one call, every exchange shard, primary account
        code = delete("/portfolio/events/orders", {"subaccount": 0})
        print(f"\nCancel-all request: HTTP {code} ({'accepted' if code in (200, 204) else 'not accepted'})")
        left = resting_orders()
        # 2) Anything still listed: cancel one by one, telling Kalshi which market (shard) it is in
        if left:
            print(f"{len(left)} still listed; cancelling individually (progress every 10)...", flush=True)
            ok = gone = other = 0
            for i, o in enumerate(left, 1):
                try:
                    c = delete(f"/portfolio/events/orders/{o.get('order_id')}", {"market_ticker": o.get("ticker")})
                except Exception as e:
                    c = f"error: {e}"
                if c in (200, 204):
                    ok += 1
                elif c == 404:
                    gone += 1
                else:
                    other += 1
                    if other <= 3:
                        print(f"  {str(o.get('order_id'))[:18]}: {c}", flush=True)
                if i % 10 == 0 or i == len(left):
                    print(f"  {i}/{len(left)} done  (cancelled {ok}, not found {gone}, errors {other})", flush=True)
        print(f"\nKalshi now lists {len(resting_orders())} resting order(s).")
    print()


if __name__ == "__main__":
    main()
