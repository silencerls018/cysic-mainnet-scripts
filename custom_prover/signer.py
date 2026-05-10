"""
Cysic Prover Signing Module
Implements secp256k1 ECDSA signing with keccak256 hash (Ethereum-style)
"""

import json
import base64
import time
import uuid
from eth_keys import keys
from eth_utils import keccak


class CysicSigner:
    def __init__(self, private_key_hex: str, worker_address: str):
        """
        Initialize signer with private key.
        
        Args:
            private_key_hex: Private key in hex format (no 0x prefix)
            worker_address: Worker address (e.g. 0xADcA5d33...)
        """
        self.private_key = keys.PrivateKey(bytes.fromhex(private_key_hex))
        self.worker_address = worker_address

    def sign_data(self, data: dict) -> str:
        """
        Sign a data dictionary.
        
        Process:
        1. Sort keys alphabetically
        2. JSON serialize with no spaces (separators=(',',':'))
        3. keccak256 hash
        4. ECDSA sign
        5. Append v+27
        6. Base64 encode
        
        Args:
            data: Dictionary to sign (should NOT contain 'sign' field)
            
        Returns:
            Base64 encoded signature string
        """
        # Sort keys and serialize
        msg = json.dumps(dict(sorted(data.items())), separators=(',', ':'))
        
        # keccak256 hash
        msg_hash = keccak(text=msg)
        
        # ECDSA sign
        sig = self.private_key.sign_msg_hash(msg_hash)
        
        # Ethereum standard: v + 27
        sig_bytes = sig.to_bytes()
        final_sig = sig_bytes[:64] + bytes([sig_bytes[64] + 27])
        
        return base64.b64encode(final_sig).decode()

    def make_heartbeat_data(self) -> dict:
        """Generate heartbeat/registration data with signature."""
        data = {
            "clientType": 1,
            "clientVersion": "1.1.0",
            "nonce": str(uuid.uuid4()),
            "timestamp": int(time.time()),
            "workerAddress": self.worker_address,
        }
        sign = self.sign_data(data)
        data["sign"] = sign
        return data

    def make_bid_data(self, task_hash: str, bid_price: str) -> dict:
        """
        Generate bid submission data with signature.
        
        Args:
            task_hash: Hash of the task to bid on
            bid_price: Bid price as string (e.g. "0.01")
        """
        data = {
            "taskHash": task_hash,
            "bid": bid_price,
            "workerAddress": self.worker_address,
            "timestamp": int(time.time()),
            "nonce": str(uuid.uuid4()),
        }
        sign = self.sign_data(data)
        data["sign"] = sign
        return data

    def make_accept_data(self, task_hash: str) -> dict:
        """Generate accept task data with signature."""
        data = {
            "taskHash": task_hash,
            "workerAddress": self.worker_address,
            "timestamp": int(time.time()),
            "nonce": str(uuid.uuid4()),
        }
        sign = self.sign_data(data)
        data["sign"] = sign
        return data

    def make_start_work_data(self, task_hash: str) -> dict:
        """Generate start work notification data."""
        data = {
            "taskHash": task_hash,
            "workerAddress": self.worker_address,
            "timestamp": int(time.time()),
            "nonce": str(uuid.uuid4()),
        }
        sign = self.sign_data(data)
        data["sign"] = sign
        return data

    def make_finish_work_data(self, task_hash: str) -> dict:
        """Generate finish work notification data."""
        data = {
            "taskHash": task_hash,
            "workerAddress": self.worker_address,
            "timestamp": int(time.time()),
            "nonce": str(uuid.uuid4()),
        }
        sign = self.sign_data(data)
        data["sign"] = sign
        return data

    def make_submit_hash_data(self, task_hash: str, proof_hash: str) -> dict:
        """Generate submit proof hash data with signature."""
        data = {
            "taskHash": task_hash,
            "proofHash": proof_hash,
            "prover": self.worker_address,
            "timestamp": int(time.time()),
            "nonce": str(uuid.uuid4()),
        }
        sign = self.sign_data(data)
        data["sign"] = sign
        return data

    def make_submit_data_raw(self, task_hash: str, proof_data: str) -> dict:
        """
        Generate submit proof data payload with signature.
        
        Args:
            task_hash: Task hash
            proof_data: Base64 encoded proof data
        """
        data = {
            "taskHash": task_hash,
            "prover": self.worker_address,
            "proofData": proof_data,
        }
        sign = self.sign_data(data)
        data["sign"] = sign
        return data

    def make_next_step_data(self, task_hash: str, step: int) -> dict:
        """Generate nextStep request data."""
        data = {
            "taskHash": task_hash,
            "workerAddress": self.worker_address,
            "step": step,
            "timestamp": int(time.time()),
            "nonce": str(uuid.uuid4()),
        }
        sign = self.sign_data(data)
        data["sign"] = sign
        return data
