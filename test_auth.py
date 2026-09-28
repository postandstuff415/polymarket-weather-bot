import time
import base64
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# ==========================================
# CREDENTIALS
# ==========================================
API_KEY_ID = "f3c190be-f642-4cad-a673-7a5903a4591e"
KEY_FILE_PATH = "kalshi_key.pem"
BASE_URL = "https://external-api.kalshi.com"

# 1. Load Private Key from Disk
with open(KEY_FILE_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None)

# 2. Cryptographic Request Signer
def sign_request(method: str, path: str, timestamp_str: str) -> str:
    message = f"{timestamp_str}{method}{path}".encode("utf-8")
    
    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(message)
    else:
        # Standard RSA-PSS with SHA256
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode("utf-8")

# 3. Authenticated GET Request
def get_portfolio_balance():
    path = "/trade-api/v2/portfolio/balance"
    timestamp_str = str(int(time.time() * 1000))
    signature = sign_request("GET", path, timestamp_str)

    headers = {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_str,
        "Content-Type": "application/json",
    }

    response = requests.get(f"{BASE_URL}{path}", headers=headers)
    return response

if __name__ == "__main__":
    print("[*] Testing Kalshi Authenticated Handshake...")
    res = get_portfolio_balance()
    print(f"Status Code: {res.status_code}")
    print("Response Body:", res.json())
