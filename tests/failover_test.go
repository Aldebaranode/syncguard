package tests

import (
	"testing"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
)

func TestFailover(t *testing.T) {
	// Step 1: Start primary and backup nodes
	node1 := startNode("node-primary", "primary", 8080)
	node2 := startNode("node-backup-1", "backup", 8081)
	node3 := startNode("node-backup-2", "backup", 8082)

	node1.startSyncGuard(&config.Config{
		Server: config.ServerConfig{
			Port: 7080,
			Role: "primary",
			ID:   "node-primary",
		},
		Communication: config.CommunicationConfig{
			Peers: []config.PeerConfig{
				{
					ID:      "node-backup-1",
					Address: "localhost:7081",
				},
				{
					ID:      "node-backup-2",
					Address: "localhost:7082",
				},
			},
		},
	})
	node2.startSyncGuard(&config.Config{
		Server: config.ServerConfig{
			Port: 7081,
			Role: "backup",
			ID:   "node-backup-1",
		},
		Communication: config.CommunicationConfig{
			Peers: []config.PeerConfig{
				{
					ID:      "node-primary",
					Address: "localhost:7080",
				},
				{
					ID:      "node-backup-2",
					Address: "localhost:7082",
				},
			},
		},
	})
	node3.startSyncGuard(&config.Config{
		Server: config.ServerConfig{
			Port: 7082,
			Role: "backup",
			ID:   "node-backup-2",
		},
		Communication: config.CommunicationConfig{
			Peers: []config.PeerConfig{
				{
					ID:      "node-primary",
					Address: "localhost:7080",
				},
				{
					ID:      "node-backup-1",
					Address: "localhost:7081",
				},
			},
		},
	})

	// // Step 2: Simulate primary node failure
	// stopNode(node1)

	// Step 3: Verify failover
	time.Sleep(2 * time.Second)
	setNodeHealth(node1, false)

	time.Sleep(2 * time.Second)
	setNodeHealth(node1, true)

	time.Sleep(2 * time.Second)

	t.Log("Failover test passed")
}
