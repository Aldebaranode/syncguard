package tests

import (
	"net/http"
	"testing"
	"time"
)

func TestFailover(t *testing.T) {
	// Step 1: Start primary and backup nodes
	node1 := startNode("node-primary", "primary", 8080)
	node2 := startNode("node-backup-1", "backup", 8081)

	node1.startSyncGuard()
	node2.startSyncGuard()

	// // Step 2: Simulate primary node failure
	// stopNode(node1)

	// Step 3: Verify failover
	time.Sleep(5 * time.Second)

	setNodeHealth(node1, false)

	time.Sleep(5 * time.Second)

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
