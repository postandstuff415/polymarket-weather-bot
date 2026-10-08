import requests
from auth import KalshiAuth

MAX_PAGES = 20  # pagination safety cap


class PositionManager:
    def __init__(self, auth_client=None, base_url="https://external-api.kalshi.com/trade-api/v2"):
        self.auth = auth_client or KalshiAuth()
        self.base_url = base_url
        self.last_ok = False  # False if the last fetch failed (callers must not trade on {})

    def get_all_positions(self) -> dict:
        """
        All unsettled positions, across every page (the old version read only the
        first 100, so the bot could forget positions and buy the same market again).
        """
        self.last_ok = False
        path = "/trade-api/v2/portfolio/positions"
        active_positions = {}
        cursor = None
        try:
            for _ in range(MAX_PAGES):
                params = {"settlement_status": "unsettled", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                res = requests.get(f"{self.base_url}/portfolio/positions",
                                   headers=self.auth.get_headers("GET", path), params=params, timeout=10)
                if res.status_code != 200:
                    print(f"[!] PositionManager API Status {res.status_code}: {res.text[:200]}")
                    return {}
                data = res.json()
                raw_positions = data.get("market_positions", []) or data.get("positions", [])
                for p in raw_positions:
                    ticker = p.get("ticker")
                    pos_val = p.get("position_fp") or p.get("position") or 0
                    pos = float(pos_val)
                    if pos != 0.0:
                        active_positions[ticker] = {
                            "ticker": ticker,
                            "position": pos,
                            "side": "yes" if pos > 0 else "no",
                            "market_exposure": float(p.get("market_exposure_dollars") or p.get("market_exposure") or 0.0),
                            "realized_pnl": float(p.get("realized_pnl_dollars") or p.get("realized_pnl") or 0.0),
                            "fees_paid": float(p.get("fees_paid_dollars") or p.get("fees_paid") or 0.0)
                        }
                cursor = data.get("cursor")
                if not cursor:
                    break
            self.last_ok = True
            return active_positions
        except Exception as e:
            print(f"[!] PositionManager network error: {e}")
            return {}

    def has_inventory(self, ticker: str) -> bool:
        positions = self.get_all_positions()
        return ticker in positions and abs(positions[ticker]["position"]) > 0

    def get_inventory_count(self, ticker: str) -> float:
        positions = self.get_all_positions()
        if ticker in positions:
            return positions[ticker]["position"]
        return 0.0
