import os
import sys
import subprocess
import re
import base64
import time

# Close any hanging Notepad tabs to avoid file lock
os.system("taskkill /f /im notepad.exe 2>nul")

print("\n" + "=" * 55)
print("   KALSHI AUTOMATED KEY SETUP & LIVE AUTH VERIFIER   ")
print("=" * 55)
print("1. Go to your browser where iCloud Notes is open.")
print("2. Open the 8:08 PM note titled with your Key ID.")
print("3. Highlight the key (from -----BEGIN down through KEY-----)")
print("   and press Ctrl + C to copy it.")
print("4. Come back to this PowerShell window.\n")

input(">>> Press ENTER here AFTER you have copied the key: ")

# Read the clipboard via PowerShell
try:
    clip = subprocess.check_output(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], text=True)
except Exception as e:
    print(f"[!] Could not access clipboard: {e}")
    sys.exit(1)

# Clean Apple Notes smart punctuation (em-dashes, en-dashes, non-breaking spaces)
clean = clip.replace("\u2014", "-").replace("\u2013", "-").replace("\u00a0", " ").strip()

# Extract the key block
m = re.search(r"(-{3,}\s*BEGIN[^-]+PRIVATE KEY\s*-{3,}[\s\S]+?-{3,}\s*END[^-]+PRIVATE KEY\s*-{3,})", clean)
if not m:
    print("\n[!] ERROR: Could not find BEGIN / END private key markers on clipboard.")
    print("Clipboard started with:", repr(clean[:80]) if clean else "[CLIPBOARD IS EMPTY]")
    print("Please make sure you highlighted and copied (Ctrl+C) the key text in iCloud.")
    sys.exit(1)

raw_key = m.group(1).strip()
lines = raw_key.splitlines()
lines[0] = re.sub(r"^[-\s]*BEGIN", "-----BEGIN", lines[0])
lines[0] = re.sub(r"KEY[-\s]*$", "KEY-----", lines[0])
lines[-1] = re.sub(r"^[-\s]*END", "-----END", lines[-1])
lines[-1] = re.sub(r"KEY[-\s]*$", "KEY-----", lines[-1])
final_pem = "\n".join(lines).strip() + "\n"

# Verify using cryptography library
try:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    pkey = load_pem_private_key(final_pem.encode("utf-8"), password=None)
    key_type = "Ed25519" if isinstance(pkey, Ed25519PrivateKey) else "RSA"
    print(f"\n[✓] Key successfully verified! Detected algorithm: {key_type}")
except Exception as e:
    print(f"\n[!] Cryptography error parsing key: {e}")
    sys.exit(1)

# Write clean kalshi_key.pem in ASCII
with open("kalshi_key.pem", "w", encoding="utf-8") as f:
    f.write(final_pem)
print("[✓] kalshi_key.pem successfully written.")

# Write auth.py supporting both Ed25519 and RSA
auth_py_code = '''import os
import time
import base64
from pathlib import Path
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

load_dotenv(override=True)

class KalshiAuth:
    def __init__(self, key_id=None, private_key_path=None):
        self.key_id = key_id or os.getenv("KALSHI_KEY_ID", "").strip()
        key_path = private_key_path or os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
        
        if not self.key_id:
            raise ValueError("[!] KALSHI_KEY_ID is missing from .env")
        if not key_path or not Path(key_path).exists():
            raise FileNotFoundError(f"[!] Private key file not found at: {key_path}")

        with open(key_path, "rb") as key_file:
            raw_bytes = key_file.read()

        self.private_key = load_pem_private_key(raw_bytes, password=None)

    def sign_request(self, timestamp: str, method: str, path: str) -> str:
        clean_path = path.split("?")[0]
        message = f"{timestamp}{method.upper()}{clean_path}".encode("utf-8")

        if isinstance(self.private_key, Ed25519PrivateKey):
            signature = self.private_key.sign(message)
        else:
            signature = self.private_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256()
            )
        return base64.b64encode(signature).decode("utf-8")

    def get_headers(self, method: str, path: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        signature = self.sign_request(timestamp, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json"
        }
'''
with open("auth.py", "w", encoding="utf-8") as f:
    f.write(auth_py_code)
print("[✓] auth.py updated with dual Ed25519 / RSA signing.")

# Perform Live API Handshake
print("\n[*] Sending authenticated handshake to Kalshi Live API...")
try:
    import requests
    from auth import KalshiAuth
    auth = KalshiAuth()
    base_url = "https://api.elections.kalshi.com/trade-api/v2"
    path = "/trade-api/v2/portfolio/balance"
    headers = auth.get_headers("GET", path)

    res = requests.get(f"{base_url}/portfolio/balance", headers=headers, timeout=10)
    if res.status_code == 200:
        print("\n" + "=" * 55)
        print(" [✓] LIVE AUTHENTICATION SUCCESSFUL!")
        print(f" Account Balance: {res.json()}")
        print("=" * 55)
        print("\n>>> Run live execution now:")
        print("    python orchestrator.py --live\n")
    else:
        print(f"\n[X] Kalshi returned HTTP {res.status_code}: {res.text}")
except Exception as e:
    print(f"\n[!] Handshake failed: {e}")