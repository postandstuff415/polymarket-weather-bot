import time
from datetime import datetime

import requests
from auth import KalshiAuth

MAX_PAGES = 20  # pagination safety cap (200 orders per page)


def _parse_ts(raw):
    """ISO timestamp from Kalshi -> epoch seconds, or None."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class OrderReconciler:
    def __init__(self, auth_client=None, ttl_seconds=60, base_url="https://external-api.kalshi.com/trade-api/v2"):
        self.auth = auth_client or KalshiAuth()
        self.base_url = base_url
        self.ttl_seconds = ttl_seconds
        self.tracked_orders = {}
        self.first_seen = {}   # order_id -> time first seen, for orders this run didn't place
        self.last_ok = False   # False if the last resting-orders fetch failed
        self.dead_ids = set()  # listed as resting but neither cancel endpoint knows them

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
        """All resting orders, across every page. last_ok is False if any page fails."""
        path = "/trade-api/v2/portfolio/orders"
        self.last_ok = False
        orders, cursor = [], None
        try:
            for _ in range(MAX_PAGES):
                params = {"status": "resting", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                res = requests.get(f"{self.base_url}/portfolio/orders",
                                   headers=self.auth.get_headers("GET", path), params=params, timeout=10)
                if res.status_code != 200:
                    print(f"[!] Resting orders API Status {res.status_code}: {res.text[:200]}")
                    return []
                data = res.json()
                orders.extend(o for o in data.get("orders", []) if o.get("order_id") not in self.dead_ids)
                cursor = data.get("cursor")
                if not cursor:
                    break
            self.last_ok = True
            return orders
        except Exception as e:
            print(f"[!] Resting orders network error: {e}")
            return []

    def cancel_order(self, order_id: str, ticker: str = None) -> bool:
        """
        Kalshi V2 cancel. Markets live on several exchange shards; passing the
        market ticker lets Kalshi route the cancel to the right one. Without it,
        orders on other shards come back 404 "not found" and can never be cancelled
        (that is how hundreds of stale orders piled up).
        """
        path = f"/trade-api/v2/portfolio/events/orders/{order_id}"
        params = {"market_ticker": ticker} if ticker else None
        try:
            res = requests.delete(f"{self.base_url}/portfolio/events/orders/{order_id}",
                                  headers=self.auth.get_headers("DELETE", path), params=params, timeout=10)
            if res.status_code in (200, 204, 404):
                self.tracked_orders.pop(order_id, None)
            if res.status_code in (200, 204):
                self.first_seen.pop(order_id, None)
                print(f"[✓] [PRUNED & CANCELLED] ID: {order_id[:18]}...")
                return True
            if res.status_code == 404:
                self.first_seen.pop(order_id, None)
                self.dead_ids.add(order_id)
                print(f"[-] Order {order_id[:18]}... not found (already filled or cancelled); ignoring it.")
            else:
                print(f"[!] [CANCEL REJECT] ID {order_id[:18]}... | Status {res.status_code}: {res.text[:200]}")
            return False
        except Exception as e:
            print(f"[!] Network error cancelling order: {e}")
            return False

    def cancel_all_resting(self, protected_prefixes=()) -> int:
        remote_orders = self.get_remote_resting_orders()
        canceled_count = 0
        for ord_info in remote_orders:
            if protected_prefixes and str(ord_info.get("ticker", "")).startswith(tuple(protected_prefixes)):
                continue
            oid = ord_info.get("order_id")
            if oid and self.cancel_order(oid, ord_info.get("ticker")):
                canceled_count += 1
        self.tracked_orders.clear()
        self.first_seen.clear()
        return canceled_count

    def _placed_at(self, oid, r_order, now):
        """When was this order placed? Local record, else Kalshi's created_time,
        else the first time this run saw it. Orders left over from a previous run
        used to look brand new forever and were never pruned."""
        local_meta = self.tracked_orders.get(oid)
        if local_meta:
            return local_meta["placed_at"]
        created = _parse_ts(r_order.get("created_time"))
        if created:
            return created
        return self.first_seen.setdefault(oid, now)

    def reconcile_cycle(self, protected_prefixes=()) -> dict:
        now = time.time()
        remote_orders = self.get_remote_resting_orders()
        remote_ids = {o.get("order_id"): o for o in remote_orders if o.get("order_id")}

        # Clean local registry of orders that no longer exist remotely
        for oid in [oid for oid in self.tracked_orders if oid not in remote_ids]:
            del self.tracked_orders[oid]
        for oid in [oid for oid in self.first_seen if oid not in remote_ids]:
            del self.first_seen[oid]

        expired_orders = []
        active_valid_orders = []

        for oid, r_order in remote_ids.items():
            ticker = r_order.get("ticker", "N/A")
            # Orders owned by another bot (e.g. the weather bot) are left alone
            if protected_prefixes and str(ticker).startswith(tuple(protected_prefixes)):
                continue
            age = now - self._placed_at(oid, r_order, now)
            if age >= self.ttl_seconds:
                expired_orders.append((oid, ticker, age))
            else:
                active_valid_orders.append(oid)

        canceled = 0
        for oid, ticker, age in expired_orders:
            print(f"[*] Pruning stale quote (Age: {age:.1f}s >= {self.ttl_seconds}s) on {ticker}")
            if self.cancel_order(oid, ticker):
                canceled += 1

        return {
            "resting_count": len(active_valid_orders),
            "expired_pruned": canceled,
            "active_ids": active_valid_orders
        }
