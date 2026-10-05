import os

code = """import uuid
import requests
from auth import KalshiAuth

class OrderManager:
    def __init__(self, auth_client=None, live=False, dry_run=None):
        if dry_run is not None:
            self.live = not dry_run
        else:
            self.live = bool(live)

        # Official Kalshi Production Router
        self.base_url = (
            "https://external-api.kalshi.com/trade-api/v2"
            if self.live
            else "https://external-api.demo.kalshi.co/trade-api/v2"
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
    def _parse_dollars(price):
        try:
            val = float(price)
            if val > 1.0:
                val = val / 100.0
            return max(0.0100, min(0.9900, round(val, 4)))
        except Exception:
            return 0.5000

    def build_ioc_sweep_order(self, ticker, target_side, max_price, contract_count):
        return {
            "ticker": ticker,
            "target_side": str(target_side).lower(),
            "price": self._parse_dollars(max_price),
            "count": int(contract_count),
            "time_in_force": "immediate_or_cancel"
        }

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
        target_side = str(order_dict.get("target_side", order_dict.get("side", "yes"))).lower()
        price_dollars = self._parse_dollars(order_dict.get("price", order_dict.get("bid_price", 0.50)))
        count = int(order_dict.get("count", order_dict.get("contract_count", 1)))
        tif = order_dict.get("time_in_force", "good_till_canceled")

        # Kalshi V2 Single-Book Side:
        # Buy YES -> "bid" at price
        # Buy NO  -> "ask" at (1.00 - price)
        if target_side in ("yes", "buy_yes", "bid"):
            v2_side = "bid"
            v2_price = price_dollars
        else:
            v2_side = "ask"
            v2_price = round(1.0 - price_dollars, 4)

        v2_price = max(0.0100, min(0.9900, v2_price))

        if not self.live:
            print(f"[DRY-RUN] V2 Order: {v2_side.upper()} {count}x {ticker} @ ${v2_price:.4f} ({tif})")
            return {"order_id": "simulated", "status": "simulated"}

        if not self.auth_client:
            print("[!] Cannot submit live order: KalshiAuth client missing.")
            return None

        # Verified Kalshi Create Order (V2) Endpoint
        endpoint = f"{self.base_url}/portfolio/events/orders"
        sign_path = "/trade-api/v2/portfolio/events/orders"

        payload = {
            "ticker": ticker,
            "side": v2_side,
            "count": f"{float(count):.2f}",
            "price": f"{v2_price:.4f}",
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
                print(f"[✓] [LIVE V2 ORDER PLACED] {v2_side.upper()} {count}x {ticker} @ ${v2_price:.4f} (ID: {order_id})")
                return data
            else:
                print(f"[!] [LIVE REJECT] Status {res.status_code}: {res.text}")
                return None
        except requests.RequestException as e:
            print(f"[!] Network error placing order: {e}")
            return None

    def place_order(self, ticker, action="buy", side="yes", count=1, price=0.50, order_type="limit"):
        return self.dispatch_order({
            "ticker": ticker,
            "target_side": side,
            "price": price,
            "count": count,
            "time_in_force": "immediate_or_cancel" if order_type.lower() == "ioc" else "good_till_canceled"
        })
"""

target_path = os.path.join("strategy", "order_manager.py")
with open(target_path, "w", encoding="utf-8") as f:
    f.write(code)

print("\n" + "=" * 65)
print("[✓] strategy/order_manager.py updated to V2 /portfolio/events/orders")
print("=" * 65 + "\n")