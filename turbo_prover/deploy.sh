#!/bin/bash
# ================================================================
# 一键部署脚本
#
# 使用方式：
#   bash deploy.sh
#
# 做的事情：
#   1. 部署 submit_tx.py 到 ~/cysic-prover/
#   2. 升级内鬼脚本（加即时上报功能）
#   3. 创建必要目录
#   4. 检查依赖
# ================================================================

set -e

echo "=========================================="
echo "  Cysic Turbo Prover 部署"
echo "=========================================="
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. 部署 submit_tx.py
echo "[1/4] 部署 submit_tx.py..."
cp "$SCRIPT_DIR/submit_tx.py" /root/cysic-prover/submit_tx.py
echo "  ✅ /root/cysic-prover/submit_tx.py"

# 2. 创建目录
echo "[2/4] 创建目录..."
mkdir -p /root/cysic-prover/task_map
mkdir -p /root/cysic-prover/cache
echo "  ✅ /root/cysic-prover/task_map"
echo "  ✅ /root/cysic-prover/cache"

# 3. 检查 Python 依赖
echo "[3/4] 检查 Python 依赖..."
MISSING=""
python3 -c "import bech32" 2>/dev/null || MISSING="$MISSING bech32-py"
python3 -c "from eth_account import Account" 2>/dev/null || MISSING="$MISSING eth-account"
python3 -c "from eth_keys import keys" 2>/dev/null || MISSING="$MISSING eth-keys"
python3 -c "from eth_utils import keccak" 2>/dev/null || MISSING="$MISSING eth-utils"
python3 -c "from google.protobuf import any_pb2" 2>/dev/null || MISSING="$MISSING protobuf"
python3 -c "from cosmpy.protos.cosmos.tx.v1beta1.tx_pb2 import TxBody" 2>/dev/null || MISSING="$MISSING cosmpy"
python3 -c "import requests" 2>/dev/null || MISSING="$MISSING requests"

if [ -n "$MISSING" ]; then
    echo "  ⚠️ 缺少依赖:$MISSING"
    echo "  正在安装..."
    pip3 install $MISSING
    echo "  ✅ 依赖已安装"
else
    echo "  ✅ 所有依赖已就绪"
fi

# 4. 检查内鬼脚本
echo "[4/4] 检查内鬼脚本..."
GHOST="/root/venus_v0_1_6/target/release/cargo-zisk"
REAL="/root/venus_v0_1_6/target/release/cargo-zisk-real"

if [ -f "$REAL" ]; then
    echo "  ✅ cargo-zisk-real 已存在（内鬼已部署）"
else
    echo "  ⚠️ cargo-zisk-real 不存在"
    echo "  如需部署内鬼，执行："
    echo "    mv $GHOST ${GHOST}-real"
    echo "    cp $SCRIPT_DIR/cargo-zisk-ghost.sh $GHOST"
    echo "    chmod +x $GHOST"
fi

echo ""
echo "=========================================="
echo "  部署完成！"
echo ""
echo "  使用步骤："
echo "  1. 修改 /root/cysic-prover/submit_tx.py 中的 PRIVATE_KEY_HEX"
echo "  2. 终端A: bash $SCRIPT_DIR/capture_task_hash.sh"
echo "  3. 终端B: 启动官方 prover"
echo "  4. 等任务来后: bash $SCRIPT_DIR/test_submit.sh"
echo "=========================================="
