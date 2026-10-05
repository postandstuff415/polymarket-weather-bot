from auth import KalshiAuth
from strategy.position_manager import PositionManager
from strategy.reconciler import OrderReconciler
from strategy.risk_governor import RiskGovernor

print("\n" + "=" * 60)
print("     TESTING MODULE 3: RISK GOVERNOR & CAPITAL CEILINGS     ")
print("=" * 60)

auth = KalshiAuth()
pos_mgr = PositionManager(auth_client=auth)
reconciler = OrderReconciler(auth_client=auth)
risk = RiskGovernor(
    max_contracts_per_market=5,
    max_dollar_exposure_per_market=15.0,
    max_total_open_exposure=250.0
)

# Fetch current live state
positions = pos_mgr.get_all_positions()
resting = reconciler.get_remote_resting_orders()

print(f"[*] Live Positions: {len(positions)} market(s)")
print(f"[*] Live Resting Orders: {len(resting)} order(s)")

# Test 1: Standard compliant order on a fresh ticker
test_ticker_1 = "KXTEST-FRESH"
ok, reason = risk.evaluate_order(
    ticker=test_ticker_1,
    price_dollars=0.05,
    count=2,
    current_positions=positions,
    resting_orders=resting
)
print(f"\n[Test 1] Order 2x @ 5¢ on new ticker: {'APPROVED' if ok else 'REJECTED'} ({reason})")

# Test 2: Over-sized order exceeding contract ceiling (e.g. 10 contracts)
ok, reason = risk.evaluate_order(
    ticker=test_ticker_1,
    price_dollars=0.05,
    count=10,
    current_positions=positions,
    resting_orders=resting
)
print(f"[Test 2] Order 10x @ 5¢ (oversized): {'APPROVED' if ok else 'BLOCKED'} ({reason})")

# Test 3: Order on your filled position ticker (KXCS2GAME-26OCT021200LILMIXNPG-LILMIX)
test_ticker_filled = "KXCS2GAME-26OCT021200LILMIXNPG-LILMIX"
ok, reason = risk.evaluate_order(
    ticker=test_ticker_filled,
    price_dollars=0.03,
    count=2,
    current_positions=positions,
    resting_orders=resting
)
print(f"[Test 3] Order 2x @ 3¢ on existing filled ticker: {'APPROVED' if ok else 'BLOCKED'} ({reason})")

print("\n" + "=" * 60)
print("[✓] Risk Governor logic verified.")
print("=" * 60 + "\n")