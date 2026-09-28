import asyncio
import time
import aiohttp

NY_FED_URL = "https://markets.newyorkfed.org/api/rates/all/latest.json"

async def fetch_sofr(session: aiohttp.ClientSession) -> dict | None:
    """
    Fetches the latest reference rates directly from the New York Fed API
    and extracts the official SOFR print.
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    }
    
    t0 = time.perf_counter()
    try:
        # Use clean endpoint without query parameters
        async with session.get(NY_FED_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                payload = await resp.json()
                latency_ms = (time.perf_counter() - t0) * 1000
                
                rates = payload.get("refRates", [])
                for rate in rates:
                    r_type = rate.get("type") or rate.get("rateType")
                    if r_type == "SOFR":
                        raw_rate = rate.get("percentRate") or rate.get("ratePercent")
                        effective_date = rate.get("effectiveDate")
                        
                        return {
                            "category": "SOFR",
                            "rate": float(raw_rate),
                            "effective_date": effective_date,
                            "latency_ms": round(latency_ms, 2),
                            "timestamp": time.time()
                        }
            else:
                print(f"[!] NY Fed HTTP Error: {resp.status}")
    except Exception as e:
        print(f"[!] Fetch failed: {e}")
        
    return None

async def main():
    print("[*] Testing clean connection to Federal Reserve Bank of New York...")
    async with aiohttp.ClientSession() as session:
        result = await fetch_sofr(session)
        if result:
            print("\n--- [NY FED SOFR CAPTURE SUCCESS] ---")
            print(f"Rate Value     : {result['rate']}%")
            print(f"Effective Date : {result['effective_date']}")
            print(f"Roundtrip Time : {result['latency_ms']} ms")
            print("--------------------------------------\n")
        else:
            print("[X] Failed to parse SOFR from NY Fed endpoint.")

if __name__ == "__main__":
    asyncio.run(main())
