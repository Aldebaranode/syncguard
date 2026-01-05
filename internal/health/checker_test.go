package health_test

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/health"
)

// mockCometBFT creates a mock CometBFT RPC server
func mockCometBFT(healthy bool, syncing bool, height int64, peers int) *httptest.Server {
	mux := http.NewServeMux()

	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		if !healthy {
			http.Error(w, "unhealthy", http.StatusInternalServerError)
			return
		}

		status := map[string]interface{}{
			"result": map[string]interface{}{
				"sync_info": map[string]interface{}{
					"latest_block_height": fmt.Sprintf("%d", height),
					"catching_up":         syncing,
				},
				"node_info": map[string]interface{}{
					"network": "test-network",
					"version": "0.38.0",
				},
			},
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(status)
	})

	mux.HandleFunc("/net_info", func(w http.ResponseWriter, r *http.Request) {
		netInfo := map[string]interface{}{
			"result": map[string]interface{}{
				"n_peers": fmt.Sprintf("%d", peers),
			},
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(netInfo)
	})

	return httptest.NewServer(mux)
}

func testConfig() *config.Config {
	return &config.Config{
		Node: config.NodeConfig{
			ID:   "test-node",
			Role: "active",
			Port: 8080,
		},
		Health: config.HealthConfig{
			Interval: 5,
			MinPeers: 3,
			Timeout:  5,
		},
		Logging: config.LoggingConfig{
			Level:   "error",
			File:    "/dev/null",
			Verbose: false,
		},
	}
}

func TestChecker_HealthyNode(t *testing.T) {
	server := mockCometBFT(true, false, 1000, 5)
	defer server.Close()

	cfg := testConfig()
	checker := health.NewChecker(cfg, server.URL)

	nodeHealth, err := checker.PerformHealthCheck()
	if err != nil {
		t.Fatalf("Health check failed: %v", err)
	}

	if !nodeHealth.Healthy {
		t.Error("Expected node to be healthy")
	}
	if nodeHealth.IsSyncing {
		t.Error("Expected node to not be syncing")
	}
	if nodeHealth.LatestHeight != 1000 {
		t.Errorf("Expected height 1000, got %d", nodeHealth.LatestHeight)
	}
	if nodeHealth.PeerCount != 5 {
		t.Errorf("Expected 5 peers, got %d", nodeHealth.PeerCount)
	}
	if !checker.IsHealthy() {
		t.Error("Checker.IsHealthy() should return true")
	}
}

func TestChecker_SyncingNode(t *testing.T) {
	server := mockCometBFT(true, true, 500, 5)
	defer server.Close()

	cfg := testConfig()
	checker := health.NewChecker(cfg, server.URL)

	nodeHealth, err := checker.PerformHealthCheck()
	if err != nil {
		t.Fatalf("Health check failed: %v", err)
	}

	if nodeHealth.Healthy {
		t.Error("Syncing node should not be marked healthy")
	}
	if !nodeHealth.IsSyncing {
		t.Error("Expected node to be syncing")
	}
	if checker.IsHealthy() {
		t.Error("Syncing node should not pass IsHealthy()")
	}
}

func TestChecker_InsufficientPeers(t *testing.T) {
	server := mockCometBFT(true, false, 1000, 2) // Only 2 peers, min is 3
	defer server.Close()

	cfg := testConfig()
	checker := health.NewChecker(cfg, server.URL)

	_, err := checker.PerformHealthCheck()
	if err != nil {
		t.Fatalf("Health check failed: %v", err)
	}

	if checker.IsHealthy() {
		t.Error("Node with insufficient peers should not pass IsHealthy()")
	}
}

func TestChecker_UnhealthyNode(t *testing.T) {
	server := mockCometBFT(false, false, 0, 0)
	defer server.Close()

	cfg := testConfig()
	checker := health.NewChecker(cfg, server.URL)

	nodeHealth, err := checker.PerformHealthCheck()
	if err != nil {
		t.Fatalf("Health check failed: %v", err)
	}

	if nodeHealth.Healthy {
		t.Error("Unhealthy node should not be marked healthy")
	}
	if checker.IsHealthy() {
		t.Error("Unhealthy node should not pass IsHealthy()")
	}
}

func TestChecker_Unreachable(t *testing.T) {
	cfg := testConfig()
	checker := health.NewChecker(cfg, "http://localhost:99999")

	_, err := checker.PerformHealthCheck()
	if err != nil {
		t.Fatalf("PerformHealthCheck should not return error, it marks health as false: %v", err)
	}

	if checker.IsHealthy() {
		t.Error("Unreachable node should not pass IsHealthy()")
	}
}
