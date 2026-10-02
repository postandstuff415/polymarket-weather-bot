import os
import csv
import time
from datetime import datetime

TRADE_LOG = "trade_log.csv"
PERF_LOG = "performance_log.csv"

# Ladder Stage Boundaries (in elapsed days from start)
STAGE_BOUNDS = {
    1: {"name": "Baseline Hygiene", "min_day": 0.0, "max_day": 4.0, "size": 1},
    2: {"name": "Spread Velocity", "min_day": 4.0, "max_day": 11.0, "size": 3},
    3: {"name": "Volume Acceleration", "min_day": 11.0, "max_day": 19.0, "size": 10},
    4: {"name": "Density Quoting", "min_day": 19.0, "max_day": 29.0, "size": 25},
    5: {"name": "Full Production", "min_day": 29.0, "max_day": 999.0, "size": 100}
}

START_TIMESTAMP = 1790908800  # Anchor: Oct 2, 2026

def init_perf_log():
    if not os.path.exists(PERF_LOG):
        with open(PERF_LOG, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "stage", "stage_name", "contract_size", "start_time", "end_time",
                "orders_placed", "filled_entries", "filled_exits", "roundtrips_completed",
                "total_contracts_traded", "gross_pnl", "est_maker_fees", "net_pnl",
                "avg_spread_cents", "status"
            ])

def compile_stage_report(target_stage: int, mark_completed: bool = False):
    """
    Parses trade_log.csv, isolates trades for target_stage,
    computes matched PnL and metrics, and appends/updates performance_log.csv.
    """
    init_perf_log()

    if not os.path.exists(TRADE_LOG):
        print(f"[!] No {TRADE_LOG} found yet. Let the engine complete more cycles.")
        return None

    stage_info = STAGE_BOUNDS.get(target_stage, STAGE_BOUNDS[1])
    min_epoch = START_TIMESTAMP + (stage_info["min_day"] * 86400)
    max_epoch = START_TIMESTAMP + (stage_info["max_day"] * 86400)

    # Read and bucket trades
    bids = []
    asks = []
    
    with open(TRADE_LOG, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                t_str = row.get("timestamp")
                t_epoch = time.mktime(time.strptime(t_str, "%Y-%m-%d %H:%M:%S"))
            except Exception:
                continue

            if min_epoch <= t_epoch < max_epoch:
                action = row.get("action", "").upper()
                if "BID" in action:
                    bids.append(row)
                elif "ASK" in action:
                    asks.append(row)

    total_orders_placed = len(bids) + len(asks)
    
    # Match entries and exits by ticker to calculate round trips
    ticker_bids = {}
    for b in bids:
        t = b["ticker"]
        ticker_bids.setdefault(t, []).append(float(b.get("price", 0.0)))

    roundtrips = 0
    gross_pnl = 0.0
    contracts_traded = 0
    total_spread_collected = 0.0

    for a in asks:
        t = a["ticker"]
        ask_px = float(a.get("price", 0.0))
        qty = int(float(a.get("count", 1)))
        
        if t in ticker_bids and len(ticker_bids[t]) > 0:
            bid_px = ticker_bids[t].pop(0)
            spread_captured = (ask_px - bid_px)
            gross_pnl += (spread_captured * qty)
            total_spread_collected += spread_captured
            roundtrips += 1
            contracts_traded += (qty * 2)

    # Kalshi maker fee calculation: ~$0.005 per contract or minimal maker charge
    est_maker_fees = round(contracts_traded * 0.005, 4)
    net_pnl = round(gross_pnl - est_maker_fees, 4)
    avg_spread = round((total_spread_collected / max(1, roundtrips)) * 100, 2)
    
    start_str = bids[0]["timestamp"] if bids else time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(min_epoch))
    end_str = asks[-1]["timestamp"] if asks else time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time()))
    status = "COMPLETED" if mark_completed else "ACTIVE"

    # Write summary row into performance_log.csv
    row_data = [
        target_stage, stage_info["name"], stage_info["size"], start_str, end_str,
        total_orders_placed, len(bids), len(asks), roundtrips,
        contracts_traded, f"{gross_pnl:.2f}", f"{est_maker_fees:.2f}", f"{net_pnl:.2f}",
        avg_spread, status
    ]

    with open(PERF_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row_data)

    # Print Clean Console Summary
    print("\n" + "=" * 65)
    print(f"      MILESTONE PERFORMANCE REPORT: STAGE {target_stage} ({stage_info['name']})")
    print("=" * 65)
    print(f"  Timeline:              {start_str}  --->  {end_str}")
    print(f"  Base Sizing:           {stage_info['size']} contract(s) per ticket")
    print(f"  Total Orders Placed:   {total_orders_placed}")
    print(f"  Completed Round Trips: {roundtrips}")
    print(f"  Total Volume Traded:   {contracts_traded} contracts")
    print(f"  Avg Spread Capture:    {avg_spread:.1f}¢ / contract")
    print(f"  Gross Captured PnL:    ${gross_pnl:.2f}")
    print(f"  Estimated Maker Fees:  ${est_maker_fees:.2f}")
    print(f"  Net Realized PnL:      ${net_pnl:.2f}")
    print(f"  Status:                [{status}] - Logged to {PERF_LOG}")
    print("=" * 65 + "\n")

    return row_data

if __name__ == "__main__":
    import sys
    stage_to_run = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    compile_stage_report(stage_to_run, mark_completed=False)