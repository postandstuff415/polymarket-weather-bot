"""
Kalshi Market Maker & Satellite Sniping Orchestrator
CLI Modes:
  python orchestrator.py         -> Safe Simulation (Dry-Run)
  python orchestrator.py --live  -> Live Execution (Real Orders)
"""

import asyncio
from datetime import datetime
import sys
import time
import aiohttp

from collectors.sofr_collector import fetch_sofr
from collectors.weather_collector import fetch_temperature
from collectors.aaa_collector import fetch_gas_prices
from strategy.pricing_engine import evaluate_contract, evaluate_satellite_edge
from strategy.order_manager import OrderManager

# Dynamic date tag generation (e.g. '26SEP28', '26SEP29')
DATE_TAG = datetime.now().strftime("%y%b%d").upper()

WATCHLIST = {
    "SOFR": {
        "ticker": f"KXSOFR-{DATE_TAG}-3.87",
        "strike": 3.87,
        "type": "exact",
        "market_ask": 0.50,
        "satellite_budget": 10.00,
        "maker_count": 20
    },
    "GAS_US": {
        "ticker": f"KXGASNAT-{DATE_TAG}-T4.4600",
        "strike": 4.4600,
        "type": "above",
        "market_ask": 0.42,
        "satellite_budget": 10.00,
        "maker_count": 15
    },
    "GAS_CA": {
        "ticker": f"KXGASCAL-{DATE_TAG}-T6.3700",
        "strike": 6.3700,
        "type": "above",
        "market_ask": 0.48,
        "satellite_budget": 10.00,
        "maker_count": 15
    },
    "WEATHER_MIA": {
        "ticker": f"KXTEMPMIA-{DATE_TAG}-T85.0",
        "station": "KMIA",
        "strike": 85.0,
        "type": "above",
        "market_ask": 0.60,
        "satellite_budget": 10.00,
        "maker_count": 10
    }
}

async def sofr_worker(session: aiohttp.ClientSession, queue: asyncio.Queue):
    result = await fetch_sofr(session)
    if result:
        await queue.put({"source": "SOFR", "data": result})

async def weather_worker(session: aiohttp.ClientSession, station: str, queue: asyncio.Queue):
    result = await fetch_temperature(session, station)
    if result:
        await queue.put({"source": "WEATHER", "data": result})

async def gas_worker(session: aiohttp.ClientSession, queue: asyncio.Queue):
    result = await fetch_gas_prices(session)
    if result:
        await queue.put({"source": "GAS", "data": result})

def handle_execution(cfg: dict, eval_res: dict, order_mgr: OrderManager):
    """Executes Satellite Snipe (if edge qualifies) + Core Market Making."""
    # 1. SATELLITE: Check for Outlier Mispricing Edge
    satellite = evaluate_satellite_edge(eval_res, cfg["market_ask"], satellite_budget_usd=cfg["satellite_budget"])
    if satellite:
        print(f"  [+] Satellite Outlier Edge Detected: {int(satellite['entry_price']*100)}¢ (Risk: ${satellite['capital_at_risk']} / Max Return: +${satellite['projected_profit']})")
        sat_sweep = order_mgr.build_ioc_sweep_order(
            ticker=cfg["ticker"],
            target_side=satellite["target_side"],
            max_price=satellite["entry_price"],
            contract_count=satellite["contract_count"]
        )
        order_mgr.dispatch_order(sat_sweep)
    
    # 2. CORE: Post Resting Market Maker Quote
    maker = order_mgr.build_maker_quote_order(
        ticker=cfg["ticker"],
        target_side=eval_res["target_side"],
        bid_price=eval_res["maker_bid_price"],
        contract_count=cfg["maker_count"]
    )
    order_mgr.dispatch_order(maker)

async def execution_consumer(queue: asyncio.Queue, order_mgr: OrderManager, total_expected: int):
    processed = 0
    print(f"\n--- [CORE + SATELLITE PIPELINE ENGAGED | ACTIVE DATE: {DATE_TAG}] ---")

    while processed < total_expected:
        event = await queue.get()
        source = event["source"]
        data = event["data"]
        processed += 1
        t_start = time.perf_counter()

        print(f"\n[>>> EVENT RECEIVED: {source}] (Collector Latency: {data.get('latency_ms')} ms)")

        if source == "SOFR":
            cfg = WATCHLIST["SOFR"]
            eval_res = evaluate_contract("SOFR", data["rate"], cfg["strike"], cfg["type"])
            handle_execution(cfg, eval_res, order_mgr)

        elif source == "WEATHER":
            cfg = WATCHLIST["WEATHER_MIA"]
            eval_res = evaluate_contract("WEATHER", data["temp_f"], cfg["strike"], cfg["type"])
            handle_execution(cfg, eval_res, order_mgr)

        elif source == "GAS":
            if data.get("national_avg"):
                cfg_us = WATCHLIST["GAS_US"]
                eval_us = evaluate_contract("GAS", data["national_avg"], cfg_us["strike"], cfg_us["type"])
                handle_execution(cfg_us, eval_us, order_mgr)

            ca_price = data["states"].get("California")
            if ca_price:
                cfg_ca = WATCHLIST["GAS_CA"]
                eval_ca = evaluate_contract("GAS", ca_price, cfg_ca["strike"], cfg_ca["type"])
                handle_execution(cfg_ca, eval_ca, order_mgr)

        pipeline_lat = round((time.perf_counter() - t_start) * 1000, 3)
        print(f"    └─ Routing Latency: {pipeline_lat} ms")
        queue.task_done()

async def main():
    is_live = "--live" in sys.argv
    if is_live:
        print("[!] =========================================")
        print("[!] WARNING: LIVE EXECUTION MODE (REAL TRADES)")
        print(f"[!] Target Board Date Tag: {DATE_TAG}")
        print("[!] =========================================")
    else:
        print(f"[*] Running in DRY-RUN Mode (Active Tag: {DATE_TAG})")
        print("[*] (Pass --live to execute real orders)")

    event_queue = asyncio.Queue()
    order_mgr = OrderManager(dry_run=not is_live)

    async with aiohttp.ClientSession() as session:
        producer_tasks = [
            asyncio.create_task(sofr_worker(session, event_queue)),
            asyncio.create_task(weather_worker(session, "KMIA", event_queue)),
            asyncio.create_task(gas_worker(session, event_queue))
        ]
        
        consumer_task = asyncio.create_task(
            execution_consumer(event_queue, order_mgr, total_expected=len(producer_tasks))
        )

        await asyncio.gather(*producer_tasks)
        await consumer_task

    print("\n[✓] Pipeline Cycle Complete.\n")

if __name__ == "__main__":
    asyncio.run(main())
