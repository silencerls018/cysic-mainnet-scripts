#!/bin/bash
# ================================================================
# 手动测试即时上报脚本
#
# 使用方式：
#   bash test_submit.sh
#
# 前提：
#   1. capture_task_hash.sh 正在运行（已捕获到 taskHash）
#   2. 至少有一个 proof 文件存在
#   3. submit_tx.py 已部署到 /root/cysic-prover/
# ================================================================

echo "=========================================="
echo "  即时上报手动测试"
echo "=========================================="

# 检查 taskHash
if [ ! -f /root/cysic-prover/current_task_hash ]; then
    echo "❌ /root/cysic-prover/current_task_hash 不存在"
    echo "   请先运行 capture_task_hash.sh 并等待任务到来"
    exit 1
fi

TASK_HASH=$(cat /root/cysic-prover/current_task_hash)
echo "TaskHash: $TASK_HASH"

# 找最新的 proof 文件
PROOF_FILE=$(ls -t /root/venus_v0_1_6/tmp/prover_7000/*/vadcop_final_proof.bin 2>/dev/null | head -1)

if [ -z "$PROOF_FILE" ]; then
    echo "❌ 未找到 proof 文件"
    echo "   目录: /root/venus_v0_1_6/tmp/prover_7000/"
    exit 1
fi

echo "Proof文件: $PROOF_FILE"
echo "文件大小: $(stat -c%s "$PROOF_FILE" 2>/dev/null) bytes"
echo ""

# 确认
read -p "确认执行即时上报？(y/N) " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "已取消"
    exit 0
fi

echo ""
echo "🚀 开始上报..."
echo ""

python3 /root/cysic-prover/submit_tx.py \
    --hash "$TASK_HASH" \
    --proof-files "$PROOF_FILE"
