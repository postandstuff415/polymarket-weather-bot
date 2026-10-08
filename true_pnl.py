"""
true_pnl.py - What is the bot ACTUALLY making? (read-only, places no orders)

Run from the bot folder in PowerShell:
    python true_pnl.py
    python true_pnl.py --start 2960        (skip the prompt)

It answers one question: how much is the account worth right now compared with
what you started with? It uses only numbers from your Kalshi account:

  cash balance
+ open positions, valued at what you could sell them for right now
= account value
- starting balance
= TRUE NET (includes fees, losing settlements, and everything else)

If you deposited or withdrew money since the start, adjust --start by that amount.
"""

import argparse
import sys

import requests

from auth import KalshiAuth

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
API_PREFIX = "/trade-api/v2"


def _get(auth, path, params=None):
    headers = auth.get_headers("GET", API_PREFIX + path)
    res = requests.get(BASE_URL + path, headers=headers, params=params, timeout=15)
    if res.status_code != 200:
        raise RuntimeError(f"GET {path} -> HTTP {res.status_code}: {res.text[:300]}")
    return res.json()


def _dollars(obj, *keys):
    """Read a money field that may come as '<name>_dollars' (dollars) or '<name>' (cents)."""
    for k in keys:
        if obj.get(f"{k}_dollars") is not None:
            return float(obj[f"{k}_dollars"])
    for k in keys:
        if obj.get(k) is not None:
            return float(obj[k]) / 100.0
    return 0.0


def _num(obj, *keys):
    for k in keys:
        if obj.get(k) is not None:
            return float(obj[k])
    return 0.0


def get_cash(auth):
    data = _get(auth, "/portfolio/balance")
    if data.get("balance_dollars") is not None:
        return float(data["balance_dollars"]), data
    if data.get("balance") is not None:
        return float(data["balance"]) / 100.0, data
    raise RuntimeError(f"Could not read balance from response: {data}")


def get_all_positions(auth):
    out, cursor = [], None
    for _ in range(50):  # pagination safety cap
        params = {"settlement_status": "all", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get(auth, "/portfolio/positions", params)
        out.extend(data.get("market_positions", []) or data.get("positions", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def mark_position(auth, ticker, qty):
    """Value an open position at the price you could sell it for right now."""
    try:
        m = _get(auth, f"/markets/{ticker}").get("market", {})
    except Exception:
        return None, None
    yes_bid = _dollars(m, "yes_bid")
    yes_ask = _dollars(m, "yes_ask")
    if qty > 0:
        return qty * yes_bid, yes_bid             # sell YES into the bid
    no_bid = (1.0 - yes_ask) if yes_ask else 0.0  # holding NO: sell NO = buy YES at the ask
    return abs(qty) * no_bid, no_bid


def main():
    ap = argparse.ArgumentParser(description="True net P&L for your Kalshi account (read-only).")
    ap.add_argument("--start", type=float, help="Starting balance in dollars (net of deposits/withdrawals)")
    args = ap.parse_args()

    start = args.start
    if start is None:
        raw = input("Starting balance in dollars when the bot began (e.g. 2960): ").strip()
        try:
            start = float(raw.replace("$", "").replace(",", ""))
        except ValueError:
            sys.exit("Please enter a number, e.g. 2960")

    auth = KalshiAuth()
    cash, _ = get_cash(auth)
    positions = get_all_positions(auth)

    realized = fees = 0.0
    open_value = open_cost = 0.0
    open_rows, unpriced = [], []

    for p in positions:
        realized += _dollars(p, "realized_pnl")
        fees += _dollars(p, "fees_paid")
        qty = _num(p, "position_fp", "position")
        if qty == 0:
            continue
        ticker = p.get("ticker", "?")
        cost = _dollars(p, "market_exposure")
        value, px = mark_position(auth, ticker, qty)
        if value is None:
            unpriced.append(ticker)
            value, px = cost, None  # fall back to cost so the total isn't distorted
        open_value += value
        open_cost += cost
        open_rows.append((ticker, qty, cost, value, px))

    account_value = cash + open_value
    true_net = account_value - start

    line = "=" * 64
    print("\n" + line)
    print("  TRUE NET P&L  (from your Kalshi account, not the bot's logs)")
    print(line)
    print(f"  Cash balance:                    ${cash:>10,.2f}")
    print(f"  Open positions (sell value now): ${open_value:>10,.2f}")
    print(f"  Account value:                   ${account_value:>10,.2f}")
    print(f"  Starting balance:                ${start:>10,.2f}")
    print("  " + "-" * 46)
    sign = "+" if true_net >= 0 else "-"
    pct = f"   ({true_net / start * 100:+.2f}%)" if start else ""
    print(f"  TRUE NET:                       {sign}${abs(true_net):>10,.2f}{pct}")
    print(line)
    print("  Breakdown reported by Kalshi (all markets, settled + open):")
    print(f"    Realized P&L:                  ${realized:>10,.2f}")
    print(f"    Fees paid:                     ${fees:>10,.2f}")
    print(f"    Open positions: cost ${open_cost:,.2f} -> worth ${open_value:,.2f} now"
          f" (unrealized {open_value - open_cost:+,.2f})")
    print("    (If these don't add up to TRUE NET exactly, trust TRUE NET.)")

    if open_rows:
        print("\n  Open positions:")
        print(f"    {'Ticker':<38}{'Qty':>6}{'Cost':>10}{'Worth':>10}")
        for t, q, c, v, _ in sorted(open_rows, key=lambda r: r[3] - r[2]):
            print(f"    {t[:38]:<38}{q:>6.0f}{c:>10.2f}{v:>10.2f}")
    if unpriced:
        print(f"\n  [!] Could not price {len(unpriced)} position(s); counted at cost: {', '.join(unpriced[:5])}")
    print()


if __name__ == "__main__":
    main()
