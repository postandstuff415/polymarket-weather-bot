import time
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
