package tests

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/aldebaranode/syncguard/internal/communication"
	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/failover"
	"github.com/aldebaranode/syncguard/internal/health"
	log "github.com/sirupsen/logrus"
)

type Node struct {
	ID        string
	Role      string
	Port      int
	isHealthy bool
	server    *http.Server
	mu        sync.Mutex
}

// startNode starts a simulated node as an HTTP server
func startNode(id, role string, port int) *Node {
	node := &Node{
		ID:        id,
		Role:      role,
		Port:      port,
		isHealthy: true, // Start healthy by default
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", node.healthHandler)
	mux.HandleFunc("/status", node.statusHandler)

	node.server = &http.Server{
		Addr:    fmt.Sprintf(":%d", port),
		Handler: mux,
	}

	go func() {
		log.Printf("Starting node %s on port %d with role %s\n", id, port, role)
		if err := node.server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("Node %s failed to start: %v", id, err)
		}
	}()

	// Allow the server to start
	time.Sleep(500 * time.Millisecond)
	return node
}

// stopNode stops the simulated node
func stopNode(node *Node) {
	log.Printf("Stopping node %s on port %d\n", node.ID, node.Port)
	if err := node.server.Close(); err != nil {
		log.Printf("Error stopping node %s: %v", node.ID, err)
	}
}

// healthHandler simulates the node's health status
func (n *Node) healthHandler(w http.ResponseWriter, r *http.Request) {
	n.mu.Lock()
	defer n.mu.Unlock()

	if !n.isHealthy {
		http.Error(w, "Node is unhealthy", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusOK)
	fmt.Fprintln(w, "Node is healthy")
}

// statusHandler provides the node's role and status
func (n *Node) statusHandler(w http.ResponseWriter, r *http.Request) {
	n.mu.Lock()
	defer n.mu.Unlock()

	status := map[string]string{
		"id":   n.ID,
		"role": n.Role,
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	fmt.Fprintf(w, `{"id": "%s", "role": "%s"}`, n.ID, n.Role)
	json.NewEncoder(w).Encode(status)
}

// startSyncGuard, mock the synguard process on each node
func (n *Node) startSyncGuard() {
	cfg := config.Config{}

	config.SetDefaults(&cfg)

	cfg.Server.Port = n.Port + 5
	cfg.Server.Role = n.Role
	cfg.Server.ID = n.ID
	cfg.Failover.HealthCheckInterval = 1
	cfg.Health.NodePort = n.Port

	commServer := communication.NewServer(&cfg)
	go func() {
		if err := commServer.Start(); err != nil {
			log.Fatalf("Communication server error: %v", err)
		}
	}()

	commClient := communication.NewClient(&cfg, time.Minute*3)
	healthChecker := health.NewHealthChecker(&cfg, commClient)
	failoverManager := failover.NewFailoverManager(&cfg, healthChecker, commServer, commClient)

	go failoverManager.Run()
}

// setNodeHealth toggles the health status of a node
func setNodeHealth(node *Node, healthy bool) {
	node.mu.Lock()
	defer node.mu.Unlock()
	node.isHealthy = healthy
	log.Printf("Node %s health set to: %v", node.ID, healthy)
}
