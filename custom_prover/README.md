# Cysic Custom Prover

Complete replacement for the official `prover` binary. Event-driven, submits proofs immediately after generation.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Custom Prover (Python)                      │
│                                                               │
│  WebSocket ──→ Bid ──→ Accept(TX) ──→ Proof ──→ Submit       │
│  (recv task)  (action=6) (MsgAccept)   (cache/GPU)           │
│                                                               │
│  Submit = MsgSubmitHash(TX) → nextStep(HTTP) →               │
│           submitTaskDataRaw(HTTP) → MsgSubmitData(TX)         │
└──────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
    api.prover.xyz              venus_prover_server
    (WebSocket + HTTP)          (GPU proof gen, port 7000)
```

## vs Official Prover

| Feature | Official | Custom |
|---------|----------|--------|
| Proof → Submit delay | 15-60s (heartbeat) | **0s** (immediate) |
| Stage transitions | Heartbeat-driven | Event-driven |
| Total task time | ~90s | **~20-30s** |

## Setup

```bash
# 1. Install dependencies
pip3 install -r requirements.txt

# 2. Configure
cp config.yaml config_local.yaml
# Edit config_local.yaml with your private key

# 3. Make sure venus_prover_server is running on port 7000

# 4. Run
python3 prover.py config_local.yaml
```

## Configuration

Edit `config.yaml`:
- `private_key`: Your worker private key (hex, no 0x)
- `worker_address`: Your 0x address
- `bid`: Bid price in CYS
- `server.ws_endpoint`: WebSocket URL (try wss://api.prover.xyz/ws)

## Files

| File | Purpose |
|------|---------|
| `prover.py` | Main orchestrator - task lifecycle |
| `signer.py` | All signing (API + chain TX) |
| `chain_client.py` | Cosmos TX (MsgAccept, MsgSubmitHash, MsgSubmitData) |
| `api_client.py` | HTTP API (nextStep, submitTaskDataRaw) |
| `ws_client.py` | WebSocket (heartbeat, bid, tasks) |
| `config.yaml` | Configuration |

## Protocol (Confirmed from packet capture)

### WebSocket Actions (Client → Server)
| Action | Purpose | Data |
|--------|---------|------|
| 2 | Heartbeat | {clientType, clientVersion, workerAddress, timestamp, nonce, sign} |
| 4 | Start work | Same as heartbeat |
| 5 | Finish work | Same as heartbeat |
| 6 | Bid | {taskHash, prover, bid, sign} |

### WebSocket Responses (Server → Client)
| respType | Purpose | Data format |
|----------|---------|-------------|
| 0 | Heartbeat ACK | null |
| 1 | Config | base64 JSON {heartbeatDuration} |
| 3 | Task update | base64 JSON [{task_hash, task_proof_data_index, ...}] |
| 5 | New task | gzip+base64 {task_hash, task_type, task_difficulty, ...} |

### Chain Transactions
| Message | type_url | Fields |
|---------|----------|--------|
| Accept | /cysicmint.zktask.v1.MsgAccept | task_hash(1), sender(2) |
| Submit Hash | /cysicmint.zktask.v1.MsgSubmitHash | task_hash(1), proof_hash(2 bytes), sender(3) |
| Submit Data | /cysicmint.zktask.v1.MsgSubmitData | task_hash(1), proof_data_url(2), sender(3) |

### HTTP API
| Endpoint | Purpose | Body |
|----------|---------|------|
| POST /api/v1/common/task/prover/nextStep | Poll state | {prover, taskHash, sign} |
| POST /api/v1/common/task/prover/submitTaskDataRaw | Upload proof | {taskHash, prover, proofData, sign} |

### Signing
- API/WebSocket: `keccak256(sorted_json_compact)` → secp256k1 → v+27 → base64
- Chain TX: `keccak256(SignDoc_bytes)` → secp256k1 → raw 65 bytes

## Known Issues

1. **WebSocket URL path**: May need to be `/ws`, `/websocket`, or just `/`. Try different paths.
2. **nextStep requires session**: The API may require active WebSocket connection for nextStep to work.
3. **Proof file ordering**: Files must be ordered matching the BlockHeight list in task_proof_data_index.
