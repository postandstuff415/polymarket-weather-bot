import asyncio
import time
import re
import aiohttp

AAA_STATE_URL = "https://gasprices.aaa.com/todays-state-averages/"
AAA_NATIONAL_URL = "https://gasprices.aaa.com/"

# Regex to match State name and Regular Unleaded price from AAA table rows
STATE_ROW_REGEX = re.compile(
    r'<td[^>]*>\s*(?:<a[^>]*>)?([A-Za-z\s]+?)(?:</a>)?\s*</td>\s*<td[^>]*>\s*\$([\d\.]+)',
    re.IGNORECASE
)

# Regex to extract National Regular Unleaded Average
NATIONAL_REGEX = re.compile(
    r'Current Avg\.\s*</td>\s*<td[^>]*>\s*\$([\d\.]+)',
    re.IGNORECASE
)

async def fetch_gas_prices(session: aiohttp.ClientSession) -> dict | None:
    """
    Fetches the latest daily state and national averages directly from AAA.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    
    t0 = time.perf_counter()
    state_prices = {}
    
    try:
        # 1. Fetch all 50 State Averages in one single request
        async with session.get(AAA_STATE_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                html = await resp.text()
                matches = STATE_ROW_REGEX.findall(html)
                for state, price in matches:
                    clean_state = state.strip()
                    if clean_state and clean_state not in ["State", "Regular", "Mid-Grade", "Premium", "Diesel"]:
                        try:
                            state_prices[clean_state] = float(price)
                        except ValueError:
                            continue
            else:
                print(f"[!] AAA State Page HTTP Error: {resp.status}")

        # 2. Fetch National Average
        national_price = None
        async with session.get(AAA_NATIONAL_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                html = await resp.text()
                nat_match = NATIONAL_REGEX.search(html)
                if nat_match:
                    national_price = float(nat_match.group(1))

        latency_ms = (time.perf_counter() - t0) * 1000

        if state_prices:
            return {
                "category": "GAS",
                "national_avg": national_price,
                "states": state_prices,
                "total_states_parsed": len(state_prices),
                "latency_ms": round(latency_ms, 2),
                "timestamp": time.time()
            }

    except Exception as e:
        print(f"[!] Fetch failed for AAA: {e}")

    return None

async def main():
    print("[*] Fetching AAA State & National Fuel Gauges in a single request...")
    async with aiohttp.ClientSession() as session:
        result = await fetch_gas_prices(session)
        if result:
            print("\n--- [AAA FUEL GAUGE CAPTURE SUCCESS] ---")
            print(f"Total States Parsed : {result['total_states_parsed']}")
            print(f"US National Average : ${result['national_avg']}")
            print(f"Roundtrip Time      : {result['latency_ms']} ms")
            print("\nSample Key State Regular Prices:")
            for state in ["California", "Texas", "Florida", "Ohio", "Michigan", "North Carolina"]:
                price = result["states"].get(state, "N/A")
                print(f"  - {state:15} : ${price}")
            print("----------------------------------------\n")
        else:
            print("[X] Failed to parse AAA gas data.")

if __name__ == "__main__":
    asyncio.run(main())