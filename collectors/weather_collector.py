import asyncio
import time
import aiohttp

NWS_STATION_URL = "https://api.weather.gov/stations/{station}/observations/latest"

async def fetch_temperature(session: aiohttp.ClientSession, station: str) -> dict | None:
    """
    Fetches the latest official ASOS METAR observation for a specific airport
    station from the National Weather Service API.
    """
    # NWS requires a descriptive User-Agent with contact/app info
    headers = {
        "User-Agent": "(KalshiMarketMakerEngine/1.0, contact@kalshimarketmaker.internal)",
        "Accept": "application/geo+json",
        "Cache-Control": "no-cache",
    }
    
    url = NWS_STATION_URL.format(station=station)
    t0 = time.perf_counter()
    
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                latency_ms = (time.perf_counter() - t0) * 1000
                
                props = data.get("properties", {})
                temp_obj = props.get("temperature", {})
                temp_c = temp_obj.get("value")
                obs_timestamp = props.get("timestamp")
                station_name = props.get("station", station)
                
                if temp_c is not None:
                    # Convert Celsius to Fahrenheit
                    temp_f = round((temp_c * 9 / 5) + 32, 2)
                    return {
                        "category": "WEATHER",
                        "station": station,
                        "temp_f": temp_f,
                        "temp_c": round(temp_c, 2),
                        "observation_time": obs_timestamp,
                        "latency_ms": round(latency_ms, 2),
                        "timestamp": time.time()
                    }
                else:
                    print(f"[!] Observation missing temperature value for {station}")
            else:
                print(f"[!] NWS HTTP Error: {resp.status} for {station}")
    except Exception as e:
        print(f"[!] Fetch failed for {station}: {e}")
        
    return None

async def main():
    # Test primary Kalshi contract stations: Chicago (KORD), Austin (KAUS), Miami (KMIA)
    test_stations = ["KORD", "KAUS", "KMIA"]
    print(f"[*] Testing live NWS observations across stations: {', '.join(test_stations)}...")
    
    async with aiohttp.ClientSession() as session:
        for station in test_stations:
            result = await fetch_temperature(session, station)
            if result:
                print(f"\n--- [NWS WEATHER CAPTURE: {result['station']}] ---")
                print(f"Current Temp   : {result['temp_f']} °F ({result['temp_c']} °C)")
                print(f"Recorded At    : {result['observation_time']}")
                print(f"Roundtrip Time : {result['latency_ms']} ms")
            else:
                print(f"[X] Failed to parse temperature for {station}.")
    print("\n-------------------------------------------------")

if __name__ == "__main__":
    asyncio.run(main())