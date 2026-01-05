# SyncGuard

> ⚠️ **Disclaimer:** This project is developed as part of my **undergraduate research** for testing and educational purposes only. Use at your own risk. Not recommended for production validator infrastructure without thorough testing and security review.

## Problem Statement

Existing CometBFT remote signing solutions like [Horcrux](https://github.com/strangelove-ventures/horcrux) are **not compatible with Story Network**. The Story client has disabled CometBFT's remote signing functionality (`priv_validator_laddr`), making traditional remote signer setups unusable.

This creates a challenge for Story Network validators who need high-availability setups to minimize downtime and slashing risks. Without remote signing support, validators cannot use distributed key management or threshold signing schemes.

**SyncGuard** provides an alternative approach by implementing failover at the node level rather than the signing level, using state synchronization to safely switch between active and passive validator nodes.

## Overview

This implementation provides high-availability failover for Story Network validators with:
- **Active-Passive topology** with automatic failover
- **Double-signing prevention** through state synchronization 
- **Health monitoring** for both CometBFT and EVM layers
- **Safe validator state management** with atomic operations

## Architecture

### Infrastructure Topology
```
┌─────────────────────────────────────┐
│            Story Network            │
├─────────────────────────────────────┤
│  ┌──────────┐     ┌──────────┐     │
│  │ Primary  │◄────►│ Secondary│     │
│  │  Active  │     │  Passive │     │
│  │          │     │          │     │
│  │ CometBFT │     │ CometBFT │     │
│  │   EVM    │     │   EVM    │     │
│  │          │     │          │     │
│  │ Signing  │     │ Standby  │     │
│  └──────────┘     └──────────┘     │
└─────────────────────────────────────┘
```

## Installation

```bash
# Build SyncGuard
go build -o syncguard ./cli

# Run Story Network failover
./syncguard story --config config-story.yaml --role active
```

## Configuration

Create `config-story.yaml`:

```yaml
server:
  id: "story-validator-1"
  role: "primary"
  port: 8080

story:
  comet_rpc_url: "http://localhost:26657"
  evm_rpc_url: "http://localhost:8545"
  validator_state_path: "/home/story/.story/story/data/priv_validator_state.json"
  validator_state_backup: "/home/story/backup/priv_validator_state.json"
  node_role: "active"  # "active" or "passive"
  is_primary_site: true
  min_peers: 3
  block_interval: 500
  state_sync_interval: 5

failover:
  health_check_interval: 5
  retry_attempts: 3
  fallback_grace_period: 60

communication:
  peers:
    - id: "story-validator-2"
      address: "192.168.1.2:8080"

logging:
  level: "info"
  verbose: true
  enable_alerts: true
```

## Double-Signing Prevention

### Key Safety Mechanisms:

1. **Exclusive State Locking**: Only one node can hold the validator lock
2. **Height/Round/Step Tracking**: Prevents signing duplicate consensus messages
3. **State Synchronization**: Ensures consistent view between nodes
4. **Grace Periods**: Delays before state transitions

### Validator State Structure:
```json
{
  "height": 1000,
  "round": 0, 
  "step": 3,
  "signature": "...",
  "signbytes": "..."
}
```

## Health Monitoring

### CometBFT Health Checks:
- RPC endpoint availability (`/status`)
- Block height progression
- Sync status (catching_up)
- Peer connectivity (`/net_info`)

### EVM Health Checks:
- JSON-RPC availability (`eth_blockNumber`)
- Block production
- Transaction processing

### System Health:
- Minimum peer threshold
- Block interval timing
- State synchronization lag

## Failover Process

### 1. Detection Phase
```
Health Check Fails → Retry (3x) → Trigger Failover
```

### 2. State Synchronization
```
Passive Node:
1. Acquire state lock
2. Sync from active node
3. Verify consistency
4. Update local state
```

### 3. Activation
```
Active Node:      Passive Node:
1. Release lock   1. Start signing
2. Stop signing   2. Begin consensus
3. Notify peers   3. Update status
```

### 4. Fallback
```
Primary Recovery:
1. Health restored
2. Grace period (60s)
3. Sync state
4. Reclaim active role
```

## Usage Examples

### Run Active Validator
```bash
./syncguard story --config config-story.yaml --role active
```

### Run Passive Standby
```bash 
./syncguard story --config config-story.yaml --role passive
```

### Monitor Health Status
```bash
curl http://localhost:8080/health
```

### Check Validator State
```bash
curl http://localhost:8080/validator_state
```

## Testing

Run comprehensive tests:

```bash
# All Story Network tests
go test ./tests -run TestStory -v

# Validator state synchronization
go test ./tests -run TestValidator -v

# Double-signing prevention
go test ./tests -run TestDouble -v
```

## Security Considerations

### Critical Files:
- `priv_validator_key.json`: **Never share** between nodes
- `priv_validator_state.json`: Synchronized between nodes
- `node_key.json`: Unique per node

### Network Security:
- Secure peer-to-peer communication
- Firewall rules for RPC endpoints
- Encrypted state synchronization

### Operational Security:
- Regular state backups
- Monitoring for double-sign attempts
- Automated alerting for failover events

## Monitoring & Alerting

Key metrics to monitor:
- Block height progression
- Health check status
- State sync lag
- Peer connectivity
- Failover events

## Troubleshooting

### Common Issues:

1. **State Lock Conflicts**
   ```
   Error: state is already locked
   Solution: Ensure only one node is active
   ```

2. **Double-Sign Prevention**
   ```
   Error: already signed at height X
   Solution: Check state synchronization
   ```

3. **Health Check Failures**
   ```
   Error: CometBFT/EVM unhealthy
   Solution: Verify node status and connectivity
   ```

## Support

For issues and questions:
- Check logs: `tail -f story-failover.log`
- Verify configuration: `./syncguard story --config config-story.yaml --help`
- Test connectivity: health check endpoints