#!/bin/bash
# ================================================================
# TaskHash 实时捕获脚本
#
# 功能：监控官方 prover 日志，实时提取 taskHash 写入文件
# 供内鬼脚本 (cargo-zisk-ghost.sh) 读取使用
#
# 使用方式：
#   bash capture_task_hash.sh
#
# 输出文件：
#   /root/cysic-prover/current_task_hash  — 当前最新的 taskHash
#   /root/cysic-prover/task_map/block_*   — block→taskHash 映射
# ================================================================

WORKER_LOG="/root/cysic-prover/start_prover.log"
TASK_MAP_DIR="/root/cysic-prover/task_map"

mkdir -p "$TASK_MAP_DIR"

echo "[$(date '+%Y/%m/%d %H:%M:%S')] TaskHash 捕获脚本启动"
echo "  监控日志: $WORKER_LOG"
echo "  映射目录: $TASK_MAP_DIR"
echo "  按 Ctrl+C 退出"
echo ""

tail -F "$WORKER_LOG" 2>/dev/null | while IFS= read -r line; do

    # 捕获 "create task success, task hash: xxx"
    if echo "$line" | grep -q "create task success"; then
        HASH=$(echo "$line" | grep -oP 'task hash: \K[a-f0-9]+')
        if [ -n "$HASH" ]; then
            echo "$HASH" > /root/cysic-prover/current_task_hash
            echo "[$(date '+%H:%M:%S')] 捕获 create task: ${HASH:0:16}..."
        fi
    fi

    # 捕获 "new task need bid, task hash: xxx"
    if echo "$line" | grep -q "new task need bid"; then
        HASH=$(echo "$line" | grep -oP 'task hash: \K[a-f0-9]+')
        if [ -n "$HASH" ]; then
            echo "$HASH" > /root/cysic-prover/current_task_hash
            echo "[$(date '+%H:%M:%S')] 捕获 new task bid: ${HASH:0:16}..."
        fi
    fi

    # 捕获 "start proof task: xxx" — 最准确的时刻
    if echo "$line" | grep -q "start proof task:"; then
        HASH=$(echo "$line" | grep -oP 'start proof task: \K[a-f0-9]+')
        if [ -n "$HASH" ]; then
            echo "$HASH" > /root/cysic-prover/current_task_hash
            echo "[$(date '+%H:%M:%S')] 捕获 start proof: ${HASH:0:16}..."
        fi
    fi

    # 捕获 "start prepare zk task venus, task: xxx"
    if echo "$line" | grep -q "start prepare zk task venus"; then
        HASH=$(echo "$line" | grep -oP 'task: \K[a-f0-9]+')
        if [ -n "$HASH" ]; then
            echo "$HASH" > /root/cysic-prover/current_task_hash
        fi
    fi

    # 捕获 proof done
    if echo "$line" | grep -q "proof done"; then
        echo "[$(date '+%H:%M:%S')] proof done!"
    fi

    # 捕获 submit 成功
    if echo "$line" | grep -q "submit task data raw success"; then
        echo "[$(date '+%H:%M:%S')] 官方 prover 上报成功"
    fi

done
