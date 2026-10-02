import requests
from auth import KalshiAuth

class PositionManager:
    def __init__(self, auth_client=None, base_url="https://external-api.kalshi.com/trade-api/v2"):
        self.auth = auth_client or KalshiAuth()
        self.base_url = base_url

    def get_all_positions(self) -> dict:
        """
        Queries Kalshi V2 for active portfolio positions.
        Uses valid V2 settlement_status filter instead of deprecated query params.
        """
        path = "/trade-api/v2/portfolio/positions"
        headers = self.auth.get_headers("GET", path)
        params = {
            "settlement_status": "unsettled",
            "limit": 100
        }
        
        try:
            res = requests.get(
                f"{self.base_url}/portfolio/positions",
                headers=headers,
                params=params,
                timeout=10
            )
            
            if res.status_code == 200:
                data = res.json()
                raw_positions = data.get("market_positions", []) or data.get("positions", [])
                active_positions = {}
                
                for p in raw_positions:
                    ticker = p.get("ticker")
                    # Handle both float strings and integer representations
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
                return active_positions
            else:
                print(f"[!] PositionManager API Status {res.status_code}: {res.text}")
                return {}
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
