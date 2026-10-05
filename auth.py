import os
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
