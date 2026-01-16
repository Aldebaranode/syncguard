package server

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/logger"
	"github.com/aldebaranode/syncguard/internal/state"
)

// StateProvider provides access to validator state
type StateProvider interface {
	LoadState() (*state.ValidatorState, error)
	AcquireLock() error
	ReleaseLock() error
}

// KeyProvider provides access to validator key operations
type KeyProvider interface {
	KeyToBytes() ([]byte, error)
	KeyFromBytes(data []byte) error
	DeleteKey() error
}

// HealthProvider provides health status
type HealthProvider interface {
	IsHealthy() bool
	GetLastHeight() int64
}

// NodeStatusProvider provides node status and control
type NodeStatusProvider interface {
	IsActive() bool
	IsPrimary() bool
	SetActive(active bool)
}

// NodeRestarter restarts the validator node process
type NodeRestarter interface {
	Restart() error
}

// Server handles HTTP peer communication
type Server struct {
	port           int
	stateProvider  StateProvider
	keyProvider    KeyProvider
	healthProvider HealthProvider
	nodeStatus     NodeStatusProvider
	nodeRestarter  NodeRestarter
	logger         *logger.Logger
	httpServer     *http.Server
}

// NewServer creates a new peer communication server
func NewServer(
	cfg *config.Config,
	stateProvider StateProvider,
	keyProvider KeyProvider,
	healthProvider HealthProvider,
	nodeStatus NodeStatusProvider,
	nodeRestarter NodeRestarter,
) *Server {
	newLogger := logger.NewLogger(cfg)
	newLogger.WithModule("server")

	return &Server{
		port:           cfg.Node.Port,
		stateProvider:  stateProvider,
		keyProvider:    keyProvider,
		healthProvider: healthProvider,
		nodeStatus:     nodeStatus,
		nodeRestarter:  nodeRestarter,
		logger:         newLogger,
	}
}

// Start starts the HTTP server
func (s *Server) Start() error {
	mux := http.NewServeMux()

	mux.HandleFunc("/validator_state", s.handleValidatorState)
	mux.HandleFunc("/validator_key", s.handleValidatorKey)
	mux.HandleFunc("/failover_notify", s.handleFailoverNotify)
	mux.HandleFunc("/failback_notify", s.handleFailbackNotify)
	mux.HandleFunc("/health", s.handleHealth)

	s.httpServer = &http.Server{
		Addr:    fmt.Sprintf(":%d", s.port),
		Handler: mux,
	}

	s.logger.Info("Starting peer server on port %d", s.port)
	return s.httpServer.ListenAndServe()
}

// Stop gracefully stops the HTTP server
func (s *Server) Stop() error {
	if s.httpServer != nil {
		return s.httpServer.Close()
	}
	return nil
}

// handleValidatorState returns current validator state
func (s *Server) handleValidatorState(w http.ResponseWriter, r *http.Request) {
	validatorState, err := s.stateProvider.LoadState()
	if err != nil {
		http.Error(w, "Failed to load state", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(validatorState)
}

// handleValidatorKey handles key transfer requests
func (s *Server) handleValidatorKey(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet {
		keyData, err := s.keyProvider.KeyToBytes()
		if err != nil {
			http.Error(w, "No key available", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write(keyData)
		return
	}

	if r.Method == http.MethodPost {
		s.logger.Info("Receiving validator key from peer")

		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "Failed to read body", http.StatusBadRequest)
			return
		}

		if err := s.keyProvider.KeyFromBytes(body); err != nil {
			s.logger.Error("Failed to save received key: %v", err)
			http.Error(w, "Failed to save key", http.StatusInternalServerError)
			return
		}

		s.logger.Info("Successfully received and saved validator key")
		w.WriteHeader(http.StatusOK)
		return
	}

	http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
}

// handleFailoverNotify processes failover notification from peer
func (s *Server) handleFailoverNotify(w http.ResponseWriter, r *http.Request) {
	s.logger.Info("Received failover notification from peer")

	if !s.nodeStatus.IsActive() && s.healthProvider.IsHealthy() {
		s.logger.Info("Taking over validator duties")

		if err := s.stateProvider.AcquireLock(); err != nil {
			s.logger.Error("Failed to acquire state lock: %v", err)
			http.Error(w, "Failed to acquire lock", http.StatusInternalServerError)
			return
		}

		// Restart node to pick up the new key (received earlier via POST /validator_key)
		if s.nodeRestarter != nil {
			if err := s.nodeRestarter.Restart(); err != nil {
				s.logger.Error("Failed to restart node: %v", err)
				http.Error(w, "Failed to restart node", http.StatusInternalServerError)
				return
			}
		}

		s.nodeStatus.SetActive(true)
		s.logger.Info("Successfully took over as active validator")
	}

	w.WriteHeader(http.StatusOK)
}

// handleFailbackNotify processes failback notification from peer
func (s *Server) handleFailbackNotify(w http.ResponseWriter, r *http.Request) {
	s.logger.Info("Received failback notification from peer")

	if s.nodeStatus.IsActive() {
		s.logger.Info("Releasing validator duties for failback")

		// Disable our key (swap to mock) before releasing
		if err := s.keyProvider.DeleteKey(); err != nil {
			s.logger.Error("Failed to disable key: %v", err)
		}

		// Restart node to pick up the disabled key
		if s.nodeRestarter != nil {
			if err := s.nodeRestarter.Restart(); err != nil {
				s.logger.Error("Failed to restart node: %v", err)
			}
		}

		if err := s.stateProvider.ReleaseLock(); err != nil {
			s.logger.Error("Failed to release state lock: %v", err)
		}

		s.nodeStatus.SetActive(false)
		s.logger.Info("Successfully released validator duties")
	}

	w.WriteHeader(http.StatusOK)
}

// handleHealth returns health status for peer monitoring
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	status := map[string]interface{}{
		"healthy": s.healthProvider.IsHealthy(),
		"active":  s.nodeStatus.IsActive(),
		"primary": s.nodeStatus.IsPrimary(),
		"height":  s.healthProvider.GetLastHeight(),
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(status)
}
