"""
Cysic Prover - Signing Module

CONFIRMED from reverse engineering:
1. ALL signatures use EthPersonalSign (\\x19Ethereum Signed Message:\\n prefix)
2. JSON includes empty "sign":"" field when signing
3. Key order follows struct definition (NOT alphabetically sorted)
4. Chain TX: keccak256(SignDoc bytes) -> secp256k1 (raw 65 bytes)
"""

import json
import base64
import time
import uuid
from eth_keys import keys
from eth_utils import keccak
import bech32
from eth_account import Account


class CysicSigner:
    def __init__(self, private_key_hex: str):
        pk_hex = private_key_hex.strip().replace('0x', '')
        self.pk_obj = keys.PrivateKey(bytes.fromhex(pk_hex))
        self.eth_account = Account.from_key(pk_hex)
        self.eth_address = self.eth_account.address
        self.compressed_pub = self.pk_obj.public_key.to_compressed_bytes()

        five = bech32.convertbits(bytes.fromhex(self.eth_address.lower().replace('0x', '')), 8, 5)
        self.cysic_address = bech32.bech32_encode("cysic", five)

    def sign_api(self, data: dict) -> str:
        """
        EthPersonalSign. JSON key order preserved (not sorted).
        JSON includes empty "sign":"" field.
        """
        msg = json.dumps(data, separators=(',', ':'))
        msg_bytes = msg.encode()
        prefix = b'\x19Ethereum Signed Message:\n' + str(len(msg_bytes)).encode()
        prefixed_msg = prefix + msg_bytes
        msg_hash = keccak(prefixed_msg)
        sig = self.pk_obj.sign_msg_hash(msg_hash)
        sig_bytes = sig.to_bytes()
        final_sig = sig_bytes[:64] + bytes([sig_bytes[64] + 27])
        return base64.b64encode(final_sig).decode()

    def sign_tx(self, sign_doc_bytes: bytes) -> bytes:
        """Chain TX: keccak256(SignDoc) -> secp256k1"""
        msg_hash = keccak(sign_doc_bytes)
        sig = self.pk_obj.sign_msg_hash(msg_hash)
        return sig.to_bytes()

    # ==================== WebSocket ====================

    def make_heartbeat(self) -> dict:
        data = {
            "clientType": 1,
            "clientVersion": "1.1.0",
            "workerAddress": self.eth_address,
            "timestamp": int(time.time()),
            "nonce": str(uuid.uuid4()),
            "sign": "",
        }
        data["sign"] = self.sign_api(data)
        return data

    def make_bid(self, task_hash: str, bid_price: str) -> dict:
        data = {
            "taskHash": task_hash,
            "prover": self.eth_address,
            "bid": bid_price,
            "sign": "",
        }
        data["sign"] = self.sign_api(data)
        return data

    def make_work_notify(self) -> dict:
        data = {
            "clientType": 1,
            "clientVersion": "1.1.0",
            "workerAddress": self.eth_address,
            "timestamp": int(time.time()),
            "nonce": str(uuid.uuid4()),
            "sign": "",
        }
        data["sign"] = self.sign_api(data)
        return data

    # ==================== HTTP API ====================

    def make_next_step(self, task_hash: str) -> dict:
        """Key order: prover, taskHash, sign"""
        data = {"prover": self.eth_address, "taskHash": task_hash, "sign": ""}
        data["sign"] = self.sign_api(data)
        return data

    def make_submit_data_raw(self, task_hash: str, proof_data: str) -> dict:
        """Key order: taskHash, prover, proofData, sign"""
        payload = {
            "taskHash": task_hash,
            "prover": self.eth_address,
            "proofData": proof_data,
            "sign": "",
        }
        payload["sign"] = self.sign_api(payload)
        return payload
