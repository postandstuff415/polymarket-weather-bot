import time
import requests
from dotenv import load_dotenv
from strategy.order_manager import OrderManager

load_dotenv()
om = OrderManager(live=True)

print(f"[*] Target Endpoint: {om.base_url}")

# 1. Configure 25% Target Balance Allocation across Shards 0, 1, 2, and 3
payload = {
    "allocations": [
        {"exchange_index": 0, "percent": 25},
        {"exchange_index": 1, "percent": 25},
        {"exchange_index": 2, "percent": 25},
        {"exchange_index": 3, "percent": 25}
    ],
    "resting_margin_reservation": "none"
}

path_alloc = "/trade-api/v2/portfolio/target_balance_allocation"
headers_alloc = om.auth_client.get_headers("POST", path_alloc)
headers_alloc["Content-Type"] = "application/json"

print("\n[*] Setting 25% Equal Split Policy Across Shards 0, 1, 2, 3...")
resp = requests.post(f"{om.base_url}/portfolio/target_balance_allocation", json=payload, headers=headers_alloc)
print(f"[✓] Policy Status {resp.status_code}: {resp.text}")

# 2. Instant Direct Transfers from Shard 0 to Shards 1, 2, and 3
# Kalshi expects transfer amounts in centicents ($1.00 = 10,000 centicents)
transfer_cents = 7400000  # $740.00 in centicents per destination shard
path_xfer = "/trade-api/v2/portfolio/intra_exchange_instance_transfer"

print("\n[*] Executing Instant Capital Transfers to Shards 1, 2, and 3...")
for target_shard in [1, 2, 3]:
    xfer_body = {
        "amount": transfer_cents,
        "source_exchange_shard": 0,
        "destination_exchange_shard": target_shard
    }
    headers_xfer = om.auth_client.get_headers("POST", path_xfer)
    headers_xfer["Content-Type"] = "application/json"
    
    x_resp = requests.post(f"{om.base_url}/portfolio/intra_exchange_instance_transfer", json=xfer_body, headers=headers_xfer)
    print(f" • Shard 0 -> Shard {target_shard} ($740.00): HTTP {x_resp.status_code}")

print("\n[*] Pausing 5 seconds for ledger confirmation...")
time.sleep(5)

# 3. Audit Verified Balances Across All 4 Shards
print("\n" + "="*50)
print("       VERIFIED ACTIVE LEDGER ACROSS ALL SHARDS")
print("="*50)
path_bal = "/trade-api/v2/portfolio/balance"
for shard in [0, 1, 2, 3]:
    headers_bal = om.auth_client.get_headers("GET", path_bal)
    b_resp = requests.get(f"{om.base_url}/portfolio/balance", params={"exchange_index": shard}, headers=headers_bal)
    if b_resp.status_code == 200:
        b_data = b_resp.json()
        cents = b_data.get("balance", 0)
        avail = b_data.get("available_to_trade", cents)
        print(f" • Shard {shard}: ${cents / 100:.2f} total (${avail / 100:.2f} available)")
    else:
        print(f" • Shard {shard}: HTTP {b_resp.status_code} - {b_resp.text}")
print("="*50 + "\n")