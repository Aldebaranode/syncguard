# SyncGuard

> ⚠️ **Disclaimer:** This project is under development as part of author **undergraduate research** for testing and educational purposes only. Use at your own risk. Not recommended for production validator infrastructure without thorough testing and security review.

High-availability failover system for CometBFT-based validator nodes.

## Requirements

- Go 1.21+
- CometBFT-based validator node (e.g., Story Network)

## Problem Statement

Existing CometBFT remote signing solutions like [Horcrux](https://github.com/strangelove-ventures/horcrux) are **not compatible with Story Network**. The Story client has disabled CometBFT's remote signing functionality (`priv_validator_laddr`), making traditional remote signer setups unusable.

This creates a challenge for Story Network validators who need high-availability setups to minimize downtime and slashing risks. Without remote signing support, validators cannot use distributed key management or threshold signing schemes.

**SyncGuard** provides an alternative approach by implementing failover at the node level rather than the signing level, using state synchronization to safely switch between active and passive validator nodes.

## Features

- **Active-Passive topology** with automatic failover
- **Double-signing prevention** through state synchronization 
- **CometBFT health monitoring** (block height, sync status, peer count)
- **Safe validator state management** with atomic file operations
- **HTTP-based peer communication** for failover coordination

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Story Network                      │
├─────────────────────────────────────────────────────┤
│  ┌──────────────┐          ┌──────────────┐        │
│  │   Primary    │◄────────►│  Secondary   │        │
│  │   (Active)   │  HTTP    │  (Passive)   │        │
│  │              │  :8080   │              │        │
│  │  CometBFT    │          │  CometBFT    │        │
│  │   :26657     │          │   :26657     │        │
│  └──────────────┘          └──────────────┘        │
└─────────────────────────────────────────────────────┘
```

## Installation

```bash
# Clone
git clone https://github.com/aldebaranode/syncguard
cd syncguard

# Build
make build

# Run
./bin/syncguard --config config.yaml --role active
```

## Configuration

Create `config.yaml`:

```yaml
node:
  id: "validator-1"
  role: "active"              # "active" or "passive"
  is_primary: true            # Primary gets failback priority
  port: 8080

peers:
  - id: "validator-2"
    address: "192.168.1.2:8080"

cometbft:
  rpc_url: "http://localhost:26657"
  state_path: "/home/story/.story/story/data/priv_validator_state.json"
  backup_path: "/home/story/backup/priv_validator_state.json"

health:
  interval: 5                 # Health check interval (seconds)
  min_peers: 3                # Minimum peers to be healthy
  timeout: 5                  # HTTP timeout (seconds)

failover:
  retry_attempts: 3           # Failures before failover
  grace_period: 60            # Wait before failback (seconds)
  state_sync_interval: 5      # State sync frequency (seconds)

logging:
  level: "info"
  file: "syncguard.log"
  verbose: false
```

## Usage

```bash
# Run as active validator
./bin/syncguard --config config.yaml --role active

# Run as passive standby
./bin/syncguard --config config.yaml --role passive

# Development with live-reload
make watch
```

## Health Monitoring

SyncGuard monitors CometBFT health via:

| Endpoint | Check |
|----------|-------|
| `/status` | Block height, sync status (`catching_up`) |
| `/net_info` | Peer count |

A node is **healthy** when:
- CometBFT is responsive
- Not syncing (`catching_up: false`)
- Peer count >= `min_peers`

## Failover Process

```
1. DETECTION
   Health Check Fails → Retry (3x) → Trigger Failover

2. FAILOVER
   Active Node                    Passive Node
   ├─ Release state lock          ├─ Receive notification
   ├─ POST /failover_notify  ────►├─ Acquire state lock
   └─ isActive = false            └─ isActive = true

3. FAILBACK (Primary site only)
   Primary recovers → Wait grace period → Reclaim active role
```

## Double-Sign Prevention

Three layers of protection:

1. **File Locking** - Exclusive `.lock` file on `priv_validator_state.json`
2. **State Comparison** - Never sync if remote height > local height
3. **Signature Tracking** - In-memory record of signed (height, round, step)

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Node health status |
| `/validator_state` | GET | Current validator state |
| `/validator_key` | GET/POST | Transfer validator key during failover |
| `/failover_notify` | POST | Trigger failover takeover |
| `/failback_notify` | POST | Trigger failback release |

## Security

### Key Transfer

During failover, the **validator private key is transferred over HTTP** from the active node to the passive node:

```
Active (failing) → POST /validator_key → Passive (taking over)
```

> ⚠️ **Current Limitation**: Key is transferred in plaintext over the network. For production use, consider:
> - Using TLS/mTLS between peers
> - VPN or private network between nodes
> - Encrypting the key payload

### Key Management

| Action | What Happens |
|--------|--------------|
| Failover | Active sends key to passive, then renames key to `.disabled` |
| Failback | Primary requests key from secondary, secondary disables its key |
| Restore | Key can be restored from `.disabled` or backup |

### File Security

| File | Handling |
|------|----------|
| `priv_validator_key.json` | Transferred during failover, only one node has active key |
| `priv_validator_state.json` | Synchronized continuously between nodes |
| `*.disabled` | Disabled keys (renamed, not deleted) |

> ⚠️ Ensure firewall rules restrict access to ports 8080 and 26657.

## Project Structure

```
syncguard/
├── cli/cmd/cmd.go           # CLI entry point
├── internal/
│   ├── config/              # Configuration loading + validation
│   ├── manager/             # Failover orchestration (FailoverManager)
│   ├── health/              # CometBFT health checking (Checker)
│   ├── state/               # Validator state + key management
│   │   ├── manager.go       # State file sync with file locking
│   │   ├── key.go           # Validator key transfer/backup
│   │   └── double_sign.go   # In-memory signature tracking
│   └── logger/              # Structured logging
├── scripts/                 # Utility scripts
├── config.yaml              # Configuration file
└── Makefile
```

## Testing

```bash
# Run all tests
go test -v ./...

# Run tests with race detection
go test -v -race ./...

# Run with coverage
go test -v -coverprofile=coverage.out ./...
go tool cover -html=coverage.out -o coverage.html
```

## Development

```bash
# Live reload during development
make watch

# Build binary
make build
```

## License

MIT