"""
Cysic Prover - Chain Client

Handles all Cosmos SDK chain transactions:
- MsgAccept: Accept a task
- MsgSubmitHash: Submit proof SHA256 hash
- MsgSubmitData: Submit proof data URL

Uses Ethermint-style signing (keccak256 + secp256k1)
"""

import base64
import time
import logging
import requests

from google.protobuf import any_pb2
from cosmpy.protos.cosmos.base.v1beta1.coin_pb2 import Coin
from cosmpy.protos.cosmos.crypto.secp256k1.keys_pb2 import PubKey as Secp256k1PubKey
from cosmpy.protos.cosmos.tx.v1beta1.tx_pb2 import (
    TxBody, AuthInfo, SignerInfo, ModeInfo, Fee, TxRaw, SignDoc
)
from cosmpy.protos.cosmos.tx.signing.v1beta1.signing_pb2 import SIGN_MODE_DIRECT

from signer import CysicSigner

logger = logging.getLogger(__name__)


def _varint(v):
    res = []
    while True:
        b = v & 0x7f
        v >>= 7
        if v:
            res.append(b | 0x80)
        else:
            res.append(b)
            break
    return bytes(res)


def encode_string_field(field_number, value):
    """Protobuf string field encoding"""
    tag = (field_number << 3) | 2
    vb = value.encode('utf-8')
    return _varint(tag) + _varint(len(vb)) + vb


def encode_bytes_field(field_number, value_bytes):
    """Protobuf bytes field encoding"""
    tag = (field_number << 3) | 2
    return _varint(tag) + _varint(len(value_bytes)) + value_bytes


class ChainClient:
    def __init__(self, signer: CysicSigner, config: dict):
        self.signer = signer
        self.rpc_url = config["server"]["rpc_url"]
        self.rest_url = config["server"]["rest_url"]
        self.chain_id = config["chain"]["chain_id"]
        self.denom = config["chain"]["denom"]
        self.gas_limit = config["chain"]["gas_limit"]
        self.gas_fee = config["chain"]["gas_fee"]
        self.pubkey_type_url = config["chain"]["pubkey_type_url"]

    def get_account_info(self):
        """Get account_number and sequence from chain"""
        url = f"{self.rest_url}/cosmos/auth/v1beta1/accounts/{self.signer.cysic_address}"
        try:
            r = requests.get(url, timeout=20)
            data = r.json()
            acc = data.get('account', {})
            if 'base_account' in acc:
                acc = acc['base_account']
            return int(acc.get('account_number', 0)), int(acc.get('sequence', 0))
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            return None, None

    def wait_for_tx(self, tx_hash, timeout=60):
        """Poll for transaction confirmation"""
        url = f"{self.rest_url}/cosmos/tx/v1beta1/txs/{tx_hash}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = requests.get(url, timeout=20)
                if r.status_code == 200:
                    j = r.json()
                    if "tx_response" in j:
                        return True, j["tx_response"]
            except:
                pass
            time.sleep(2)
        return False, None

    def _broadcast(self, msg_type_url, msg_value, memo=""):
        """Build, sign, and broadcast a transaction"""
        for attempt in range(5):
            acc_num, seq = self.get_account_info()
            if acc_num is None:
                time.sleep(3)
                continue

            # PubKey
            pk_any = any_pb2.Any(
                type_url=self.pubkey_type_url,
                value=Secp256k1PubKey(key=self.signer.compressed_pub).SerializeToString()
            )

            # AuthInfo
            auth_info = AuthInfo(
                signer_infos=[SignerInfo(
                    public_key=pk_any,
                    mode_info=ModeInfo(single=ModeInfo.Single(mode=SIGN_MODE_DIRECT)),
                    sequence=seq
                )],
                fee=Fee(
                    amount=[Coin(denom=self.denom, amount=self.gas_fee)],
                    gas_limit=self.gas_limit
                )
            )
            auth_bytes = auth_info.SerializeToString()

            # TxBody
            tx_body = TxBody(messages=[any_pb2.Any(
                type_url=msg_type_url,
                value=msg_value
            )], memo=memo)

            # SignDoc
            sign_doc = SignDoc(
                body_bytes=tx_body.SerializeToString(),
                auth_info_bytes=auth_bytes,
                chain_id=self.chain_id,
                account_number=acc_num
            )

            # Sign
            sig = self.signer.sign_tx(sign_doc.SerializeToString())

            # TxRaw
            tx_raw = TxRaw(
                body_bytes=sign_doc.body_bytes,
                auth_info_bytes=auth_bytes,
                signatures=[sig]
            )
            tx_b64 = base64.b64encode(tx_raw.SerializeToString()).decode("utf-8")

            # Broadcast
            try:
                r = requests.post(self.rpc_url, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "broadcast_tx_sync",
                    "params": [tx_b64]
                }, timeout=20)
                res = r.json()
            except Exception as e:
                logger.error(f"Broadcast failed: {e}")
                time.sleep(3)
                continue

            if "result" not in res or "hash" not in res.get("result", {}):
                logger.error(f"RPC rejected: {res}")
                time.sleep(3)
                continue

            tx_hash = res["result"]["hash"]
            logger.info(f"Broadcast tx={tx_hash[:16]}... waiting...")

            ok, txr = self.wait_for_tx(tx_hash)
            if ok:
                code = int(txr.get("code", 0))
                if code == 0:
                    logger.info(f"TX confirmed: {tx_hash}")
                    return True, tx_hash
                else:
                    raw_log = txr.get('raw_log', '')
                    logger.error(f"TX failed: code={code} log={raw_log}")
                    if "sequence" in raw_log.lower():
                        time.sleep(2)
                        continue
                    return False, raw_log
            else:
                logger.error(f"TX timeout: {tx_hash}")
                return False, "timeout"

        return False, "max retries"

    # ==================== Transaction Types ====================

    def accept_task(self, task_hash: str):
        """
        MsgAccept: /cysicmint.zktask.v1.MsgAccept
        Fields: task_hash(1), sender(2)
        """
        msg_value = (
            encode_string_field(1, task_hash) +
            encode_string_field(2, self.signer.cysic_address)
        )
        logger.info(f"AcceptTask: {task_hash[:16]}...")
        return self._broadcast("/cysicmint.zktask.v1.MsgAccept", msg_value)

    def submit_hash(self, task_hash: str, proof_hash_b64: str):
        """
        MsgSubmitHash: /cysicmint.zktask.v1.MsgSubmitHash
        Fields: task_hash(1), proof_hash(2 bytes), sender(3)
        """
        proof_hash_bytes = base64.b64decode(proof_hash_b64)
        msg_value = (
            encode_string_field(1, task_hash) +
            encode_bytes_field(2, proof_hash_bytes) +
            encode_string_field(3, self.signer.cysic_address)
        )
        logger.info(f"SubmitHash: {task_hash[:16]}... hash={proof_hash_b64[:20]}...")
        return self._broadcast("/cysicmint.zktask.v1.MsgSubmitHash", msg_value)

    def submit_data(self, task_hash: str, proof_url: str):
        """
        MsgSubmitData: /cysicmint.zktask.v1.MsgSubmitData
        Fields: task_hash(1), proof_data_url(2), sender(3)
        """
        msg_value = (
            encode_string_field(1, task_hash) +
            encode_string_field(2, proof_url) +
            encode_string_field(3, self.signer.cysic_address)
        )
        logger.info(f"SubmitData: {task_hash[:16]}... url={proof_url[:50]}...")
        return self._broadcast("/cysicmint.zktask.v1.MsgSubmitData", msg_value)
