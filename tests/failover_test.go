package tests

import (
	"net/http"
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
					Address: "localhost:8081",
				},
				{
					ID:      "node-backup-2",
					Address: "localhost:8082",
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
					Address: "localhost:8080",
				},
				{
					ID:      "node-backup-2",
					Address: "localhost:8082",
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
					Address: "localhost:8080",
				},
				{
					ID:      "node-backup-1",
					Address: "localhost:8081",
				},
			},
		},
	})

	// // Step 2: Simulate primary node failure
	// stopNode(node1)

	// Step 3: Verify failover
	time.Sleep(2 * time.Second)

	setNodeHealth(node1, false)

	time.Sleep(4 * time.Second)

	setNodeHealth(node1, true)

	time.Sleep(60 * time.Second)

	resp, err := http.Get("http://localhost:8081/status")
	if err != nil {
		t.Fatalf("Failed to reach backup node: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("Backup node did not take over: received status %d", resp.StatusCode)
	}

	t.Log("Failover test passed")
	stopNode(node2)
}
