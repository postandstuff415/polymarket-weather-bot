import requests
import ctypes

# Kalshi public market endpoint
url = "https://external-api.kalshi.com/trade-api/v2/markets?limit=1&status=open"

res = requests.get(url)

if res.status_code == 200:
    markets = res.json().get("markets", [])
    if markets:
        market = markets[0]
        title = market.get("title")
        ticker = market.get("ticker")
        yes_bid = market.get("yes_bid", 0)
        yes_ask = market.get("yes_ask", 0)

        # Native Windows dialog box (0x40 = Info icon)
        alert_text = f"{title} ({ticker})\n\n• Yes Bid: {yes_bid}¢\n• Yes Ask: {yes_ask}¢"
        ctypes.windll.user32.MessageBoxW(0, alert_text, "Kalshi Alert Desk", 0x40)
    else:
        print("No open markets found.")
else:
    print(f"Kalshi request failed: {res.status_code}")
