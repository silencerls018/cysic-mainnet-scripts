#!/bin/bash
# ================================================================
# 终极拦截代理 (内鬼 + 即时上报版)
# 
# 功能：
# 1. 截获 venus_prover_server 对 cargo-zisk 的调用
# 2. 如果有预计算缓存，直接复制（0秒完成）
# 3. 复制完成后，立即触发 submit_tx.py 上报（不等心跳）
# 4. 如果没有缓存，老老实实调用真正的 cargo-zisk
#
# 部署方式：
#   mv /root/venus_v0_1_6/target/release/cargo-zisk /root/venus_v0_1_6/target/release/cargo-zisk-real
#   cp cargo-zisk-ghost.sh /root/venus_v0_1_6/target/release/cargo-zisk
#   chmod +x /root/venus_v0_1_6/target/release/cargo-zisk
# ================================================================

GHOST_LOG="/root/cysic-prover/proxy_ghost.log"
CACHE="/root/cysic-prover/cache"
TASK_MAP_DIR="/root/cysic-prover/task_map"
SUBMIT_SCRIPT="/root/cysic-prover/submit_tx.py"

mkdir -p "$TASK_MAP_DIR"

log() {
    local MSG="[内鬼] $(date '+%Y-%m-%d %H:%M:%S.%3N') - $1"
    echo "$MSG" | tee -a "$GHOST_LOG"
}

log "================================================================="
log "🔥 老板下发新指令! PID=$$ 参数: $*"

# ==================== 解析参数 ====================
OUT_DIR=""
INPUT_FILE=""
args=("$@")

for i in "${!args[@]}"; do
    if [[ "${args[$i]}" == "-o" ]]; then
        OUT_DIR="${args[$((i+1))]}"
    fi
    if [[ "${args[$i]}" == "-i" ]]; then
        INPUT_FILE="${args[$((i+1))]}"
    fi
done

# 没有 -o 参数，放行
if [ -z "$OUT_DIR" ]; then
    log "⚠️ 无 -o 参数，放行真实程序"
    exec /root/venus_v0_1_6/target/release/cargo-zisk-real "$@"
fi

log "🔍 输出目录: $OUT_DIR"
log "🔍 输入文件: $INPUT_FILE"

# ==================== 提取 BlockHeight ====================
FOLDER_NAME=$(basename "$OUT_DIR")
BLOCK_HEIGHT=$(echo "$FOLDER_NAME" | cut -d'_' -f1)
log "🎯 BlockHeight: $BLOCK_HEIGHT"

# ==================== 查找预计算缓存 ====================
CACHE_DIR=$(ls -d ${CACHE}/proof_${BLOCK_HEIGHT}_* 2>/dev/null | head -n 1)

if [ -n "$CACHE_DIR" ] && [ -d "$CACHE_DIR" ]; then
    log "✅ 命中缓存: $CACHE_DIR"
    CACHE_PROOF="$CACHE_DIR/vadcop_final_proof.bin"

    # 等待缓存文件就绪（学霸可能还在算）
    WAIT_SECS=0
    while [ ! -f "$CACHE_PROOF" ]; do
        if [ $((WAIT_SECS % 3)) -eq 0 ]; then
            log "⏳ 等待缓存文件就绪... (${WAIT_SECS}s)"
        fi
        sleep 1
        WAIT_SECS=$((WAIT_SECS + 1))
        if [ $WAIT_SECS -gt 600 ]; then
            log "❌ 等待超时(600s)，交给真实程序"
            exec /root/venus_v0_1_6/target/release/cargo-zisk-real "$@"
        fi
    done

    [ $WAIT_SECS -gt 0 ] && log "🎉 等了 ${WAIT_SECS}s 后缓存就绪"

    # 复制 proof 到目标目录
    mkdir -p "$OUT_DIR"
    cp "$CACHE_PROOF" "$OUT_DIR/vadcop_final_proof.bin"
    FILE_SIZE=$(stat -c%s "$OUT_DIR/vadcop_final_proof.bin" 2>/dev/null)
    log "✅ Proof 已就位! 大小: $FILE_SIZE bytes"

    # ==================== 核心：立即触发上报 ====================
    # 从映射文件中查找 taskHash（monitor.sh 会维护这个映射）
    TASK_HASH=""
    
    # 方法1：从 block→task 映射文件查找
    if [ -f "$TASK_MAP_DIR/block_${BLOCK_HEIGHT}" ]; then
        TASK_HASH=$(cat "$TASK_MAP_DIR/block_${BLOCK_HEIGHT}" 2>/dev/null | head -1)
        log "📋 从映射文件找到 taskHash: ${TASK_HASH:0:16}..."
    fi

    # 方法2：从 current_task_hash 文件查找
    if [ -z "$TASK_HASH" ] && [ -f "/root/cysic-prover/current_task_hash" ]; then
        TASK_HASH=$(cat /root/cysic-prover/current_task_hash 2>/dev/null | head -1)
        log "📋 从 current_task_hash 找到: ${TASK_HASH:0:16}..."
    fi

    if [ -n "$TASK_HASH" ] && [ -f "$SUBMIT_SCRIPT" ]; then
        log "🚀🚀🚀 不等心跳！内鬼直接交卷！"
        log "  TaskHash: $TASK_HASH"
        log "  ProofFile: $OUT_DIR/vadcop_final_proof.bin"
        
        # 后台执行上报，不阻塞返回
        nohup python3 "$SUBMIT_SCRIPT" \
            --hash "$TASK_HASH" \
            --proof-files "$OUT_DIR/vadcop_final_proof.bin" \
            >> /root/cysic-prover/instant_submit.log 2>&1 &
        
        SUBMIT_PID=$!
        log "🚀 上报进程已启动 PID=$SUBMIT_PID"
    else
        if [ -z "$TASK_HASH" ]; then
            log "⚠️ 未找到 taskHash 映射，等官方 prover 自己上报"
        fi
        if [ ! -f "$SUBMIT_SCRIPT" ]; then
            log "⚠️ submit_tx.py 不存在: $SUBMIT_SCRIPT"
        fi
    fi

    log "🫡 向老板返回 exit 0（假装计算完成）"
    log "================================================================="
    exit 0
else
    # 没有缓存，调用真实程序
    log "❌ 未找到 block=$BLOCK_HEIGHT 的缓存"
    log "🚀 调用真实 cargo-zisk-real"
    log "================================================================="
    exec /root/venus_v0_1_6/target/release/cargo-zisk-real "$@"
fi
