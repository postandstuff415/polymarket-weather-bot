import requests

TELEGRAM_BOT_TOKEN = "8934544953:AAFxmnh-fCh4pSdzd3DygV2K1Sd1gb-l4pM"
TELEGRAM_CHAT_ID = "8453672740"

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    res = requests.post(url, json=payload)
    print("Telegram response:", res.json())

def check_kalshi():
    # Pulls the most active open contract currently on Kalshi
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
            
            msg = f"Kalshi Desk Alert\n\n{title} ({ticker})\n• Yes Bid: {yes_bid}¢\n• Yes Ask: {yes_ask}¢"
            send_telegram_alert(msg)
        else:
            print("No open markets found.")
    else:
        print(f"Kalshi request failed with status: {res.status_code}")

if __name__ == "__main__":
    check_kalshi()