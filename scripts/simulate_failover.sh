#!/bin/bash
# Simulate failover by stopping/starting CometBFT service

set -e

ACTION=${1:-help}

case $ACTION in
    stop)
        echo "Simulating node failure..."
        # Option 1: Stop systemd service
        # sudo systemctl stop story-node
        
        # Option 2: Block CometBFT port
        # sudo iptables -A INPUT -p tcp --dport 26657 -j DROP

        # Option 3: Stop Docker container
        # sudo docker stop story-node
        
        echo "Node stopped. SyncGuard should trigger failover after 3 health check failures."
        ;;
    
    start)
        echo "Restoring node..."
        # Option 1: Start systemd service
        # sudo systemctl start story-node
        
        # Option 2: Unblock port
        # sudo iptables -D INPUT -p tcp --dport 26657 -j DROP

        # Option 3: Start Docker container
        # sudo docker start story-node
        
        echo "Node restored. SyncGuard should failback after grace period."
        ;;
    
    status)
        echo "=== Node Status ==="
        curl -s http://localhost:26657/status | jq '.result.sync_info'
        echo ""
        echo "=== SyncGuard Status ==="
        curl -s http://localhost:8080/health | jq '.'
        ;;
    
    *)
        echo "Usage: $0 {stop|start|status}"
        echo ""
        echo "  stop   - Simulate node failure"
        echo "  start  - Restore node"
        echo "  status - Show current status"
        ;;
esac
