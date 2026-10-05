import requests
from auth import KalshiAuth

auth = KalshiAuth()
base_url = "https://external-api.kalshi.com/trade-api/v2"

real_series = ["KXHIGHMIA", "KXHIGHCHI", "KXHIGHNY", "KXSOFRD", "KXAAAGASM"]

print("\n" + "=" * 65)
print("[*] FETCHING LIVE OPEN MARKETS FOR REAL KALSHI SERIES...")
print("=" * 65)

headers = auth.get_headers("GET", "/trade-api/v2/markets")

for s in real_series:
    url = f"{base_url}/markets?series_ticker={s}&status=open&limit=4"
    res = requests.get(url, headers=headers, timeout=10)
    if res.status_code == 200:
        markets = res.json().get("markets", [])
        if markets:
            print(f"\n[+] {s} (FOUND {len(markets)} OPEN MARKETS):")
            for m in markets:
                print(f"    Ticker: {m.get('ticker')} | Closes: {m.get('close_time')}")
        else:
            print(f"\n[-] {s}: No open markets right now")
    else:
        print(f"\n[!] Error {s}: {res.status_code}")

print("\n" + "=" * 65 + "\n")