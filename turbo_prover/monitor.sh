#!/bin/bash
# ================================================================
# Cysic Venus Turbo Monitor (完整版)
#
# 功能：
# 1. 启动 venus_prover_server
# 2. API 轮询发现新任务 → 预下载 → 预计算
# 3. 监控官方 prover 日志 → 实时捕获 taskHash → 写入映射文件
# 4. Ctrl+C 安全退出
#
# 关键改进：
# - 新增 taskHash ↔ BlockHeight 映射，供内鬼脚本使用
# - 内鬼脚本读取映射后，可以立即调 submit_tx.py 上报
# ================================================================

# ==================== 配置 ====================
MY_WALLET="0xADcA5d33FA9c47feb62f165F0d457B5cade58A6f"
WORKER_LOG="/root/cysic-prover/start_prover.log"
CACHE="/root/cysic-prover/cache"
TASK_MAP_DIR="/root/cysic-prover/task_map"

CARGO_ZISK="/root/venus_v0_1_6/target/release/cargo-zisk-real"
ELF="/root/venus_v0_1_6/guest/zisk-eth-client/bin/guests/stateless-validator-reth/target/riscv64ima-zisk-zkvm-elf/release/zec-reth"
PROVING_KEY="/root/venus_v0_1_6/build/provingKey"
PRECALC_PORT=23115

export ASM_UNLOCK=true
export VENUS_DIR=/root/venus_v0_1_6
export VENUS_OUT_DIR=/root/venus_v0_1_6/tmp
export RUST_LOG=info

# ==================== 初始化 ====================
mkdir -p "$CACHE" "$TASK_MAP_DIR"

log() {
    echo "[turbo] $(date '+%Y/%m/%d %H:%M:%S') $1"
}

# ==================== 安全退出 ====================
cleanup() {
    echo ""
    log "🛑 收到停止命令，正在安全关闭..."
    
    [ -n "$VENUS_SERVER_PID" ] && kill -0 $VENUS_SERVER_PID 2>/dev/null && kill -9 $VENUS_SERVER_PID 2>/dev/null
    [ -n "$API_PID" ] && kill -0 $API_PID 2>/dev/null && kill -9 $API_PID 2>/dev/null
    [ -n "$LOG_MONITOR_PID" ] && kill -0 $LOG_MONITOR_PID 2>/dev/null && kill -9 $LOG_MONITOR_PID 2>/dev/null
    
    log "👋 所有进程已退出"
    exit 0
}
trap cleanup SIGINT SIGTERM

# ==================== 启动 venus_prover_server ====================
log "启动 venus_prover_server (端口 7000)..."
env VENUS_PROVER_GRPC_PORT="7000" \
    VENUS_DIR="$HOME/venus_v0_1_6" \
    VENUS_OUT_DIR="$HOME/venus_v0_1_6/tmp" \
    ASM_UNLOCK="true" \
    RUST_LOG="info" \
    /root/cysic-prover/venus_prover_server &
VENUS_SERVER_PID=$!
log "venus_prover_server PID=$VENUS_SERVER_PID"

# ==================== 工具函数 ====================

is_gpu_busy() {
    pgrep -f "cargo-zisk-real prove" > /dev/null 2>&1
}

wait_gpu_free() {
    local WAIT_COUNT=0
    while is_gpu_busy; do
        if [ $((WAIT_COUNT % 6)) -eq 0 ]; then
            log "GPU 忙碌中，等待... (${WAIT_COUNT}0s)"
        fi
        WAIT_COUNT=$((WAIT_COUNT + 1))
        sleep 10
    done
}

download_input() {
    local BLOCK="$1"
    local URL="$2"
    local GZ_FILE="$CACHE/${BLOCK}.gz"
    local BIN_FILE="$CACHE/${BLOCK}.bin"

    log "  📥 下载 block=$BLOCK"
    local HTTP_CODE
    HTTP_CODE=$(curl -s -o "$GZ_FILE" -w "%{http_code}" --max-time 60 --connect-timeout 5 "$URL" 2>/dev/null)

    if [ "$HTTP_CODE" != "200" ] || [ ! -s "$GZ_FILE" ]; then
        log "  ❌ 下载失败 HTTP=$HTTP_CODE block=$BLOCK"
        rm -f "$GZ_FILE"
        return 1
    fi

    gunzip -k -f "$GZ_FILE" 2>/dev/null
    [ -f "$CACHE/${BLOCK}" ] && mv "$CACHE/${BLOCK}" "$BIN_FILE"

    if [ ! -f "$BIN_FILE" ]; then
        log "  ❌ 解压失败 block=$BLOCK"
        return 1
    fi

    log "  ✅ 下载成功 block=$BLOCK"
    return 0
}

run_prove() {
    local BLOCK="$1"
    local INPUT_FILE="$2"
    local PROOF_DIR="$3"

    mkdir -p "$PROOF_DIR"
    log "  🔥 预计算 block=$BLOCK"
    local START=$(date +%s)

    $CARGO_ZISK prove \
        -k "$PROVING_KEY" \
        -e "$ELF" \
        -i "$INPUT_FILE" \
        -o "$PROOF_DIR" \
        -a -y -u --port "$PRECALC_PORT" \
        2>&1 | while IFS= read -r pline; do
            echo "[venus real] $pline"
        done

    local DURATION=$(( $(date +%s) - START ))

    if [ -f "$PROOF_DIR/vadcop_final_proof.bin" ]; then
        log "  🎉 预计算成功 block=$BLOCK 耗时=${DURATION}s"
        return 0
    else
        log "  ❌ 预计算失败 block=$BLOCK"
        return 1
    fi
}

# ==================== TaskHash 映射写入 ====================
# 当 API 发现任务时，从 task detail 中提取 taskHash 和 BlockHeight 的对应关系
write_task_mapping() {
    local TASK_HASH="$1"
    local BLOCK_HEIGHT="$2"
    
    if [ -n "$TASK_HASH" ] && [ -n "$BLOCK_HEIGHT" ]; then
        echo "$TASK_HASH" > "$TASK_MAP_DIR/block_${BLOCK_HEIGHT}"
        log "  📋 映射写入: block_${BLOCK_HEIGHT} → ${TASK_HASH:0:16}..."
    fi
}

# ==================== API 轮询监听器 ====================
api_monitor_loop() {
    log "📡 API 监听启动 (3秒轮询) | 钱包: $MY_WALLET"
    
    while true; do
        # 获取活跃任务列表
        TASK_RESPONSE=$(curl -s --max-time 10 "https://api.cysic.xyz/api/v1/zkTask/dashboard/task/list?pageNo=1&pageSize=10" 2>/dev/null)
        
        TASK_IDS=$(echo "$TASK_RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for t in data.get('data', {}).get('list', []):
        if t.get('status') != 100: 
            print(t.get('id'))
except: pass
" 2>/dev/null)

        for TID in $TASK_IDS; do
            DETAIL_JSON=$(curl -s --max-time 10 "https://api.cysic.xyz/api/v1/zkTask/dashboard/task/detail/${TID}" 2>/dev/null)
            
            # 提取 taskHash 和 block 信息
            echo "$DETAIL_JSON" | python3 -c "
import sys, json
wallet = '$MY_WALLET'.lower()
task_map_dir = '$TASK_MAP_DIR'
try:
    d = json.load(sys.stdin).get('data', {})
    task_hash = d.get('taskHash', '')
    proofs = d.get('proofList', [])
    
    is_mine = any(p.get('name', '').lower() == wallet for p in proofs)
    if is_mine:
        items = json.loads(d.get('inputData', '[]'))
        for idx, item in enumerate(items):
            bh = item.get('BlockHeight')
            url = item.get('S3Url')
            if bh and url:
                # 输出映射信息
                print(f'MAP {task_hash} {bh}')
                # 输出下载信息
                print(f'DL {idx} {bh} {url}')
except Exception as e:
    pass
" 2>/dev/null | while read ACTION REST; do
                case "$ACTION" in
                    MAP)
                        # 写入 taskHash ↔ BlockHeight 映射
                        MAP_HASH=$(echo "$REST" | awk '{print $1}')
                        MAP_BLOCK=$(echo "$REST" | awk '{print $2}')
                        write_task_mapping "$MAP_HASH" "$MAP_BLOCK"
                        ;;
                    DL)
                        IDX=$(echo "$REST" | awk '{print $1}')
                        BH=$(echo "$REST" | awk '{print $2}')
                        URL=$(echo "$REST" | awk '{print $3}')
                        
                        [ -z "$BH" ] && continue
                        
                        MARKER_FILE="$CACHE/processed_${TID}_${BH}"
                        if [ ! -f "$MARKER_FILE" ]; then
                            touch "$MARKER_FILE"
                            log "================================================================"
                            log "🎯 发现任务! TID=$TID block=$BH"
                            log "================================================================"
                            
                            (
                                if download_input "$BH" "$URL"; then
                                    INPUT_FILE="$CACHE/${BH}.bin"
                                    PROOF_DIR="$CACHE/proof_${BH}_${IDX}"
                                    
                                    wait_gpu_free
                                    run_prove "$BH" "$INPUT_FILE" "$PROOF_DIR"
                                    
                                    rm -f "$CACHE/${BH}.gz" "$CACHE/${BH}.bin"
                                fi
                            ) &
                        fi
                        ;;
                esac
            done
        done
        
        sleep 3
    done
}

# ==================== 官方 Prover 日志监控 (实时提取 taskHash) ====================
prover_log_monitor() {
    log "📺 开始监控官方 prover 日志，实时捕获 taskHash..."
    
    tail -F "$WORKER_LOG" 2>/dev/null | while IFS= read -r line; do
        
        # 捕获 "create task success, task hash: xxx"
        if echo "$line" | grep -q "create task success"; then
            HASH=$(echo "$line" | grep -oP 'task hash: \K[a-f0-9]+')
            if [ -n "$HASH" ]; then
                echo "$HASH" > /root/cysic-prover/current_task_hash
                log "[捕获] create task: ${HASH:0:16}..."
            fi
        fi
        
        # 捕获 "new task need bid, task hash: xxx"
        if echo "$line" | grep -q "new task need bid"; then
            HASH=$(echo "$line" | grep -oP 'task hash: \K[a-f0-9]+')
            if [ -n "$HASH" ]; then
                echo "$HASH" > /root/cysic-prover/current_task_hash
                log "[捕获] new task bid: ${HASH:0:16}..."
            fi
        fi

        # 捕获 "start proof task: xxx" — 这是最准确的时刻
        if echo "$line" | grep -q "start proof task:"; then
            HASH=$(echo "$line" | grep -oP 'start proof task: \K[a-f0-9]+')
            if [ -n "$HASH" ]; then
                echo "$HASH" > /root/cysic-prover/current_task_hash
                log "[捕获] start proof: ${HASH:0:16}..."
            fi
        fi
        
        # 捕获 "start prepare zk task venus, task: xxx" + 从中提取 block height
        if echo "$line" | grep -q "start prepare zk task venus"; then
            HASH=$(echo "$line" | grep -oP 'task: \K[a-f0-9]+')
            if [ -n "$HASH" ]; then
                echo "$HASH" > /root/cysic-prover/current_task_hash
            fi
        fi

        # 捕获 accept task 的 tx
        if echo "$line" | grep -q "accept task:"; then
            HASH=$(echo "$line" | grep -oP 'accept task: \K[a-f0-9]+')
            TX=$(echo "$line" | grep -oP 'with tx: \K[A-F0-9]+')
            [ -n "$HASH" ] && log "[捕获] accept: ${HASH:0:16}... tx=${TX:0:16}..."
        fi

        # 捕获提交成功
        if echo "$line" | grep -q "submit task data raw success"; then
            log "[捕获] 官方 prover 提交成功"
        fi
        
        # 捕获 proof done
        if echo "$line" | grep -q "proof done"; then
            log "[捕获] proof done (如果内鬼已触发上报，则此次为重复)"
        fi

        # 关键：捕获 "start process task" 并写入 BlockHeight 映射
        # 从服务端推送的 base64 数据中提取 BlockHeight
        if echo "$line" | grep -q "start prepare task:"; then
            HASH=$(echo "$line" | grep -oP 'start prepare task: \K[a-f0-9]+')
            if [ -n "$HASH" ]; then
                echo "$HASH" > /root/cysic-prover/current_task_hash
            fi
        fi

    done
}

# ==================== 启动所有组件 ====================

# 启动 API 轮询
api_monitor_loop &
API_PID=$!

# 启动日志监控（提取 taskHash）
prover_log_monitor &
LOG_MONITOR_PID=$!

# ==================== 主进程 ====================
log "========================================"
log "  Cysic Venus Turbo System 启动完成"
log "  - venus_prover_server PID=$VENUS_SERVER_PID"
log "  - API 监听 PID=$API_PID"
log "  - 日志监控 PID=$LOG_MONITOR_PID"
log "  按 Ctrl+C 安全退出"
log "========================================"

# 保持主进程活着
wait
