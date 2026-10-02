import os
import sys
import time
import csv
import requests
from auth import KalshiAuth
from strategy.order_manager import OrderManager
from strategy.reconciler import OrderReconciler
from strategy.position_manager import PositionManager
from strategy.risk_governor import RiskGovernor
from stage_reporter import compile_stage_report

START_TIMESTAMP = 1790908800  # Anchor: Oct 2, 2026

class LadderManager:
    @staticmethod
    def get_tier(current_balance: float = 2960.0):
        days_elapsed = max(0.0, (time.time() - START_TIMESTAMP) / 86400.0)
        
        if days_elapsed < 4.0:
            return {"stage": 1, "days": f"{days_elapsed:.1f}/4.0", "contract_size": 1, "max_market_contracts": 5, "max_market_dollars": 15.0, "max_portfolio_exposure": 250.0}
        elif days_elapsed < 11.0:
            return {"stage": 2, "days": f"{days_elapsed:.1f}/11.0", "contract_size": 3, "max_market_contracts": 9, "max_market_dollars": 30.0, "max_portfolio_exposure": 450.0}
        elif days_elapsed < 19.0:
            return {"stage": 3, "days": f"{days_elapsed:.1f}/19.0", "contract_size": 10, "max_market_contracts": 25, "max_market_dollars": 75.0, "max_portfolio_exposure": 900.0}
        elif days_elapsed < 29.0:
            return {"stage": 4, "days": f"{days_elapsed:.1f}/29.0", "contract_size": 25, "max_market_contracts": 60, "max_market_dollars": 175.0, "max_portfolio_exposure": 1500.0}
        else:
            size = 100 if current_balance >= 7000.0 else 40
            exp = 3000.0 if current_balance >= 7000.0 else 1500.0
            return {"stage": 5, "days": f"{days_elapsed:.1f}+", "contract_size": size, "max_market_contracts": size * 2, "max_market_dollars": 350.0, "max_portfolio_exposure": exp}

CYCLE_INTERVAL_SECONDS = 20
ORDER_TTL_SECONDS = 60
MIN_SPREAD_CENTS = 4
MAX_MARKETS_PER_PASS = 5
CSV_FILE = "trade_log.csv"

if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["timestamp", "ticker", "action", "side", "count", "price", "order_id"])

def log_trade(ticker, action, side, count, price, order_id):
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([time.strftime('%Y-%m-%d %H:%M:%S'), ticker, action, side, count, price, order_id])

print("\n" + "=" * 65)
print("     KALSHI AUTONOMOUS ENGINE (SELF-SCALING + REPORTING)     ")
print("=" * 65)

auth = KalshiAuth()
reconciler = OrderReconciler(auth_client=auth, ttl_seconds=ORDER_TTL_SECONDS)
pos_mgr = PositionManager(auth_client=auth)
risk = RiskGovernor()
order_mgr = OrderManager(auth_client=auth, live=True)
base_url = "https://external-api.kalshi.com/trade-api/v2"

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
        return []

def run_inventory_exits(positions: dict, resting_orders: list):
    resting_tickers = {ro.get("ticker") for ro in resting_orders}
    for ticker, p in positions.items():
        qty = p.get("position", 0.0)
        if qty > 0 and ticker not in resting_tickers:
            print(f"\n[*] INVENTORY ACTION: Holding {qty}x {ticker} (YES)")
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
                print(f"[!] Error managing exit: {e}")

cycle = 1
active_stage = 1

try:
    while True:
        timestamp = time.strftime('%H:%M:%S')
        positions = pos_mgr.get_all_positions()
        tier = LadderManager.get_tier(current_balance=2960.0)

        # Automated Milestone Detection & Reporting Hook
        if tier["stage"] > active_stage:
            print(f"\n[★] MILESTONE TRANSITION DETECTED: Stage {active_stage} -> Stage {tier['stage']}")
            compile_stage_report(active_stage, mark_completed=True)
            active_stage = tier["stage"]

        risk.max_contracts_per_market = tier["max_market_contracts"]
        risk.max_dollar_exposure_per_market = tier["max_market_dollars"]
        risk.max_total_open_exposure = tier["max_portfolio_exposure"]
        contract_size = tier["contract_size"]

        print(f"\n{'='*20} [CYCLE {cycle:04d} | {timestamp} | STAGE {tier['stage']} ({contract_size}x)] {'='*20}")
        print(f"[*] Ladder: Day {tier['days']} | Size: {contract_size}x | Market Cap: {tier['max_market_contracts']}x (${tier['max_market_dollars']:.0f}) | Global Cap: ${tier['max_portfolio_exposure']:.0f}")

        rec_status = reconciler.reconcile_cycle()
        resting_orders = reconciler.get_remote_resting_orders()
        print(f"[*] Orders: {rec_status['resting_count']} active resting | {rec_status['expired_pruned']} stale pruned")

        print(f"[*] Inventory: {len(positions)} active position(s) held")
        run_inventory_exits(positions, resting_orders)

        markets = fetch_open_liquid_markets()
        spread_candidates = [m for m in markets if m["spread_cents"] >= MIN_SPREAD_CENTS]
        print(f"[*] Discovery: {len(markets)} open liquid books | {len(spread_candidates)} wide spreads (≥ {MIN_SPREAD_CENTS}¢)")

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
                count=contract_size,
                current_positions=positions,
                resting_orders=resting_orders
            )

            if approved:
                print(f"\n[+] Placing Maker Quote on {ticker}")
                print(f"    Spread: {m['spread_cents']}¢ | Target Bid: {int(target_bid*100)}¢ ({contract_size}x)")
                order = order_mgr.build_maker_quote_order(ticker=ticker, target_side="bid", bid_price=target_bid, contract_count=contract_size)
                placed = order_mgr.dispatch_order(order)
                if placed:
                    oid = placed.get("order_id")
                    reconciler.register_order(oid, ticker, "bid", target_bid)
                    log_trade(ticker, "ENTRY_BID", "BID", contract_size, target_bid, oid)
                    active_tickers.add(ticker)
                    placed_this_cycle += 1

        print(f"\n[✓] Cycle {cycle:04d} complete. Resting {CYCLE_INTERVAL_SECONDS}s before next heartbeat...")
        cycle += 1
        time.sleep(CYCLE_INTERVAL_SECONDS)

except KeyboardInterrupt:
    print("\n\n" + "=" * 65)
    print("[!] SHUTDOWN SIGNAL (Ctrl+C). Cleaning up engine state...")
    print("=" * 65)
    cleared = reconciler.cancel_all_resting()
    print(f"[✓] Cancelled {cleared} resting order(s). Cash margin 100% unlocked.")
    print("[*] Generating interim stage report before shutdown...")
    compile_stage_report(active_stage, mark_completed=False)
    print("[✓] Autopilot shutdown cleanly. Goodbye!\n")
    sys.exit(0)
