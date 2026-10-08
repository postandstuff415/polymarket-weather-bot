import os
import sys
import time
import csv
from datetime import datetime, timezone

import requests
from auth import KalshiAuth
from strategy.order_manager import OrderManager
from strategy.reconciler import OrderReconciler
from strategy.position_manager import PositionManager
from strategy.risk_governor import RiskGovernor

# =============================================================================
# SAFETY SETTINGS (Oct 8 review) - change these on purpose, not by the calendar
# =============================================================================
# The ladder used to move up a stage automatically every few days, whether the
# bot was making money or not (Stage 3 = 10x contracts, $900 exposure, on Oct 12).
# It now stays on the stage below until you change this number yourself.
MANUAL_STAGE = 2          # 3 contracts per order

# Loss stop: if cash + open positions (at cost) drops this far below the
# starting balance, the bot stops opening new positions (exits keep working).
# START_BALANCE = None means "use the balance when the bot starts" - set it to
# your real starting number so restarts don't reset the baseline.
START_BALANCE = 3877.00       # test baseline set Oct 8
MAX_LOSS_DOLLARS = 387.70     # 10% of 3877

# Only open positions priced between these (tails carry big settlement risk)
MIN_ENTRY_PRICE = 0.15
MAX_ENTRY_PRICE = 0.85
# Don't open positions in markets that close within this many hours
MIN_HOURS_TO_CLOSE = 6.0
# ...and never in markets that close more than 7 days out (no long bets)
MAX_HOURS_TO_CLOSE = 168.0
# Spread risk across at most this many markets at once (positions + open orders)
MAX_ACTIVE_MARKETS = 75
# How many pages of 100 markets to scan each cycle for candidates
MARKET_SCAN_PAGES = 10
# =============================================================================


class LadderManager:
    TIERS = {
        1: {"contract_size": 1, "max_market_contracts": 5, "max_market_dollars": 15.0, "max_portfolio_exposure": 250.0},
        2: {"contract_size": 3, "max_market_contracts": 9, "max_market_dollars": 30.0, "max_portfolio_exposure": 450.0},
        3: {"contract_size": 10, "max_market_contracts": 25, "max_market_dollars": 75.0, "max_portfolio_exposure": 900.0},
        4: {"contract_size": 25, "max_market_contracts": 60, "max_market_dollars": 175.0, "max_portfolio_exposure": 1500.0},
        5: {"contract_size": 40, "max_market_contracts": 80, "max_market_dollars": 350.0, "max_portfolio_exposure": 1500.0},
    }

    @staticmethod
    def get_tier(stage: int = MANUAL_STAGE):
        tier = dict(LadderManager.TIERS.get(stage, LadderManager.TIERS[1]))
        tier["stage"] = stage
        tier["days"] = "manual"
        return tier


CYCLE_INTERVAL_SECONDS = 20
ORDER_TTL_SECONDS = 60
MIN_SPREAD_CENTS = 4
# Skip near-empty books: a spread wider than this means almost nobody is trading there
MAX_SPREAD_CENTS = 20
MAX_MARKETS_PER_PASS = 5
CSV_FILE = "trade_log.csv"

# Exit-loop guards (Oct 8 patch)
# Market responses report live markets as "active"; the list filter uses "open".
TRADABLE_STATUSES = {"active", "open"}
EXIT_TIME_BUDGET_SECONDS = 5.0     # exits never hold up quoting longer than this per cycle
closed_tickers = set()             # markets seen closed; they never reopen, so skip forever

if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["timestamp", "ticker", "action", "side", "count", "price", "order_id"])


def log_trade(ticker, action, side, count, price, order_id):
    # NOTE: this logs orders PLACED, not orders FILLED. Use true_pnl.py for real results.
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([time.strftime('%Y-%m-%d %H:%M:%S'), ticker, action, side, count, price, order_id])


print("\n" + "=" * 65)
print("     KALSHI AUTONOMOUS ENGINE (MANUAL STAGE + LOSS STOP)     ")
print("=" * 65)

auth = KalshiAuth()
reconciler = OrderReconciler(auth_client=auth, ttl_seconds=ORDER_TTL_SECONDS)
pos_mgr = PositionManager(auth_client=auth)
risk = RiskGovernor()
order_mgr = OrderManager(auth_client=auth, live=True)
base_url = "https://external-api.kalshi.com/trade-api/v2"

SHARD_BLACKLIST = ("KXITF", "KXATP", "KXWTA")
# Daily high/low temperature markets are traded only by weather_bot.py
WEATHER_PREFIXES = ("KXHIGH", "KXLOW")


def fetch_cash_balance():
    """Real cash balance in dollars from Kalshi, or None if the call fails."""
    path = "/trade-api/v2/portfolio/balance"
    try:
        res = requests.get(f"{base_url}/portfolio/balance", headers=auth.get_headers("GET", path), timeout=10)
        if res.status_code != 200:
            print(f"[!] Balance API Status {res.status_code}: {res.text[:200]}")
            return None
        data = res.json()
        if data.get("balance_dollars") is not None:
            return float(data["balance_dollars"])
        if data.get("balance") is not None:
            return float(data["balance"]) / 100.0
        print(f"[!] Could not read balance from: {data}")
        return None
    except Exception as e:
        print(f"[!] Balance network error: {e}")
        return None


def hours_to_close(market: dict):
    raw = market.get("close_time") or market.get("expected_expiration_time")
    if not raw:
        return None
    try:
        close = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return (close - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except Exception:
        return None


def fetch_open_liquid_markets():
    path = "/trade-api/v2/markets"
    valid, cursor = [], None
    try:
        raw_markets = []
        for _ in range(MARKET_SCAN_PAGES):
            params = {"status": "open", "mve_filter": "exclude", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            res = requests.get(f"{base_url}/markets", headers=auth.get_headers("GET", path), params=params, timeout=10)
            if res.status_code != 200:
                break
            data = res.json()
            raw_markets.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        for m in raw_markets:
            ticker = m.get("ticker", "")
            if ticker.startswith(SHARD_BLACKLIST) or ticker.startswith(WEATHER_PREFIXES):
                continue
            bid_val = float(m.get("yes_bid_dollars") or m.get("yes_bid") or 0.0)
            ask_val = float(m.get("yes_ask_dollars") or m.get("yes_ask") or 0.0)
            if bid_val > 1.0:
                bid_val /= 100.0
            if ask_val > 1.0:
                ask_val /= 100.0
            if bid_val > 0.0 and ask_val > 0.0:
                valid.append({
                    "ticker": ticker,
                    "title": m.get("title", ""),
                    "yes_bid": bid_val,
                    "yes_ask": ask_val,
                    "spread_cents": int(round((ask_val - bid_val) * 100)),
                    "hours_to_close": hours_to_close(m),
                })
        return valid
    except Exception as e:
        print(f"[!] Market discovery error: {e}")
        return []


def market_is_tradable(m_data: dict) -> bool:
    """Open for trading and not already past its close time."""
    if m_data.get("status") not in TRADABLE_STATUSES:
        return False
    htc = hours_to_close(m_data)
    return htc is None or htc > 0


def _price(m: dict, key: str) -> float:
    try:
        v = float(m.get(f"{key}_dollars") or m.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return v / 100.0 if v > 1.0 else v


def exit_price(m_data: dict) -> float:
    """
    Sell price for held YES contracts: 1¢ under the best ask, but never at or below
    the best bid (that would cross the spread and pay taker fees).
    The old code read an empty ask ("0.0000") as 0 and dumped inventory at 5¢.
    """
    bid = _price(m_data, "yes_bid")
    ask = _price(m_data, "yes_ask")
    if 0.0 < ask < 1.0:
        target = round(ask - 0.01, 2)
        if target <= bid:
            target = round(ask, 2)          # 1¢-wide book: join the ask instead of crossing
    else:
        target = round(bid + 0.05, 2)       # no sellers on the book: rest a little above the bid
    return min(0.99, max(0.02, target))


def run_inventory_exits(positions: dict, resting_orders: list):
    resting_tickers = {ro.get("ticker") for ro in resting_orders}
    deadline = time.monotonic() + EXIT_TIME_BUDGET_SECONDS
    dust = closed = deferred = 0

    for ticker, p in positions.items():
        qty = p.get("position", 0.0)
        # Only YES inventory (qty > 0) is exited: the bot only buys YES, and an
        # "ask" on a NO holding would open a new position instead of closing one.
        if qty <= 0 or ticker in resting_tickers:
            continue
        # Weather markets belong to weather_bot.py; never sell its positions
        if ticker.startswith(WEATHER_PREFIXES):
            continue
        # Bug A: fractional dust (0.9x, 0.24x...) would become count 0 -> HTTP 400.
        if qty < 1.0:
            dust += 1
            continue
        # Bug B: expired markets wait in positions until settlement -> HTTP 409.
        if ticker in closed_tickers:
            closed += 1
            continue
        # Time budget: whatever is left gets handled next cycle.
        if time.monotonic() >= deadline:
            deferred += 1
            continue

        count = int(qty)  # whole contracts only; any fraction rides to settlement
        try:
            m_path = f"/trade-api/v2/markets/{ticker}"
            m_res = requests.get(f"{base_url}/markets/{ticker}", headers=auth.get_headers("GET", m_path), timeout=4)
            if m_res.status_code != 200:
                continue
            m_data = m_res.json().get("market", {})
            if not market_is_tradable(m_data):
                closed_tickers.add(ticker)
                closed += 1
                print(f"[-] {ticker} is {m_data.get('status', 'closed')}; holding to settlement, no exit order.")
                continue

            print(f"\n[*] INVENTORY ACTION: Holding {count}x {ticker} (YES)")
            target_ask = exit_price(m_data)
            print(f"    [*] Posting Exit Ask @ ${target_ask:.2f}...")
            exit_order = {
                "ticker": ticker,
                "side": "ask",
                "price": target_ask,
                "count": count,
                "time_in_force": "good_till_canceled"
            }
            res = order_mgr.dispatch_order(exit_order)
            if res:
                oid = res.get("order_id")
                reconciler.register_order(oid, ticker, "ask", target_ask)
                log_trade(ticker, "EXIT_ASK", "ASK", count, target_ask, oid)
            elif order_mgr.last_reject_code == "market_closed":
                closed_tickers.add(ticker)  # closed between our check and the order
        except Exception as e:
            print(f"[!] Error managing exit on {ticker}: {e}")

    if dust or closed or deferred:
        print(f"[*] Exits skipped: {dust} dust (<1 contract) | {closed} closed/awaiting settlement | "
              f"{deferred} deferred to next cycle ({EXIT_TIME_BUDGET_SECONDS:.0f}s budget)")


cycle = 1
active_stage = MANUAL_STAGE
start_balance = START_BALANCE
halted_reason = None

try:
    while True:
        timestamp = time.strftime('%H:%M:%S')
        tier = LadderManager.get_tier(MANUAL_STAGE)

        risk.max_contracts_per_market = tier["max_market_contracts"]
        risk.max_dollar_exposure_per_market = tier["max_market_dollars"]
        risk.max_total_open_exposure = tier["max_portfolio_exposure"]
        contract_size = tier["contract_size"]

        print(f"\n{'='*20} [CYCLE {cycle:04d} | {timestamp} | STAGE {tier['stage']} ({contract_size}x)] {'='*20}")

        # ---- Fail closed: no trading on missing data -------------------------
        positions = pos_mgr.get_all_positions()
        rec_status = reconciler.reconcile_cycle(protected_prefixes=WEATHER_PREFIXES)
        resting_orders = reconciler.get_remote_resting_orders()
        if not pos_mgr.last_ok or not reconciler.last_ok:
            print("[!] Could not read positions or open orders from Kalshi. Skipping this cycle (no new orders).")
            cycle += 1
            time.sleep(CYCLE_INTERVAL_SECONDS)
            continue

        # ---- Real balance + loss stop ---------------------------------------
        cash = fetch_cash_balance()
        open_cost = sum(p.get("market_exposure", 0.0) for p in positions.values())
        can_open = cash is not None
        if cash is not None:
            if start_balance is None:
                start_balance = cash + open_cost
                print(f"[!] START_BALANCE not set; using ${start_balance:,.2f} from this launch as the baseline.")
            value_at_cost = cash + open_cost
            drawdown = start_balance - value_at_cost
            print(f"[*] Cash ${cash:,.2f} + open at cost ${open_cost:,.2f} = ${value_at_cost:,.2f} "
                  f"| vs start {-drawdown:+,.2f} | loss stop at -{MAX_LOSS_DOLLARS:,.2f}")
            if drawdown >= MAX_LOSS_DOLLARS and halted_reason is None:
                halted_reason = f"loss of ${drawdown:,.2f} reached the ${MAX_LOSS_DOLLARS:,.2f} stop"
            if halted_reason:
                can_open = False
                print(f"[!!] LOSS STOP ACTIVE ({halted_reason}). No new positions; exits only. "
                      f"Restart the bot after reviewing to clear it.")
        else:
            print("[!] Balance unavailable this cycle: no new positions.")

        print(f"[*] Orders: {rec_status['resting_count']} active resting | {rec_status['expired_pruned']} stale pruned")
        print(f"[*] Inventory: {len(positions)} active position(s) held")
        run_inventory_exits(positions, resting_orders)

        if not can_open:
            cycle += 1
            time.sleep(CYCLE_INTERVAL_SECONDS)
            continue

        markets = fetch_open_liquid_markets()
        spread_candidates = [m for m in markets if MIN_SPREAD_CENTS <= m["spread_cents"] <= MAX_SPREAD_CENTS]
        too_wide = sum(1 for m in markets if m["spread_cents"] > MAX_SPREAD_CENTS)
        print(f"[*] Discovery: {len(markets)} open books | {len(spread_candidates)} with {MIN_SPREAD_CENTS}-{MAX_SPREAD_CENTS}¢ spreads "
              f"| {too_wide} skipped as too thin (>{MAX_SPREAD_CENTS}¢)")

        active_tickers = {ro.get("ticker") for ro in resting_orders}.union(positions.keys())
        print(f"[*] Active markets: {len(active_tickers)} / {MAX_ACTIVE_MARKETS}")
        placed_this_cycle = 0
        skipped_price = skipped_time = 0

        for m in spread_candidates:
            if placed_this_cycle >= MAX_MARKETS_PER_PASS or len(active_tickers) >= MAX_ACTIVE_MARKETS:
                break
            ticker = m["ticker"]
            if ticker in active_tickers:
                continue

            yes_bid = m["yes_bid"]
            target_bid = round(yes_bid + 0.01, 2)

            if not (MIN_ENTRY_PRICE <= target_bid <= MAX_ENTRY_PRICE):
                skipped_price += 1
                continue
            htc = m["hours_to_close"]
            if htc is None or htc < MIN_HOURS_TO_CLOSE or htc > MAX_HOURS_TO_CLOSE:
                skipped_time += 1
                continue

            approved, reason = risk.evaluate_order(
                ticker=ticker,
                price_dollars=target_bid,
                count=contract_size,
                current_positions=positions,
                resting_orders=resting_orders,
                current_balance=cash,
            )

            if approved:
                print(f"\n[+] Placing Maker Quote on {ticker}")
                print(f"    Spread: {m['spread_cents']}¢ | Target Bid: {int(round(target_bid*100))}¢ ({contract_size}x) | Closes in {htc:.1f}h")
                order = order_mgr.build_maker_quote_order(ticker=ticker, target_side="bid", bid_price=target_bid, contract_count=contract_size)
                placed = order_mgr.dispatch_order(order)
                if placed:
                    oid = placed.get("order_id")
                    reconciler.register_order(oid, ticker, "bid", target_bid)
                    log_trade(ticker, "ENTRY_BID", "BID", contract_size, target_bid, oid)
                    active_tickers.add(ticker)
                    placed_this_cycle += 1

        if skipped_price or skipped_time:
            print(f"[*] Skipped: {skipped_price} outside {int(MIN_ENTRY_PRICE*100)}-{int(MAX_ENTRY_PRICE*100)}¢ | "
                  f"{skipped_time} closing within {MIN_HOURS_TO_CLOSE:.0f}h, beyond 7 days, or no close time")

        print(f"\n[✓] Cycle {cycle:04d} complete. Resting {CYCLE_INTERVAL_SECONDS}s before next heartbeat...")
        cycle += 1
        time.sleep(CYCLE_INTERVAL_SECONDS)

except KeyboardInterrupt:
    print("\n\n" + "=" * 65)
    print("[!] SHUTDOWN SIGNAL (Ctrl+C). Cleaning up engine state...")
    print("=" * 65)
    cleared = reconciler.cancel_all_resting(protected_prefixes=WEATHER_PREFIXES)
    print(f"[✓] Cancelled {cleared} resting order(s).")
    print("[*] For your real results, run:  python true_pnl.py")
    print("[✓] Autopilot shutdown cleanly. Goodbye!\n")
    sys.exit(0)
