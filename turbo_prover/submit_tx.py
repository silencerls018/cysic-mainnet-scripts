#!/usr/bin/env python3
# ================================================================
# Cysic Prover 即时上报脚本 (完整版)
#
# 完整流程（不依赖官方 prover）：
#   1. MsgSubmitHash — 链上提交 proof hash (SHA256)
#   2. nextStep 轮询 — 等待服务端确认 (nextStep=3)
#   3. submitTaskDataRaw — HTTP 上传 proof 文件
#   4. MsgSubmitData — 链上提交 proof URL
#
# 使用方式：
#   python3 submit_tx.py --hash <taskHash> --proof-files <path1>,<path2>
#
# 由内鬼脚本 (cargo-zisk-ghost.sh) 在 proof 就绪后立即调用
# ================================================================

import argparse
import json
import base64
import hashlib
import sys
import os
import datetime
import time
import gzip
import io
import requests

# ==================== 配置 ====================
PRIVATE_KEY_HEX = "8825d620e031a80f37cdd57d087da4062e5f41f9d29d5dbd594eeff073fb1f93"

RPC_URL = "https://rpc.cysic.xyz"
REST_URL = "https://rest.cysic.xyz"
API_URL = "https://api.prover.xyz"
CHAIN_ID = "cysicmint_4399-1"
DENOM = "CYS"
CORRECT_TYPE_URL = "/cysicmint.crypto.v1.ethsecp256k1.PubKey"
MAX_RETRIES = 5
RETRY_DELAY_SEC = 5
NEXT_STEP_POLL_INTERVAL = 2
NEXT_STEP_TIMEOUT = 120
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


# ==================== 工具函数 ====================

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


def encode_bytes_field(field_number, value_bytes):
    """手动 protobuf 编码 bytes 字段"""
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
    return varint(tag) + varint(len(value_bytes)) + value_bytes


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


def generate_api_signature(data_dict, pk_hex):
    """keccak256(sorted_json) → secp256k1 ECDSA → v+27 → Base64"""
    msg_str = json.dumps(dict(sorted(data_dict.items())), separators=(',', ':'))
    msg_hash = keccak(text=msg_str)
    pk = keys.PrivateKey(bytes.fromhex(pk_hex))
    sig = pk.sign_msg_hash(msg_hash)
    sig_bytes = sig.to_bytes()
    final_sig = sig_bytes[:64] + bytes([sig_bytes[64] + 27])
    return base64.b64encode(final_sig).decode('utf-8')


# ==================== 第一步: MsgSubmitHash 链上交易 ====================

def compute_proof_hash(filepaths):
    """计算所有 proof 文件拼接后的 SHA256，返回 base64 编码"""
    h = hashlib.sha256()
    for filepath in filepaths:
        with open(filepath, 'rb') as f:
            h.update(f.read())
    return base64.b64encode(h.digest()).decode('utf-8')


def broadcast_submit_hash(task_hash, proof_hash_b64, cysic_addr, pk_obj, compressed_pub):
    """
    第一步：链上提交 MsgSubmitHash
    type_url: /cysicmint.zktask.v1.MsgSubmitHash
    fields: task_hash(1), proof_hash(2), sender(3)
    """
    log(f"[Step 1] MsgSubmitHash: proofHash={proof_hash_b64[:30]}...")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            acc_num, seq = get_account_info(cysic_addr)
            if acc_num is None:
                time.sleep(RETRY_DELAY_SEC)
                continue

            log(f"  [+] 账户: Num={acc_num}, Seq={seq}")

            pk_any = any_pb2.Any(
                type_url=CORRECT_TYPE_URL,
                value=Secp256k1PubKey(key=compressed_pub).SerializeToString()
            )

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

            # MsgSubmitHash: {taskHash, proofHash, sender}
            # proof_hash 是 bytes 类型（base64 解码后的 32 字节）
            proof_hash_bytes = base64.b64decode(proof_hash_b64)
            msg_value = (
                encode_string_field(1, task_hash) +
                encode_bytes_field(2, proof_hash_bytes) +
                encode_string_field(3, cysic_addr)
            )

            tx_body = TxBody(messages=[any_pb2.Any(
                type_url="/cysicmint.zktask.v1.MsgSubmitHash",
                value=msg_value
            )])

            sign_doc = SignDoc(
                body_bytes=tx_body.SerializeToString(),
                auth_info_bytes=auth_bytes,
                chain_id=CHAIN_ID,
                account_number=acc_num
            )

            sig = pk_obj.sign_msg_hash(keccak(sign_doc.SerializeToString())).to_bytes()

            tx_raw = TxRaw(
                body_bytes=sign_doc.body_bytes,
                auth_info_bytes=auth_bytes,
                signatures=[sig]
            )
            tx_b64 = base64.b64encode(tx_raw.SerializeToString()).decode("utf-8")

            log(f"  [*] 广播 MsgSubmitHash (attempt {attempt})...")
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
                        log(f"  ✅ [Step 1] MsgSubmitHash 成功! tx={tx_h}")
                        return True
                    else:
                        raw_log = txr.get('raw_log', '')
                        log(f"  ❌ 链上失败: code={code} log={raw_log}")
                        if "no task" in raw_log.lower() and attempt < MAX_RETRIES:
                            time.sleep(RETRY_DELAY_SEC)
                            continue
                        return False
                else:
                    log(f"  ❌ 查询超时 tx={tx_h}")
                    return False
            else:
                log(f"  ❌ RPC 拒绝: {json.dumps(res, ensure_ascii=False)[:200]}")
                time.sleep(RETRY_DELAY_SEC)
                continue

        except Exception as e:
            log(f"  ❌ 异常: {str(e)}")
            time.sleep(RETRY_DELAY_SEC)

    return False


# ==================== 第二步: nextStep 轮询 ====================

def poll_next_step(task_hash, eth_address, pk_hex):
    """
    轮询 nextStep API 直到返回 nextStep >= 3
    签名只包含 {prover, taskHash}
    """
    log(f"[Step 2] 轮询 nextStep，等待 nextStep=3...")

    data = {"prover": eth_address, "taskHash": task_hash}
    sign = generate_api_signature(data, pk_hex)
    payload = {**data, "sign": sign}

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Go-http-client/2.0",
        "Accept-Encoding": "gzip"
    }

    url = f"{API_URL}/api/v1/common/task/prover/nextStep"
    deadline = time.time() + NEXT_STEP_TIMEOUT

    while time.time() < deadline:
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=20)
            resp = r.json()

            if resp.get("code") == 0:
                next_step = resp.get("nextStep", 0)
                if next_step >= 3:
                    log(f"  ✅ [Step 2] nextStep={next_step}，可以上传了!")
                    return True
                else:
                    log(f"  [*] nextStep={next_step}，继续等待...")
            else:
                log(f"  ⚠️ API 返回: {json.dumps(resp, ensure_ascii=False)[:100]}")

        except Exception as e:
            log(f"  ⚠️ 请求异常: {e}")

        time.sleep(NEXT_STEP_POLL_INTERVAL)

    log(f"  ❌ [Step 2] 轮询超时 ({NEXT_STEP_TIMEOUT}s)")
    return False


# ==================== 第三步: submitTaskDataRaw HTTP 上传 ====================

def build_proof_data(filepaths):
    """
    真实编码流程（从抓包确认）：
    文件原始字节 → Gzip(level=6, mtime=0) → Base64 → 包裹成"[x,y]" → 整体再 Base64
    """
    inner_b64_list = []

    for filepath in filepaths:
        log(f"  [*] 读取: {filepath}")

        with open(filepath, "rb") as f:
            raw_bytes = f.read()

        log(f"      原始大小: {len(raw_bytes)} bytes")

        # Gzip 压缩 (level=6, mtime=0 让 header 全零)
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=6, mtime=0) as gz:
            gz.write(raw_bytes)
        compressed = buf.getvalue()

        log(f"      Gzip 后: {len(compressed)} bytes")
        inner_b64_list.append(base64.b64encode(compressed).decode('utf-8'))

    # 包裹成 JSON 数组字符串: "[Base64_gzip_1,Base64_gzip_2]"
    wrapped_str = f"[{','.join(inner_b64_list)}]"

    # 整体再做一次 Base64
    outer_b64 = base64.b64encode(wrapped_str.encode('utf-8')).decode('utf-8')
    log(f"  [+] proofData 最终长度: {len(outer_b64)} chars")
    return outer_b64


def upload_to_api(task_hash, eth_address, proof_files, pk_hex):
    """POST submitTaskDataRaw → 获取 S3 URL"""
    log(f"[Step 3] 上传 proof 到 API...")

    proofData = build_proof_data(proof_files)

    payload_data = {
        "taskHash": task_hash,
        "prover": eth_address,
        "proofData": proofData
    }

    sig = generate_api_signature(payload_data, pk_hex)
    final_payload = {**payload_data, "sign": sig}

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Go-http-client/2.0",
        "Accept-Encoding": "gzip"
    }

    url = f"{API_URL}/api/v1/common/task/prover/submitTaskDataRaw"
    log(f"  [*] POST → {url}")

    try:
        response = requests.post(url, json=final_payload, headers=headers, timeout=60)
        resp_json = response.json()

        if response.status_code == 200 and resp_json.get('code') == 0:
            real_url = resp_json.get('data', {}).get('url')
            if real_url:
                log(f"  ✅ [Step 3] 上传成功! URL: {real_url}")
                return real_url

        log(f"  ❌ 上传失败: {json.dumps(resp_json, ensure_ascii=False)[:200]}")
        return None
    except Exception as e:
        log(f"  ❌ 网络异常: {e}")
        return None


# ==================== 第四步: MsgSubmitData 链上交易 ====================

def broadcast_submit_data(task_hash, file_url, cysic_addr, pk_obj, compressed_pub):
    """
    第四步：链上提交 MsgSubmitData
    type_url: /cysicmint.zktask.v1.MsgSubmitData
    fields: task_hash(1), proof_data_url(2), sender(3)
    """
    log(f"[Step 4] MsgSubmitData: url={file_url[:80]}...")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            acc_num, seq = get_account_info(cysic_addr)
            if acc_num is None:
                time.sleep(RETRY_DELAY_SEC)
                continue

            log(f"  [+] 账户: Num={acc_num}, Seq={seq}")

            pk_any = any_pb2.Any(
                type_url=CORRECT_TYPE_URL,
                value=Secp256k1PubKey(key=compressed_pub).SerializeToString()
            )

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

            # MsgSubmitData: {taskHash, proofDataUrl, sender}
            msg_value = (
                encode_string_field(1, task_hash) +
                encode_string_field(2, file_url) +
                encode_string_field(3, cysic_addr)
            )

            tx_body = TxBody(messages=[any_pb2.Any(
                type_url="/cysicmint.zktask.v1.MsgSubmitData",
                value=msg_value
            )])

            sign_doc = SignDoc(
                body_bytes=tx_body.SerializeToString(),
                auth_info_bytes=auth_bytes,
                chain_id=CHAIN_ID,
                account_number=acc_num
            )

            sig = pk_obj.sign_msg_hash(keccak(sign_doc.SerializeToString())).to_bytes()

            tx_raw = TxRaw(
                body_bytes=sign_doc.body_bytes,
                auth_info_bytes=auth_bytes,
                signatures=[sig]
            )
            tx_b64 = base64.b64encode(tx_raw.SerializeToString()).decode("utf-8")

            log(f"  [*] 广播 MsgSubmitData (attempt {attempt})...")
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
                        log(f"  🎉 [Step 4] MsgSubmitData 成功! tx={tx_h}")
                        return True
                    else:
                        raw_log = txr.get('raw_log', '')
                        log(f"  ❌ 链上失败: code={code} log={raw_log}")
                        if "no task" in raw_log.lower() and attempt < MAX_RETRIES:
                            time.sleep(RETRY_DELAY_SEC)
                            continue
                        return False
                else:
                    log(f"  ❌ 查询超时 tx={tx_h}")
                    return False
            else:
                log(f"  ❌ RPC 拒绝: {json.dumps(res, ensure_ascii=False)[:200]}")
                time.sleep(RETRY_DELAY_SEC)
                continue

        except Exception as e:
            log(f"  ❌ 异常: {str(e)}")
            time.sleep(RETRY_DELAY_SEC)

    return False


# ==================== 主入口 ====================

def main():
    parser = argparse.ArgumentParser(description="Cysic Prover 即时上报 (完整版)")
    parser.add_argument("--hash", required=True, help="Task hash")
    parser.add_argument("--proof-files", required=True, help="Proof 文件路径(逗号分隔，顺序与 BlockHeight 列表一致)")
    args = parser.parse_args()

    key_hex = PRIVATE_KEY_HEX.strip().replace('0x', '')
    eth_acc = Account.from_key(key_hex)
    eth_address = eth_acc.address

    five = bech32.convertbits(bytes.fromhex(eth_address.lower().replace('0x', '')), 8, 5)
    cysic_addr = bech32.bech32_encode("cysic", five)

    pk_obj = keys.PrivateKey(bytes.fromhex(key_hex))
    compressed_pub = pk_obj.public_key.to_compressed_bytes()

    # 解析 proof 文件列表
    proof_files = [p.strip() for p in args.proof_files.split(',') if p.strip()]

    # 等待所有文件就绪
    for pf in proof_files:
        start_wait = time.time()
        while not os.path.exists(pf):
            if time.time() - start_wait > 900:
                log(f"❌ 超时等待文件: {pf}")
                sys.exit(1)
            time.sleep(1)

    log("================================================================")
    log(f"🚀 即时上报启动! (完整4步流程)")
    log(f"  TaskHash: {args.hash}")
    log(f"  Account: {cysic_addr} | {eth_address}")
    log(f"  Proof files ({len(proof_files)}): {[os.path.basename(f) for f in proof_files]}")
    log("")

    start_time = time.time()

    # === Step 1: MsgSubmitHash ===
    proof_hash = compute_proof_hash(proof_files)
    log(f"  ProofHash (SHA256): {proof_hash}")

    if not broadcast_submit_hash(args.hash, proof_hash, cysic_addr, pk_obj, compressed_pub):
        log("❌ Step 1 失败，退出")
        sys.exit(1)

    # === Step 2: 轮询 nextStep ===
    if not poll_next_step(args.hash, eth_address, key_hex):
        log("❌ Step 2 超时，退出")
        sys.exit(1)

    # === Step 3: 上传 submitTaskDataRaw ===
    real_url = upload_to_api(args.hash, eth_address, proof_files, key_hex)
    if not real_url:
        log("❌ Step 3 失败，退出")
        sys.exit(1)

    # === Step 4: MsgSubmitData ===
    if not broadcast_submit_data(args.hash, real_url, cysic_addr, pk_obj, compressed_pub):
        log("❌ Step 4 失败，退出")
        sys.exit(1)

    total_time = time.time() - start_time
    log("")
    log(f"✅✅✅ 全流程完成! 总耗时: {total_time:.1f}s")
    log("================================================================\n")


if __name__ == "__main__":
    main()
