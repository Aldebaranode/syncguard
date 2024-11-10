package communication

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"

	log "github.com/sirupsen/logrus"

	"github.com/aldebaranode/syncguard/internal/config"
)

type HealthStatus struct {
	NodeID  string `json:"node_id"`
	Healthy bool   `json:"healthy"`
}

// Server is the communication server for handling inter-node communication
type Server struct {
	cfg            *config.Config
	statusLock     sync.Mutex
	nodeStatuses   map[string]bool // Stores the health status of each node
	fallbackSignal chan string     // Channel for triggering failover
}

// NewServer initializes a new communication server with the given config
func NewServer(cfg *config.Config) *Server {
	return &Server{
		cfg:            cfg,
		nodeStatuses:   make(map[string]bool),
		fallbackSignal: make(chan string),
	}
}

// Start begins the HTTP server and listens for incoming requests
func (s *Server) Start() error {
	port := s.cfg.Server.Port
	mux := http.NewServeMux()

	mux.HandleFunc("/health_update", s.handleHealthUpdate)
	mux.HandleFunc("/trigger_failover", s.handleTriggerFailover)

	server := &http.Server{
		Addr:    fmt.Sprintf(":%d", port),
		Handler: mux,
	}

	log.Printf("Starting communication server on port %d", port)
	return server.ListenAndServe()
}

// handleHealthUpdate processes incoming health status updates from other nodes
func (s *Server) handleHealthUpdate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Invalid request method", http.StatusMethodNotAllowed)
		return
	}

	var status HealthStatus
	if err := json.NewDecoder(r.Body).Decode(&status); err != nil {
		http.Error(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	s.statusLock.Lock()
	defer s.statusLock.Unlock()

	s.nodeStatuses[status.NodeID] = status.Healthy
	log.Printf("Updated health status for node %s: %v", status.NodeID, status.Healthy)

	w.WriteHeader(http.StatusOK)
}

// handleTriggerFailover initiates a failover process when triggered
func (s *Server) handleTriggerFailover(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Invalid request method", http.StatusMethodNotAllowed)
		return
	}

	nodeID := r.URL.Query().Get("node_id")
	if nodeID == "" {
		http.Error(w, "Missing node_id parameter", http.StatusBadRequest)
		return
	}

	s.fallbackSignal <- nodeID
	log.Printf("Failover triggered for node %s", nodeID)

	w.WriteHeader(http.StatusOK)
}
