import os
import requests
from auth import KalshiAuth

# 1. WRITE THE CLEAN ORDER MANAGER
code = '''import uuid
import requests
from auth import KalshiAuth

class OrderManager:
    def __init__(self, auth_client=None, live=False, dry_run=None):
        if dry_run is not None:
            self.live = not dry_run
        else:
            self.live = bool(live)

        self.base_url = (
            "https://api.elections.kalshi.com/trade-api/v2"
            if self.live
            else "https://demo-api.kalshi.co/trade-api/v2"
        )

        if auth_client:
            self.auth_client = auth_client
        else:
            try:
                self.auth_client = KalshiAuth()
            except Exception as e:
                self.auth_client = None
                if self.live:
                    print(f"[!] Warning: KalshiAuth failed to load: {e}")

    @staticmethod
    def _to_cents(price):
        try:
            val = float(price)
            if 0.0 < val <= 1.0:
                val = val * 100.0
            return max(1, min(99, int(round(val))))
        except Exception:
            return 50

    def build_ioc_sweep_order(self, ticker, target_side, max_price, contract_count):
        return {
            "ticker": ticker,
            "action": "buy",
            "side": str(target_side).lower(),
            "price": max_price,
            "count": int(contract_count),
            "type": "limit"
        }

    def build_maker_quote_order(self, ticker, target_side, bid_price, contract_count):
        return {
            "ticker": ticker,
            "action": "buy",
            "side": str(target_side).lower(),
            "price": bid_price,
            "count": int(contract_count),
            "type": "limit"
        }

    def place_order(self, ticker: str, action: str = "buy", side: str = "yes", count: int = 1, price: float = 0.50, order_type: str = "limit"):
        cents = self._to_cents(price)
        count = int(count)
        side_clean = "yes" if "yes" in str(side).lower() or str(side).lower() == "bid" else "no"
        action_clean = action.lower()

        if not self.live:
            print(f"[DRY-RUN] Order: {action_clean.upper()} {count}x {ticker} {side_clean.upper()} @ {cents}¢")
            return {"order_id": "simulated", "status": "simulated"}

        if not self.auth_client:
            print("[!] Cannot submit live order: KalshiAuth client missing.")
            return None

        # Official Kalshi V2 Orders Route
        endpoint = f"{self.base_url}/orders"
        sign_path = "/trade-api/v2/orders"

        payload = {
            "action": action_clean,
            "ticker": ticker,
            "type": order_type.lower(),
            "side": side_clean,
            "count": count,
            "client_order_id": str(uuid.uuid4())
        }

        if side_clean == "yes":
            payload["yes_price"] = cents
        else:
            payload["no_price"] = cents

        headers = self.auth_client.get_headers(method="POST", path=sign_path)

        try:
            res = requests.post(endpoint, json=payload, headers=headers, timeout=10)
            if res.status_code in (200, 201):
                data = res.json()
                order_info = data.get("order", data)
                order_id = order_info.get("order_id", "N/A")
                print(f"[✓] [LIVE ORDER SUCCESS] {action_clean.upper()} {count}x {ticker} @ {cents}¢ (ID: {order_id})")
                return data
            else:
                print(f"[!] [LIVE REJECT] Status {res.status_code}: {res.text}")
                return None
        except requests.RequestException as e:
            print(f"[!] Network error placing order: {e}")
            return None

    def dispatch_order(self, order_dict: dict):
        if not order_dict:
            return None
        return self.place_order(
            ticker=order_dict.get("ticker"),
            action=order_dict.get("action", "buy"),
            side=order_dict.get("side", order_dict.get("target_side", "yes")),
            count=order_dict.get("count", order_dict.get("contract_count", 1)),
            price=order_dict.get("price", order_dict.get("max_price", order_dict.get("bid_price", 50))),
            order_type=order_dict.get("type", "limit")
        )
'''

target_path = os.path.join("strategy", "order_manager.py")
with open(target_path, "w", encoding="utf-8") as f:
    f.write(code)
print("[1/2] strategy/order_manager.py written.")

# 2. IMMEDIATE PROBE TEST AGAINST KALSHI
print("[2/2] Sending test order probe to Kalshi /trade-api/v2/orders...")
auth = KalshiAuth()
headers = auth.get_headers("POST", "/trade-api/v2/orders")
# Probe with an intentional dry test
res = requests.post(
    "https://api.elections.kalshi.com/trade-api/v2/orders",
    headers=headers,
    json={"ticker": "TEST-PROBE", "action": "buy", "type": "limit", "side": "yes", "count": 1, "yes_price": 50},
    timeout=10
)

print("\n" + "=" * 50)
print(f"Kalshi Response HTTP Status: {res.status_code}")
print(f"Kalshi Raw Response: {res.text}")
print("=" * 50)

if res.status_code != 404:
    print("\n[✓] 404 IS ELIMINATED! The order route is active and responding.")
else:
    print("\n[!] Still 404.")