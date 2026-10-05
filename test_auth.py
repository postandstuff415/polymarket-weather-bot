import requests
from auth import KalshiAuth

def test_environments():
    try:
        auth = KalshiAuth()
        print(f"[*] Loaded Key ID: {auth.key_id[:8]}...{auth.key_id[-4:]}")
    except Exception as e:
        print(f"[!] Failed to load credentials from .env: {e}")
        return

    path = "/trade-api/v2/portfolio/balance"
    
    environments = {
        "PRODUCTION (Live)": "https://api.elections.kalshi.com/trade-api/v2",
        "DEMO (Sandbox)": "https://demo-api.kalshi.co/trade-api/v2"
    }

    for env_name, base_url in environments.items():
        print(f"\n--- Testing against {env_name} ---")
        headers = auth.get_headers("GET", path)
        try:
            res = requests.get(f"{base_url}/portfolio/balance", headers=headers, timeout=10)
            print(f"Status: {res.status_code}")
            if res.status_code == 200:
                print(f"[✓] SUCCESS on {env_name}! Balance response: {res.json()}")
            else:
                print(f"[X] Rejected on {env_name}: {res.text}")
        except Exception as err:
            print(f"[!] Connection failed to {env_name}: {err}")

if __name__ == "__main__":
    test_environments()
