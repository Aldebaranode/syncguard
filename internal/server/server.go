package server

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/constants"
	"github.com/aldebaranode/syncguard/internal/crypto"
	"github.com/aldebaranode/syncguard/internal/logger"
	"github.com/aldebaranode/syncguard/internal/state"
)

// StateProvider provides access to validator state
type StateProvider interface {
	LoadState() (*state.ValidatorState, error)
	AcquireLock() error
	ReleaseLock() error
}

// KeyProvider provides access to validator key operations. The key never
// leaves the process unencrypted: callers exchange ciphertext bound to the
// cluster secret.
type KeyProvider interface {
	EncryptKeyToBytes(secret string) ([]byte, error)
	DecryptKeyFromBytes(data []byte, secret string) error
	DeleteKey() error
	// HoldsValidatorKey reports whether the key file holds the validator key,
	// so a node never takes over on a mock key.
	HoldsValidatorKey() (bool, error)
	// RestoreKey puts the real key back after a release that could not finish.
	RestoreKey() error
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
	// BeginWarmup suppresses failover during the node's post-restart boot so a
	// takeover restart does not immediately re-trigger a failover.
	BeginWarmup()
}

// NodeRestarter restarts the validator node process
type NodeRestarter interface {
	Restart() error
}

// Server handles HTTP peer communication
type Server struct {
	port           int
	secret         string
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
		secret:         cfg.Secret,
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

// verifyKeyTransferAuth validates the HMAC signature and timestamp headers on a
// key-transfer request, rejecting unsigned, tampered, or replayed requests.
func (s *Server) verifyKeyTransferAuth(r *http.Request) bool {
	signature := r.Header.Get(constants.HeaderSignature)
	timestamp, err := strconv.ParseInt(r.Header.Get(constants.HeaderTimestamp), 10, 64)
	if err != nil {
		return false
	}

	return crypto.VerifyTimedSignature(
		constants.AuthPayloadValidatorKey,
		signature,
		s.secret,
		timestamp,
		constants.KeyTransferTimeoutMs,
	)
}

// handleValidatorKey handles key transfer requests. Every request must carry a
// valid signed header pair; the key itself is encrypted on GET and decrypted on
// POST so it is never exposed in plaintext over the wire.
func (s *Server) handleValidatorKey(w http.ResponseWriter, r *http.Request) {
	if !s.verifyKeyTransferAuth(r) {
		s.logger.Warn("Rejected unauthenticated validator key request from %s", r.RemoteAddr)
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	if r.Method == http.MethodGet {
		keyData, err := s.keyProvider.EncryptKeyToBytes(s.secret)
		if err != nil {
			http.Error(w, "No key available", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/octet-stream")
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

		if err := s.keyProvider.DecryptKeyFromBytes(body, s.secret); err != nil {
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

		holds, err := s.keyProvider.HoldsValidatorKey()
		if err != nil || !holds {
			s.logger.Error("Refusing takeover, validator key not present (holds=%v, err=%v)", holds, err)
			http.Error(w, "Validator key not present", http.StatusInternalServerError)
			return
		}

		if err := s.stateProvider.AcquireLock(); err != nil {
			s.logger.Error("Failed to acquire state lock: %v", err)
			http.Error(w, "Failed to acquire lock", http.StatusInternalServerError)
			return
		}

		// Begin the warm-up window before the restart so the node's boot time
		// is never counted as a failure (prevents the failover ping-pong).
		s.nodeStatus.BeginWarmup()

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

// handleFailbackNotify releases validator duties for a failback. It answers
// only after the node has restarted onto the mock key. When the key swap or
// the restart fails it answers 500 and keeps this node active, so the primary
// never starts while this node may still be signing.
func (s *Server) handleFailbackNotify(w http.ResponseWriter, r *http.Request) {
	s.logger.Info("Received failback notification from peer")

	if !s.nodeStatus.IsActive() {
		w.WriteHeader(http.StatusOK)
		return
	}

	s.logger.Info("Releasing validator duties for failback")

	if err := s.keyProvider.DeleteKey(); err != nil {
		s.logger.Error("Failed to disable key: %v", err)
		http.Error(w, "Failed to disable key", http.StatusInternalServerError)
		return
	}

	if s.nodeRestarter != nil {
		if err := s.nodeRestarter.Restart(); err != nil {
			s.logger.Error("Failed to restart node: %v", err)
			// The running node may still hold the real key in memory, so put
			// the real key file back and stay active.
			if restoreErr := s.keyProvider.RestoreKey(); restoreErr != nil {
				s.logger.Error("Failed to restore key after failed release: %v", restoreErr)
			}
			http.Error(w, "Failed to restart node", http.StatusInternalServerError)
			return
		}
	}

	if err := s.stateProvider.ReleaseLock(); err != nil {
		s.logger.Error("Failed to release state lock: %v", err)
	}

	s.nodeStatus.SetActive(false)
	s.logger.Info("Successfully released validator duties")
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
