from auth import KalshiAuth
from strategy.position_manager import PositionManager

print("\n" + "=" * 60)
print("     TESTING MODULE 2: REAL-TIME POSITION & INVENTORY MANAGER     ")
print("=" * 60)

auth = KalshiAuth()
pos_mgr = PositionManager(auth_client=auth)

print("\n[*] Fetching live open positions from your Kalshi account...")
positions = pos_mgr.get_all_positions()

print(f"[✓] Active Positions Found: {len(positions)}")
if positions:
    for idx, (ticker, p) in enumerate(positions.items(), start=1):
        print(f"\n  {idx}. [{ticker}]")
        print(f"      Quantity: {p['position']} ({p['side'].upper()})")
        print(f"      Capital Exposure: ${p['market_exposure']:.2f}")
        print(f"      Realized PnL:     ${p['realized_pnl']:.2f}")
else:
    print("  [-] No open inventory held currently (100% in cash).")

print("\n" + "=" * 60)
print("[✓] Position Manager test completed.")
print("=" * 60 + "\n")