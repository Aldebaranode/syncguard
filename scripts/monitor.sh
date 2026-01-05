#!/bin/bash
# Monitor SyncGuard health and node status

set -e

SYNCGUARD_PORT=${1:-8080}
COMET_PORT=${2:-26657}
INTERVAL=${3:-5}

echo "Monitoring SyncGuard on :$SYNCGUARD_PORT, CometBFT on :$COMET_PORT"
echo "Press Ctrl+C to stop"
echo ""

while true; do
    echo "=== $(date '+%H:%M:%S') ==="
    
    # Check SyncGuard health
    if HEALTH=$(curl -s --max-time 2 http://localhost:$SYNCGUARD_PORT/health 2>/dev/null); then
        echo "SyncGuard: $(echo $HEALTH | jq -c '.')"
    else
        echo "SyncGuard: UNREACHABLE"
    fi
    
    # Check CometBFT
    if STATUS=$(curl -s --max-time 2 http://localhost:$COMET_PORT/status 2>/dev/null); then
        HEIGHT=$(echo $STATUS | jq -r '.result.sync_info.latest_block_height')
        CATCHING=$(echo $STATUS | jq -r '.result.sync_info.catching_up')
        echo "CometBFT:  height=$HEIGHT syncing=$CATCHING"
    else
        echo "CometBFT:  UNREACHABLE"
    fi
    
    # Check peers
    if NET=$(curl -s --max-time 2 http://localhost:$COMET_PORT/net_info 2>/dev/null); then
        PEERS=$(echo $NET | jq -r '.result.n_peers')
        echo "Peers:     $PEERS"
    fi
    
    echo ""
    sleep $INTERVAL
done
