"""
Cysic Prover - Signing Module

Two types of signing:
1. API/WebSocket signing: EthPersonalSign (Ethereum prefix + keccak256 + secp256k1)
2. Chain TX signing: keccak256(SignDoc bytes) → secp256k1 (raw 65 bytes)
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

        # Derive cysic bech32 address
        five = bech32.convertbits(bytes.fromhex(self.eth_address.lower().replace('0x', '')), 8, 5)
        self.cysic_address = bech32.bech32_encode("cysic", five)

    def sign_api(self, data: dict) -> str:
        """
        EthPersonalSign for ALL API/WebSocket signatures.
        Process: sort keys → JSON compact → add Ethereum prefix → keccak256 → ECDSA → v+27 → base64

        Confirmed from reverse engineering: ALL signatures use EthPersonalSign
        (wsSendRegisterInfoToServer, wsSendHeartbeatToServer, WsSendStartWorkToServer,
         WsSendFinishWorkToServer, queryTaskNextStep, submitTaskDataRaw, wsSendSubmitBidToServer)
        """
        msg = json.dumps(dict(sorted(data.items())), separators=(',', ':'))
        # Ethereum Personal Sign: "\x19Ethereum Signed Message:\n" + len(msg) + msg
        prefix = f"\x19Ethereum Signed Message:\n{len(msg)}"
        prefixed_msg = prefix.encode() + msg.encode()
        msg_hash = keccak(prefixed_msg)
        sig = self.pk_obj.sign_msg_hash(msg_hash)
        sig_bytes = sig.to_bytes()
        final_sig = sig_bytes[:64] + bytes([sig_bytes[64] + 27])
        return base64.b64encode(final_sig).decode()

    def sign_tx(self, sign_doc_bytes: bytes) -> bytes:
        """
        Sign for Cosmos chain transactions.
        Process: keccak256(SignDoc serialized bytes) → ECDSA → raw 65 bytes
        """
        msg_hash = keccak(sign_doc_bytes)
        sig = self.pk_obj.sign_msg_hash(msg_hash)
        return sig.to_bytes()

    # ==================== WebSocket message builders ====================

    def make_heartbeat(self) -> dict:
        """action=2: heartbeat/registration"""
        data = {
            "clientType": 1,
            "clientVersion": "1.1.0",
            "nonce": str(uuid.uuid4()),
            "timestamp": int(time.time()),
            "workerAddress": self.eth_address,
        }
        data["sign"] = self.sign_api(data)
        return data

    def make_bid(self, task_hash: str, bid_price: str) -> dict:
        """action=6: bid submission"""
        data = {
            "taskHash": task_hash,
            "prover": self.eth_address,
            "bid": bid_price,
        }
        data["sign"] = self.sign_api(data)
        return data

    def make_work_notify(self) -> dict:
        """action=4 (start work) or action=5 (finish work)"""
        data = {
            "clientType": 1,
            "clientVersion": "1.1.0",
            "nonce": str(uuid.uuid4()),
            "timestamp": int(time.time()),
            "workerAddress": self.eth_address,
        }
        data["sign"] = self.sign_api(data)
        return data

    # ==================== API message builders ====================

    def make_next_step(self, task_hash: str) -> dict:
        """nextStep API request"""
        data = {"prover": self.eth_address, "taskHash": task_hash}
        data["sign"] = self.sign_api(data)
        return data

    def make_submit_data_raw(self, task_hash: str, proof_data: str) -> dict:
        """submitTaskDataRaw API request - sign ALL fields including proofData"""
        payload = {
            "taskHash": task_hash,
            "prover": self.eth_address,
            "proofData": proof_data,
        }
        payload["sign"] = self.sign_api(payload)
        return payload
