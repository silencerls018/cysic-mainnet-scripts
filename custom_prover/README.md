# Custom Cysic Prover Client

Event-driven prover client that submits proofs **immediately** after generation, without waiting for heartbeat cycles.

## Key Advantages

| Feature | Official Prover | Custom Prover |
|---------|----------------|---------------|
| Proof → Submit delay | 15-30s (heartbeat) | **0s** (immediate) |
| Stage transitions | Wait for heartbeat | Event-driven |
| "proofData empty" bug | Yes (race condition) | No |
| Total task time | ~90s | **~35s** (proof time only) |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│              Custom Prover Client (Python)                │
│                                                          │
│  WebSocket ──→ Task Engine ──→ Instant Reporter          │
│  (recv task)   (bid/accept/   (submitHash +              │
│                 call GPU)      submitDataRaw)             │
│                     │                                    │
│                     ▼                                    │
│           venus_prover_server (gRPC :7000)               │
└─────────────────────────────────────────────────────────┘
```

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Copy your config
cp config.yaml.example config.yaml
# Edit config.yaml with your private key and worker address
```

## Configuration

```yaml
private_key: "YOUR_HEX_PRIVATE_KEY"
worker_address: "0xYOUR_WORKER_ADDRESS"
bid: "0.01"

server:
  ws_endpoint: "wss://api.prover.xyz/ws"
  api_endpoint: "https://api.prover.xyz"
  venus_grpc_endpoint: "127.0.0.1:7000"

timing:
  heartbeat_interval: 14
  bid_delay: 0
  accept_delay: 0
  submit_hash_delay: 0
  submit_data_delay: 0
```

## Usage

```bash
# Make sure venus_prover_server is running on port 7000 first!
# Then start the custom prover:
python3 prover.py --config config.yaml
```

## Status: Work in Progress

### Confirmed (from packet capture):
- [x] WebSocket heartbeat protocol
- [x] Signing mechanism (secp256k1 + keccak256)
- [x] `submitTaskDataRaw` HTTP API format
- [x] Task notification decoding (gzip + base64)
- [x] Task update decoding (base64 JSON)

### Needs Verification (from packet capture):
- [ ] WebSocket bid action number (assumed action=3)
- [ ] WebSocket start/finish work action numbers
- [ ] `nextStep` request body format
- [ ] accept task mechanism (HTTP or chain tx?)
- [ ] venus_prover_server gRPC proto definition
- [ ] submitProofHash mechanism (HTTP or chain tx?)

## Required Packet Captures

To complete this implementation, capture these requests:

1. **Bid submission** - WebSocket message when "send submit bid" appears in log
2. **nextStep** - Full HTTP POST body to `/api/v1/common/task/prover/nextStep`
3. **Start/Finish work** - WebSocket messages for these events
4. **Accept task** - Whether this is HTTP API or chain transaction
5. **Submit proof hash** - The chain transaction or HTTP call

## File Structure

```
custom_prover/
├── prover.py          # Main orchestrator
├── signer.py          # Signing module (secp256k1 + keccak256)
├── ws_client.py       # WebSocket client
├── api_client.py      # HTTP API client
├── venus_client.py    # Venus prover gRPC client
├── config.yaml        # Configuration
├── requirements.txt   # Python dependencies
└── README.md          # This file
```
