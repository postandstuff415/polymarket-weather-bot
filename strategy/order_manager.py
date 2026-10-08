import time
import uuid
import requests
from auth import KalshiAuth

# Rejects that are expected during normal running (dust, expired markets).
# They are logged once per ticker per hour instead of every cycle, and never raise.
QUIET_REJECTS = {(400, "invalid_order"), (409, "market_closed")}
QUIET_REJECT_INTERVAL_SECONDS = 3600


class OrderManager:
    def __init__(self, auth_client=None, live=False, dry_run=None):
        self.live = not dry_run if dry_run is not None else bool(live)
        self.base_url = (
            "https://external-api.kalshi.com/trade-api/v2"
            if self.live
            else "https://external-api.demo.kalshi.co/trade-api/v2"
        )
        self.auth_client = auth_client or KalshiAuth()
        self.last_reject_code = None      # error code of the most recent reject, e.g. "market_closed"
        self._last_quiet_log = {}         # (ticker, code) -> time last printed

    @staticmethod
    def _parse_dollars(price):
        try:
            val = float(price)
            if val > 1.0:
                val = val / 100.0
            return max(0.0100, min(0.9900, round(val, 4)))
        except Exception:
            return None  # unreadable price: refuse the order (it used to become a 50¢ order)

    def build_maker_quote_order(self, ticker, target_side, bid_price, contract_count):
        return {
            "ticker": ticker,
            "target_side": str(target_side).lower(),
            "price": self._parse_dollars(bid_price),
            "count": int(contract_count),
            "time_in_force": "good_till_canceled"
        }

    def dispatch_order(self, order_dict: dict):
        self.last_reject_code = None
        if not order_dict:
            return None

        ticker = order_dict.get("ticker")
        target_side = str(order_dict.get("target_side", order_dict.get("side", "bid"))).lower()
        price_dollars = self._parse_dollars(order_dict.get("price"))
        if price_dollars is None:
            print(f"[!] [SKIP] {ticker}: unreadable price {order_dict.get('price')!r}; order not sent.")
            return None

        # Dust guard: int(0.9) == 0 and Kalshi rejects a 0 count (HTTP 400).
        # Never send an order for less than one whole contract.
        try:
            raw_count = abs(float(order_dict.get("count", 1)))
        except (TypeError, ValueError):
            raw_count = 0.0
        if raw_count < 1.0:
            return None
        count = int(raw_count)
        tif = order_dict.get("time_in_force", "good_till_canceled")

        # In Kalshi V2 single-book:
        # "bid" = Buy YES at price_dollars
        # "ask" = Sell YES at price_dollars
        v2_side = "ask" if target_side in ("ask", "sell", "sell_yes") else "bid"

        endpoint = f"{self.base_url}/portfolio/events/orders"
        sign_path = "/trade-api/v2/portfolio/events/orders"

        payload = {
            "ticker": ticker,
            "side": v2_side,
            "count": f"{float(count):.2f}",
            "price": f"{price_dollars:.4f}",
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
                print(f"[✓] [LIVE V2 ORDER PLACED] {v2_side.upper()} {count}x {ticker} @ ${price_dollars:.4f} (ID: {order_id})")
                return data
            else:
                try:
                    code = (res.json().get("error") or {}).get("code")
                except ValueError:
                    code = None
                self.last_reject_code = code
                if (res.status_code, code) in QUIET_REJECTS:
                    key = (ticker, code)
                    now = time.time()
                    if now - self._last_quiet_log.get(key, 0) >= QUIET_REJECT_INTERVAL_SECONDS:
                        print(f"[-] [SKIP] {ticker}: {res.status_code} {code} (repeats muted for 1h)")
                        self._last_quiet_log[key] = now
                    return None
                print(f"[!] [LIVE REJECT] Status {res.status_code}: {res.text}")
                return None
        except requests.RequestException as e:
            print(f"[!] Network error placing order: {e}")
            return None
