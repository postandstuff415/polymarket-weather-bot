"""
Kalshi Authenticated Order Manager
Constructs, cryptographically signs (RSA-PSS), and dispatches:
1. IOC Taker Sweeps (Sniping stale retail quotes)
2. Resting Maker Quotes (Dynamic liquidity posting)
3. Batch Cancellations
"""

import base64
import json
import time
import uuid
from pathlib import Path
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API_KEY_ID = "f3c190be-f642-4cad-a673-7a5903a4591e"
KEY_FILE_PATH = "kalshi_key.pem"
BASE_URL = "https://external-api.kalshi.com"

class OrderManager:
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.api_key_id = API_KEY_ID
        self.base_url = BASE_URL
        self.private_key = self._load_private_key(KEY_FILE_PATH)

    def _load_private_key(self, key_path: str):
        """Loads RSA private key from disk."""
        path = Path(key_path)
        if not path.exists():
            raise FileNotFoundError(f"Private key not found at {key_path}")
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def _sign_request(self, method: str, path: str, timestamp_str: str) -> str:
        """
        Generates Kalshi V2 RSA-PSS SHA-256 base64 signature.
        Pre-sign message structure: timestamp + method + path
        """
        message = f"{timestamp_str}{method}{path}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _get_headers(self, method: str, path: str) -> dict:
        """Generates authenticated HTTP headers."""
        timestamp_str = str(int(time.time() * 1000))
        signature = self._sign_request(method, path, timestamp_str)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_str,
            "Content-Type": "application/json",
        }

    def build_ioc_sweep_order(self, ticker: str, target_side: str, max_price: float, contract_count: int) -> dict:
        """Builds an Immediate-or-Cancel (IOC) limit order payload to sweep resting asks."""
        price_cents = int(round(max_price * 100))
        client_order_id = f"sweep_{ticker}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        
        payload = {
            "ticker": ticker,
            "action": "buy",
            "side": target_side.lower(),
            "type": "limit",
            "count": contract_count,
            "client_order_id": client_order_id,
            "time_in_force": "ioc",
        }
        if target_side.lower() == "yes":
            payload["yes_price"] = price_cents
        else:
            payload["no_price"] = price_cents
        return payload

    def build_maker_quote_order(self, ticker: str, target_side: str, bid_price: float, contract_count: int) -> dict:
        """Builds a resting maker quote to capture panic retail sellers."""
        price_cents = int(round(bid_price * 100))
        client_order_id = f"maker_{ticker}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        
        payload = {
            "ticker": ticker,
            "action": "buy",
            "side": target_side.lower(),
            "type": "limit",
            "count": contract_count,
            "client_order_id": client_order_id,
            "post_only": True,
        }
        if target_side.lower() == "yes":
            payload["yes_price"] = price_cents
        else:
            payload["no_price"] = price_cents
        return payload

    def dispatch_order(self, payload: dict) -> dict:
        """Dispatches order to Kalshi API or logs simulated dry run."""
        order_type = "IOC SWEEP" if payload.get("time_in_force") == "ioc" else "RESTING MAKER"
        side = payload["side"].upper()
        cents = payload.get("yes_price") or payload.get("no_price")

        if self.dry_run:
            print(f"[*] [DRY-RUN] Dispatched {order_type} -> {payload['count']}x {payload['ticker']} {side} @ {cents}¢")
            return {
                "status": "simulated_executed",
                "order_id": f"sim_{uuid.uuid4().hex[:8]}",
                "payload": payload,
            }

        # LIVE EXECUTION PATH
        path = "/trade-api/v2/portfolio/orders"
        headers = self._get_headers("POST", path)
        url = f"{self.base_url}{path}"

        try:
            res = requests.post(url, headers=headers, json=payload, timeout=4)
            data = res.json()
            if res.status_code in [200, 201]:
                print(f"[✓] [LIVE FILL] {order_type} Placed: Order ID {data.get('order', {}).get('order_id')}")
            else:
                print(f"[!] [LIVE REJECT] Status {res.status_code}: {data.get('message', data)}")
            return {"status_code": res.status_code, "data": data}
        except Exception as e:
            print(f"[X] [DISPATCH ERROR]: {e}")
            return {"status": "error", "error": str(e)}


def main():
    print("--- [TESTING AUTHENTICATED ORDER MANAGER] ---")
    # Initialize with dry_run=True for validation
    manager = OrderManager(dry_run=True)
    
    test_sweep = manager.build_ioc_sweep_order(
        ticker="KXGASNAT-26SEP28-T4.4600",
        target_side="yes",
        max_price=0.88,
        contract_count=1
    )
    result = manager.dispatch_order(test_sweep)
    print(f"Dispatched Result: {result['status']}")
    print("---------------------------------------------")

if __name__ == "__main__":
    main()
