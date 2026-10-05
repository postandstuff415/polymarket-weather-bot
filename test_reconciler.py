from auth import KalshiAuth
from strategy.reconciler import OrderReconciler

print("\n" + "=" * 60)
print("     TESTING MODULE 1: ORDER RECONCILER & TTL MANAGER     ")
print("=" * 60)

auth = KalshiAuth()
reconciler = OrderReconciler(auth_client=auth, ttl_seconds=60)

# 1. Fetch live open orders
print("\n[*] Querying live resting orders on your Kalshi account...")
resting = reconciler.get_remote_resting_orders()
print(f"[✓] Live Resting Orders Found: {len(resting)}")

for idx, o in enumerate(resting, start=1):
    oid = o.get("order_id")
    ticker = o.get("ticker")
    side = o.get("side")
    price = o.get("yes_price") or o.get("price")
    count = o.get("remaining_count") or o.get("count")
    print(f"  {idx}. ID: {oid[:18]}... | {ticker} | {side.upper()} {count}x @ {price}")

# 2. Run reconciliation cycle
print("\n[*] Executing a reconciliation cycle check...")
result = reconciler.reconcile_cycle()
print(f"[✓] Cycle Output: {result}")

print("\n" + "=" * 60)
print("[✓] Reconciler test completed successfully.")
print("=" * 60 + "\n")