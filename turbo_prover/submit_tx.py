#!/usr/bin/env python3
# ================================================================
# Cysic Prover 即时上报脚本
# 
# 功能：
#   1. 读取 proof 文件 → Base64 编码 → POST 到 API 获取 S3 URL
#   2. 拿到 URL 后 → 构造 MsgSubmitData → 广播链上交易
#
# 使用方式：
#   python3 submit_tx.py --hash <taskHash> --proof-files <path1>,<path2>
#
# 由内鬼脚本 (cargo-zisk-ghost.sh) 在 proof 就绪后立即调用
# ================================================================

import argparse
import json
import base64
import sys
import os
import datetime
import time
import requests

# ==================== 配置 ====================
PRIVATE_KEY_HEX = "8825d620e031a80f37cdd57d087da4062e5f41f9d29d5dbd594eeff073fb1f93"

RPC_URL = "https://rpc.cysic.xyz"
REST_URL = "https://rest.cysic.xyz"
API_URL = "https://api.prover.xyz/api/v1/common/task/prover/submitTaskDataRaw"
CHAIN_ID = "cysicmint_4399-1"
DENOM = "CYS"
CORRECT_TYPE_URL = "/cysicmint.crypto.v1.ethsecp256k1.PubKey"
MAX_RETRIES = 5
RETRY_DELAY_SEC = 5
LOG_FILE = "/root/cysic-prover/submit_tx.log"

try:
    import bech32
    from eth_account import Account
    from eth_keys import keys
    from eth_utils import keccak
    from google.protobuf import any_pb2
    from cosmpy.protos.cosmos.base.v1beta1.coin_pb2 import Coin
    from cosmpy.protos.cosmos.crypto.secp256k1.keys_pb2 import PubKey as Secp256k1PubKey
    from cosmpy.protos.cosmos.tx.v1beta1.tx_pb2 import (
        TxBody, AuthInfo, SignerInfo, ModeInfo, Fee, TxRaw, SignDoc
    )
    from cosmpy.protos.cosmos.tx.signing.v1beta1.signing_pb2 import SIGN_MODE_DIRECT
except ImportError:
    print("[-] 缺少依赖: pip3 install cosmpy protobuf bech32-py eth-account eth-utils requests")
    sys.exit(1)


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    formatted = f"[{ts}] {msg}"
    print(formatted)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except:
        pass


# ==================== Web2: 上传 Proof 到 API ====================

def build_proof_data(filepaths_str):
    """读取 proof 文件 → Base64 编码 → 套娃封装"""
    filepaths = [p.strip() for p in filepaths_str.split(',') if p.strip()]
    inner_b64_list = []

    for filepath in filepaths:
        log(f"  [⏳] 等待文件: {filepath}")
        start_wait = time.time()

        while not os.path.exists(filepath):
            if time.time() - start_wait > 900:
                log(f"  ❌ 超时(15min)未找到: {filepath}")
                return None
            time.sleep(1)

        time.sleep(0.5)  # 等落盘

        with open(filepath, "rb") as f:
            raw_bytes = f.read()

        log(f"  [+] 文件就绪: {len(raw_bytes)} bytes")
        inner_b64_list.append(base64.b64encode(raw_bytes).decode('utf-8'))

    # [Base64_1, Base64_2, ...] → 整体再 Base64
    wrapped_str = f"[{','.join(inner_b64_list)}]"
    outer_b64 = base64.b64encode(wrapped_str.encode('utf-8')).decode('utf-8')
    return outer_b64


def generate_signature(data_dict, pk_hex):
    """keccak256(sorted_json) → secp256k1 ECDSA → v+27 → Base64"""
    msg_str = json.dumps(dict(sorted(data_dict.items())), separators=(',', ':'))
    msg_hash = keccak(text=msg_str)

    pk = keys.PrivateKey(bytes.fromhex(pk_hex))
    sig = pk.sign_msg_hash(msg_hash)

    sig_bytes = sig.to_bytes()
    final_sig = sig_bytes[:64] + bytes([sig_bytes[64] + 27])

    return base64.b64encode(final_sig).decode('utf-8')


def upload_to_api(task_hash, eth_address, proof_files_str, pk_hex):
    """POST submitTaskDataRaw → 获取 S3 URL"""
    log(f"[*] 上传 proof 到 API...")

    proofData = build_proof_data(proof_files_str)
    if not proofData:
        return None

    payload_data = {
        "taskHash": task_hash,
        "prover": eth_address,
        "proofData": proofData
    }

    sig = generate_signature(payload_data, pk_hex)

    final_payload = {**payload_data, "sign": sig}

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Go-http-client/2.0",
        "Accept-Encoding": "gzip"
    }

    log(f"  [*] POST → {API_URL}")
    try:
        response = requests.post(API_URL, json=final_payload, headers=headers, timeout=45)
        resp_json = response.json()

        if response.status_code == 200 and resp_json.get('code') == 0:
            real_url = resp_json.get('data', {}).get('url')
            if real_url:
                log(f"  ✅ 上传成功! URL: {real_url}")
                return real_url

        log(f"  ❌ 上传失败: {json.dumps(resp_json, ensure_ascii=False)}")
        return None
    except Exception as e:
        log(f"  ❌ 网络异常: {e}")
        return None


# ==================== Web3: 链上提交 MsgSubmitData ====================

def robust_get(url):
    while True:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code in (200, 400, 404, 500):
                return r
        except:
            time.sleep(2)


def robust_post(url, json_data):
    while True:
        try:
            return requests.post(url, json=json_data, timeout=20)
        except:
            time.sleep(2)


def get_account_info(address):
    url = f"{REST_URL}/cosmos/auth/v1beta1/accounts/{address}"
    try:
        r = robust_get(url)
        data = r.json()
        acc = data.get('account', {})
        if 'base_account' in acc:
            acc = acc['base_account']
        return int(acc.get('account_number', 0)), int(acc.get('sequence', 0))
    except:
        return None, None


def wait_for_tx(tx_hash):
    url = f"{REST_URL}/cosmos/tx/v1beta1/txs/{tx_hash}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            r = robust_get(url)
            if r.status_code == 200:
                j = r.json()
                if "tx_response" in j:
                    return True, j["tx_response"]
        except:
            pass
        time.sleep(2)
    return False, None


def encode_string_field(field_number, value):
    """手动 protobuf 编码 string 字段"""
    def varint(v):
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

    tag = (field_number << 3) | 2
    vb = value.encode('utf-8')
    return varint(tag) + varint(len(vb)) + vb


def broadcast_transaction(task_hash, file_url, cysic_addr, pk_obj, compressed_pub):
    """构造 MsgSubmitData 并广播"""
    log(f"[*] 构建链上交易 MsgSubmitData...")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            acc_num, seq = get_account_info(cysic_addr)
            if acc_num is None:
                time.sleep(RETRY_DELAY_SEC)
                continue

            log(f"  [+] 账户: Num={acc_num}, Seq={seq}")

            # PubKey
            pk_any = any_pb2.Any(
                type_url=CORRECT_TYPE_URL,
                value=Secp256k1PubKey(key=compressed_pub).SerializeToString()
            )

            # AuthInfo
            auth_info = AuthInfo(
                signer_infos=[SignerInfo(
                    public_key=pk_any,
                    mode_info=ModeInfo(single=ModeInfo.Single(mode=SIGN_MODE_DIRECT)),
                    sequence=seq
                )],
                fee=Fee(
                    amount=[Coin(denom=DENOM, amount="7500000000000000")],
                    gas_limit=300000
                )
            )
            auth_bytes = auth_info.SerializeToString()

            # MsgSubmitData: {taskHash, fileUrl, sender}
            msg_value = (
                encode_string_field(1, task_hash) +
                encode_string_field(2, file_url) +
                encode_string_field(3, cysic_addr)
            )

            # TxBody
            tx_body = TxBody(messages=[any_pb2.Any(
                type_url="/cysicmint.zktask.v1.MsgSubmitData",
                value=msg_value
            )])

            # SignDoc
            sign_doc = SignDoc(
                body_bytes=tx_body.SerializeToString(),
                auth_info_bytes=auth_bytes,
                chain_id=CHAIN_ID,
                account_number=acc_num
            )

            # 签名
            sig = pk_obj.sign_msg_hash(keccak(sign_doc.SerializeToString())).to_bytes()

            # TxRaw
            tx_raw = TxRaw(
                body_bytes=sign_doc.body_bytes,
                auth_info_bytes=auth_bytes,
                signatures=[sig]
            )
            tx_b64 = base64.b64encode(tx_raw.SerializeToString()).decode("utf-8")

            # 广播
            log(f"  [*] 广播交易 (attempt {attempt}/{MAX_RETRIES})...")
            r = robust_post(RPC_URL, {
                "jsonrpc": "2.0", "id": 1,
                "method": "broadcast_tx_sync",
                "params": [tx_b64]
            })

            try:
                res = r.json()
            except:
                time.sleep(RETRY_DELAY_SEC)
                continue

            if "result" in res and "hash" in res["result"]:
                tx_h = res["result"]["hash"]
                log(f"  [*] 已广播 tx={tx_h}, 等待确认...")

                ok, txr = wait_for_tx(tx_h)
                if ok:
                    code = int(txr.get("code", 0))
                    if code == 0:
                        log(f"  🎉 链上提交成功! tx={tx_h}")
                        return True
                    elif "no task" in txr.get('raw_log', '').lower() and attempt < MAX_RETRIES:
                        log(f"  ⚠️ 'no task' 错误，{RETRY_DELAY_SEC}s 后重试...")
                        time.sleep(RETRY_DELAY_SEC)
                        continue
                    else:
                        log(f"  ❌ 链上失败: code={code} log={txr.get('raw_log', '')}")
                        return False
                else:
                    log(f"  ❌ 查询超时 tx={tx_h}")
                    return False
            else:
                log(f"  ❌ RPC 拒绝: {json.dumps(res, ensure_ascii=False)}")
                return False

        except Exception as e:
            log(f"  ❌ 异常: {str(e)}")
            time.sleep(RETRY_DELAY_SEC)

    log(f"  ❌ 重试 {MAX_RETRIES} 次后放弃")
    return False


# ==================== 主入口 ====================

def main():
    parser = argparse.ArgumentParser(description="Cysic Prover 即时上报")
    parser.add_argument("--hash", required=True, help="Task hash")
    parser.add_argument("--proof-files", required=True, help="Proof 文件路径(逗号分隔)")
    args = parser.parse_args()

    key_hex = PRIVATE_KEY_HEX.strip().replace('0x', '')
    eth_acc = Account.from_key(key_hex)
    eth_address = eth_acc.address

    five = bech32.convertbits(bytes.fromhex(eth_address.lower().replace('0x', '')), 8, 5)
    cysic_addr = bech32.bech32_encode("cysic", five)

    pk_obj = keys.PrivateKey(bytes.fromhex(key_hex))
    compressed_pub = pk_obj.public_key.to_compressed_bytes()

    log("================================================================")
    log(f"🚀 即时上报启动!")
    log(f"  TaskHash: {args.hash}")
    log(f"  Account: {cysic_addr} | {eth_address}")
    log(f"  Proof: {args.proof_files}")

    # 阶段一：上传到 API，获取 S3 URL
    real_s3_url = upload_to_api(args.hash, eth_address, args.proof_files, key_hex)

    if not real_s3_url:
        log("❌ API 上传失败，退出")
        sys.exit(1)

    # 阶段二：链上提交 MsgSubmitData
    success = broadcast_transaction(
        task_hash=args.hash,
        file_url=real_s3_url,
        cysic_addr=cysic_addr,
        pk_obj=pk_obj,
        compressed_pub=compressed_pub
    )

    if success:
        log("✅ 全流程完成!")
    else:
        log("❌ 链上提交失败")

    log("================================================================\n")


if __name__ == "__main__":
    main()
