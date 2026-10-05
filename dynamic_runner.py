import os
import requests
from auth import KalshiAuth
from strategy.order_manager import OrderManager

print("\n" + "=" * 65)
print("   KALSHI DYNAMIC OPEN MARKET SCANNER & PIPELINE   ")
print("=" * 65)

auth = KalshiAuth()
order_mgr = OrderManager(auth_client=auth, live=True)
base_url = "https://external-api.kalshi.com/trade-api/v2"

def get_active_open_markets(limit=200):
    path = "/trade-api/v2/markets"
    # mve_filter=exclude strips out the 0-volume multivariate sports parlays
    params = {
        "status": "open",
        "mve_filter": "exclude",
        "limit": limit
    }
    headers = auth.get_headers("GET", path)

    try:
        res = requests.get(f"{base_url}/markets", headers=headers, params=params, timeout=10)
        if res.status_code != 200:
            print(f"[!] API Error {res.status_code}: {res.text}")
            return []

        raw_markets = res.json().get("markets", [])
        active = []

        for m in raw_markets:
            ticker = m.get("ticker")
            
            # Kalshi V2 uses yes_bid_dollars / yes_ask_dollars
            bid_str = m.get("yes_bid_dollars") or m.get("yes_bid") or "0"
            ask_str = m.get("yes_ask_dollars") or m.get("yes_ask") or "0"
            
            yes_bid = float(bid_str)
            yes_ask = float(ask_str)

            # Keep markets that have an active order book
            if yes_bid > 0.0 or yes_ask > 0.0:
                active.append({
                    "ticker": ticker,
                    "title": m.get("title", ""),
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "close_time": m.get("close_time", "")
                })

        return active

    except Exception as e:
        print(f"[!] Network error: {e}")
        return []

# 1. Discover Real Open Markets
print("[*] Querying Kalshi V2 for liquid non-combo markets...")
tradable_markets = get_active_open_markets(limit=200)

print(f"\n[✓] Found {len(tradable_markets)} open markets with active order books:\n")
for idx, m in enumerate(tradable_markets[:10], start=1):
    bid_c = int(round(m['yes_bid'] * 100))
    ask_c = int(round(m['yes_ask'] * 100))
    print(f"  {idx:2d}. [{m['ticker']}]")
    print(f"      Title: {m['title']}")
    print(f"      Book:  Bid {bid_c}¢ / Ask {ask_c}¢ | Closes: {m['close_time']}\n")

if not tradable_markets:
    print("[-] No active books found.")
    exit(0)

# 2. Market-Making Evaluation Loop
print("=" * 65)
print("[*] EVALUATING SPREADS & PLACING ORDERS...")
print("=" * 65)

for market in tradable_markets[:5]:
    ticker = market["ticker"]
    yes_bid = market["yes_bid"]
    yes_ask = market["yes_ask"]

    if yes_bid > 0 and yes_ask > 0:
        spread = round(yes_ask - yes_bid, 4)
        spread_cents = int(round(spread * 100))
        bid_cents = int(round(yes_bid * 100))
        ask_cents = int(round(yes_ask * 100))

        print(f"\n[+] Ticker: {ticker}")
        print(f"    Spread: {spread_cents}¢ (Bid: {bid_cents}¢ | Ask: {ask_cents}¢)")

        # Market-making rule: place a passive maker bid 1¢ above market bid if spread is wide
        if spread_cents >= 4:
            target_bid = round(yes_bid + 0.01, 2)
            print(f"    [*] Placing Maker Quote @ {int(round(target_bid * 100))}¢...")

            order = order_mgr.build_maker_quote_order(
                ticker=ticker,
                target_side="yes",
                bid_price=target_bid,
                contract_count=1
            )
            order_mgr.dispatch_order(order)
        else:
            print("    [-] Spread < 4¢. Skipping.")

print("\n" + "=" * 65)
print("[✓] Pipeline Cycle Complete.")
print("=" * 65 + "\n")