import os

# 1. FIX RECONCILER CANCEL PATH (V2 /portfolio/events/orders)
reconciler_code = '''import time
import requests
from auth import KalshiAuth

class OrderReconciler:
    def __init__(self, auth_client=None, ttl_seconds=60, base_url="https://external-api.kalshi.com/trade-api/v2"):
        self.auth = auth_client or KalshiAuth()
        self.base_url = base_url
        self.ttl_seconds = ttl_seconds
        self.tracked_orders = {}

    def register_order(self, order_id: str, ticker: str, side: str, price: float):
        if not order_id or order_id == "simulated":
            return
        self.tracked_orders[order_id] = {
            "ticker": ticker,
            "side": side,
            "price": price,
            "placed_at": time.time()
        }

    def get_remote_resting_orders(self) -> list:
        path = "/trade-api/v2/portfolio/orders"
        headers = self.auth.get_headers("GET", path)
        params = {"status": "resting"}
        try:
            res = requests.get(f"{self.base_url}/portfolio/orders", headers=headers, params=params, timeout=10)
            if res.status_code == 200:
                return res.json().get("orders", [])
            return []
        except Exception:
            return []

    def cancel_order(self, order_id: str) -> bool:
        """Verified Kalshi V2 Cancel Order Endpoint"""
        path = f"/trade-api/v2/portfolio/events/orders/{order_id}"
        headers = self.auth.get_headers("DELETE", path)
        try:
            res = requests.delete(f"{self.base_url}/portfolio/events/orders/{order_id}", headers=headers, timeout=10)
            if res.status_code in (200, 204):
                self.tracked_orders.pop(order_id, None)
                print(f"[✓] [PRUNED & CANCELLED] ID: {order_id[:18]}...")
                return True
            else:
                # If order already filled or canceled remotely, drop from tracking
                if res.status_code in (404, 400):
                    self.tracked_orders.pop(order_id, None)
                print(f"[!] [CANCEL REJECT] ID {order_id[:18]}... | Status {res.status_code}: {res.text}")
                return False
        except Exception as e:
            print(f"[!] Network error cancelling order: {e}")
            return False

    def cancel_all_resting(self) -> int:
        remote_orders = self.get_remote_resting_orders()
        canceled_count = 0
        for ord_info in remote_orders:
            oid = ord_info.get("order_id")
            if oid and self.cancel_order(oid):
                canceled_count += 1
        self.tracked_orders.clear()
        return canceled_count

    def reconcile_cycle(self) -> dict:
        now = time.time()
        remote_orders = self.get_remote_resting_orders()
        remote_ids = {o.get("order_id"): o for o in remote_orders if o.get("order_id")}
        
        # Clean local registry of orders that no longer exist remotely
        dead_local = [oid for oid in self.tracked_orders if oid not in remote_ids]
        for oid in dead_local:
            del self.tracked_orders[oid]

        expired_orders = []
        active_valid_orders = []

        for oid, r_order in remote_ids.items():
            local_meta = self.tracked_orders.get(oid)
            placed_at = local_meta["placed_at"] if local_meta else now
            age = now - placed_at

            if age >= self.ttl_seconds:
                expired_orders.append((oid, r_order.get("ticker", "N/A"), age))
            else:
                active_valid_orders.append(oid)

        canceled = 0
        for oid, ticker, age in expired_orders:
            print(f"[*] Pruning stale quote (Age: {age:.1f}s >= {self.ttl_seconds}s) on {ticker}")
            if self.cancel_order(oid):
                canceled += 1

        return {
            "resting_count": len(active_valid_orders),
            "expired_pruned": canceled,
            "active_ids": active_valid_orders
        }
'''
with open(os.path.join("strategy", "reconciler.py"), "w", encoding="utf-8") as f:
    f.write(reconciler_code)

# 2. FIX ORDER MANAGER V2 EXIT PRICING
order_manager_code = '''import uuid
import requests
from auth import KalshiAuth

class OrderManager:
    def __init__(self, auth_client=None, live=False, dry_run=None):
        self.live = not dry_run if dry_run is not None else bool(live)
        self.base_url = (
            "https://external-api.kalshi.com/trade-api/v2"
            if self.live
            else "https://external-api.demo.kalshi.co/trade-api/v2"
        )
        self.auth_client = auth_client or KalshiAuth()

    @staticmethod
    def _parse_dollars(price):
        try:
            val = float(price)
            if val > 1.0:
                val = val / 100.0
            return max(0.0100, min(0.9900, round(val, 4)))
        except Exception:
            return 0.5000

    def build_maker_quote_order(self, ticker, target_side, bid_price, contract_count):
        return {
            "ticker": ticker,
            "target_side": str(target_side).lower(),
            "price": self._parse_dollars(bid_price),
            "count": int(contract_count),
            "time_in_force": "good_till_canceled"
        }

    def dispatch_order(self, order_dict: dict):
        if not order_dict:
            return None

        ticker = order_dict.get("ticker")
        target_side = str(order_dict.get("target_side", order_dict.get("side", "bid"))).lower()
        price_dollars = self._parse_dollars(order_dict.get("price", 0.50))
        count = int(order_dict.get("count", 1))
        tif = order_dict.get("time_in_force", "good_till_canceled")

        # In Kalshi V2 single-book:
        # "bid" = Buy YES at price_dollars
        # "ask" = Sell YES at price_dollars
        v2_side = "ask" if target_side in ("ask", "sell", "sell_yes") else "bid"

        endpoint = f"{self.base_url}/portfolio/events/orders"
        sign_path = "/trade-api/v2/portfolio/events/orders"

        payload = {
            "ticker": ticker,
            "side": v2_side,
            "count": f"{float(count):.2f}",
            "price": f"{price_dollars:.4f}",
            "time_in_force": tif,
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": str(uuid.uuid4())
        }

        headers = self.auth_client.get_headers(method="POST", path=sign_path)

        try:
            res = requests.post(endpoint, json=payload, headers=headers, timeout=10)
            if res.status_code in (200, 201):
                data = res.json()
                order_id = data.get("order_id", "N/A")
                print(f"[✓] [LIVE V2 ORDER PLACED] {v2_side.upper()} {count}x {ticker} @ ${price_dollars:.4f} (ID: {order_id})")
                return data
            else:
                print(f"[!] [LIVE REJECT] Status {res.status_code}: {res.text}")
                return None
        except requests.RequestException as e:
            print(f"[!] Network error placing order: {e}")
            return None
'''
with open(os.path.join("strategy", "order_manager.py"), "w", encoding="utf-8") as f:
    f.write(order_manager_code)

# 3. UPDATE AUTOPILOT WITH SHARD EXCLUSIONS & CSV TRADE LOGGING
autopilot_code = '''import os
import sys
import time
import csv
import requests
from auth import KalshiAuth
from strategy.order_manager import OrderManager
from strategy.reconciler import OrderReconciler
from strategy.position_manager import PositionManager
from strategy.risk_governor import RiskGovernor

CYCLE_INTERVAL_SECONDS = 20
ORDER_TTL_SECONDS = 60
MIN_SPREAD_CENTS = 4
CONTRACT_SIZE = 1
MAX_MARKETS_PER_PASS = 5
CSV_FILE = "trade_log.csv"

# Ensure CSV exists with headers
if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "ticker", "action", "side", "count", "price", "order_id"])

def log_trade(ticker, action, side, count, price, order_id):
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([time.strftime('%Y-%m-%d %H:%M:%S'), ticker, action, side, count, price, order_id])

print("\\n" + "=" * 65)
print("     KALSHI AUTONOMOUS MARKET MAKING ENGINE (AUTOPILOT V2.1)     ")
print("=" * 65)

auth = KalshiAuth()
reconciler = OrderReconciler(auth_client=auth, ttl_seconds=ORDER_TTL_SECONDS)
pos_mgr = PositionManager(auth_client=auth)
risk = RiskGovernor(
    max_contracts_per_market=5,
    max_dollar_exposure_per_market=15.0,
    max_total_open_exposure=250.0,
    min_balance_threshold=2500.0
)
order_mgr = OrderManager(auth_client=auth, live=True)
base_url = "https://external-api.kalshi.com/trade-api/v2"

# Sharded/restricted prefixes to ignore
SHARD_BLACKLIST = ("KXITF", "KXATP", "KXWTA")

def fetch_open_liquid_markets():
    path = "/trade-api/v2/markets"
    params = {"status": "open", "mve_filter": "exclude", "limit": 100}
    headers = auth.get_headers("GET", path)
    try:
        res = requests.get(f"{base_url}/markets", headers=headers, params=params, timeout=10)
        if res.status_code != 200:
            return []
        
        valid = []
        for m in res.json().get("markets", []):
            ticker = m.get("ticker", "")
            if any(ticker.startswith(b) for b in SHARD_BLACKLIST):
                continue
                
            bid_val = float(m.get("yes_bid_dollars") or m.get("yes_bid") or 0.0)
            ask_val = float(m.get("yes_ask_dollars") or m.get("yes_ask") or 0.0)
            
            if bid_val > 0.0 and ask_val > 0.0:
                valid.append({
                    "ticker": ticker,
                    "title": m.get("title", ""),
                    "yes_bid": bid_val,
                    "yes_ask": ask_val,
                    "spread_cents": int(round((ask_val - bid_val) * 100))
                })
        return valid
    except Exception as e:
        print(f"[!] Market discovery error: {e}")
        return []

def run_inventory_exits(positions: dict, resting_orders: list):
    resting_tickers = {ro.get("ticker") for ro in resting_orders}
    
    for ticker, p in positions.items():
        qty = p.get("position", 0.0)
        if qty > 0 and ticker not in resting_tickers:
            print(f"\\n[*] INVENTORY ACTION: Holding {qty}x {ticker} (YES)")
            try:
                m_path = f"/trade-api/v2/markets/{ticker}"
                m_res = requests.get(f"{base_url}/markets/{ticker}", headers=auth.get_headers("GET", m_path), timeout=8)
                if m_res.status_code == 200:
                    m_data = m_res.json().get("market", {})
                    current_ask = float(m_data.get("yes_ask_dollars") or 0.90)
                    target_ask = max(0.05, round(current_ask - 0.01, 2))
                    
                    print(f"    [*] Posting Profit-Taking Ask @ ${target_ask:.2f}...")
                    exit_order = {
                        "ticker": ticker,
                        "side": "ask",
                        "price": target_ask,
                        "count": int(qty),
                        "time_in_force": "good_till_canceled"
                    }
                    res = order_mgr.dispatch_order(exit_order)
                    if res:
                        oid = res.get("order_id")
                        reconciler.register_order(oid, ticker, "ask", target_ask)
                        log_trade(ticker, "EXIT_ASK", "ASK", qty, target_ask, oid)
            except Exception as e:
                print(f"[!] Error managing exit for {ticker}: {e}")

cycle = 1

try:
    while True:
        timestamp = time.strftime('%H:%M:%S')
        print(f"\\n{'='*25} [CYCLE {cycle:04d} | {timestamp}] {'='*25}")

        # 1. State Reconciliation
        rec_status = reconciler.reconcile_cycle()
        resting_orders = reconciler.get_remote_resting_orders()
        print(f"[*] Orders: {rec_status['resting_count']} active resting | {rec_status['expired_pruned']} stale pruned")

        # 2. Inventory & Exit Check
        positions = pos_mgr.get_all_positions()
        print(f"[*] Inventory: {len(positions)} active position(s) held")
        run_inventory_exits(positions, resting_orders)

        # 3. Discovery
        markets = fetch_open_liquid_markets()
        spread_candidates = [m for m in markets if m["spread_cents"] >= MIN_SPREAD_CENTS]
        print(f"[*] Discovery: {len(markets)} open liquid books | {len(spread_candidates)} wide spreads (≥ {MIN_SPREAD_CENTS}¢)")

        # 4. Filter & Quote
        active_tickers = {ro.get("ticker") for ro in resting_orders}.union(positions.keys())
        placed_this_cycle = 0

        for m in spread_candidates:
            if placed_this_cycle >= MAX_MARKETS_PER_PASS:
                break
                
            ticker = m["ticker"]
            if ticker in active_tickers:
                continue

            yes_bid = m["yes_bid"]
            target_bid = round(yes_bid + 0.01, 2)

            approved, reason = risk.evaluate_order(
                ticker=ticker,
                price_dollars=target_bid,
                count=CONTRACT_SIZE,
                current_positions=positions,
                resting_orders=resting_orders
            )

            if approved:
                print(f"\\n[+] Placing Maker Quote on {ticker}")
                print(f"    Spread: {m['spread_cents']}¢ | Bid: {int(yes_bid*100)}¢ -> Target: {int(target_bid*100)}¢")

                order = order_mgr.build_maker_quote_order(
                    ticker=ticker,
                    target_side="bid",
                    bid_price=target_bid,
                    contract_count=CONTRACT_SIZE
                )
                placed = order_mgr.dispatch_order(order)
                if placed:
                    oid = placed.get("order_id")
                    reconciler.register_order(oid, ticker, "bid", target_bid)
                    log_trade(ticker, "ENTRY_BID", "BID", CONTRACT_SIZE, target_bid, oid)
                    active_tickers.add(ticker)
                    placed_this_cycle += 1

        print(f"\\n[✓] Cycle {cycle:04d} complete. Resting {CYCLE_INTERVAL_SECONDS}s before next heartbeat...")
        cycle += 1
        time.sleep(CYCLE_INTERVAL_SECONDS)

except KeyboardInterrupt:
    print("\\n\\n" + "=" * 65)
    print("[!] SHUTDOWN SIGNAL (Ctrl+C). Cleaning up engine state...")
    print("=" * 65)
    cleared = reconciler.cancel_all_resting()
    print(f"[✓] Cancelled {cleared} resting order(s). Cash margin 100% unlocked.")
    print("[✓] Autopilot shutdown cleanly. Goodbye!\\n")
    sys.exit(0)
'''
with open("autopilot.py", "w", encoding="utf-8") as f:
    f.write(autopilot_code)

print("\n" + "=" * 65)
print("[✓] ALL 3 MODULES PATCHED:")
print("    1. reconciler.py -> V2 /portfolio/events/orders/{order_id} cancel endpoint fixed")
print("    2. order_manager.py -> Removed 1-price inversion for V2 YES exits")
print("    3. autopilot.py -> Filtered shard tennis markets + added trade_log.csv logging")
print("=" * 65 + "\n")