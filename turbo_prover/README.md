# Cysic Venus Turbo Prover System

证明完成后 **0 秒延迟** 上报，完全绕过官方 prover 的 15-30 秒心跳等待。

## 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Turbo Prover System                            │
│                                                                   │
│  ┌─────────────────┐    ┌──────────────────────────────────────┐ │
│  │  monitor.sh      │    │  cargo-zisk-ghost.sh (内鬼代理)      │ │
│  │                  │    │                                      │ │
│  │  • API轮询发现   │    │  • 截获 venus_prover_server 调用     │ │
│  │    新任务        │    │  • 查找预计算缓存                    │ │
│  │  • 预下载 input  │    │  • 有缓存 → cp → 立即触发上报       │ │
│  │  • 预计算 proof  │    │  • 无缓存 → 调真实 cargo-zisk       │ │
│  │  • 写入 taskHash │    │                                      │ │
│  │    ↔ block 映射  │    │  触发 submit_tx.py:                  │ │
│  └─────────────────┘    │  • 读 proof 文件 → Base64            │ │
│                          │  • POST API → 获取 S3 URL            │ │
│  ┌─────────────────┐    │  • MsgSubmitData → 链上交易           │ │
│  │  日志监控        │    └──────────────────────────────────────┘ │
│  │  (prover日志)    │                                             │
│  │  • 实时提取      │    ┌──────────────────────────────────────┐ │
│  │    taskHash      │    │  官方 prover (不改动)                 │ │
│  │  • 写入映射文件  │    │  • 照常运行，接任务、bid、accept      │ │
│  └─────────────────┘    │  • 它的上报会晚到（被我们抢先了）      │ │
│                          └──────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

## 时间对比

| 步骤 | 官方流程 | Turbo 流程 |
|------|---------|-----------|
| 证明生成 | 27 秒 GPU | **0 秒** (缓存命中) |
| 等心跳触发上报 | 15-30 秒 | **0 秒** (内鬼直接触发) |
| API 上传 | 2-3 秒 | 2-3 秒 |
| 链上 submit | 3-5 秒 | 3-5 秒 |
| **总计** | ~70-90 秒 | **~10-15 秒** |

## 文件说明

| 文件 | 作用 |
|------|------|
| `cargo-zisk-ghost.sh` | 内鬼代理，替换 cargo-zisk |
| `monitor.sh` | 主控：启动 venus_prover_server + API轮询预计算 + 日志监控 |
| `submit_tx.py` | 即时上报：上传API + 链上 MsgSubmitData |

## 部署步骤

```bash
# 1. 备份真正的 cargo-zisk
mv /root/venus_v0_1_6/target/release/cargo-zisk \
   /root/venus_v0_1_6/target/release/cargo-zisk-real

# 2. 部署内鬼代理
cp cargo-zisk-ghost.sh /root/venus_v0_1_6/target/release/cargo-zisk
chmod +x /root/venus_v0_1_6/target/release/cargo-zisk

# 3. 部署上报脚本
cp submit_tx.py /root/cysic-prover/submit_tx.py

# 4. 安装 Python 依赖
pip3 install cosmpy protobuf bech32-py eth-account eth-utils requests

# 5. 创建映射目录
mkdir -p /root/cysic-prover/task_map

# 6. 启动 Turbo 系统（会启动 venus_prover_server + 预计算 + 日志监控）
bash monitor.sh

# 7. 在另一个终端启动官方 prover
cd ~/cysic-prover && LD_LIBRARY_PATH=. CHAIN_ID=534352 ./prover 2>&1 | tee start_prover.log
```

## 配置

编辑 `submit_tx.py` 顶部的配置：
```python
PRIVATE_KEY_HEX = "你的私钥"
```

编辑 `monitor.sh` 顶部的配置：
```bash
MY_WALLET="你的 0x 地址"
```

## 工作流程

1. 官方 prover 接到任务 → bid → accept
2. monitor.sh 通过 API 发现任务 → 预下载 input → 预计算 proof → 存缓存
3. 同时 monitor.sh 监控 prover 日志 → 提取 taskHash → 写入映射文件
4. 官方 prover 调用 venus_prover_server → venus_prover_server 调用 cargo-zisk
5. 内鬼代理截获 → 发现缓存 → cp proof → 立即触发 submit_tx.py
6. submit_tx.py: 上传 API → 获取 URL → 链上 MsgSubmitData → 完成！
7. 官方 prover 收到 "proof done" → 等心跳 → 尝试上报 → 发现已完成 → skip

## 日志文件

| 文件 | 内容 |
|------|------|
| `/root/cysic-prover/proxy_ghost.log` | 内鬼代理日志 |
| `/root/cysic-prover/submit_tx.log` | 上报脚本日志 |
| `/root/cysic-prover/instant_submit.log` | 后台上报进程日志 |
| `/root/cysic-prover/start_prover.log` | 官方 prover 日志 |
