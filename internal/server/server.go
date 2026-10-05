package server

import (
	"encoding/json"
	"errors"
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
}

// KeyProvider provides access to validator key operations. The key never
// leaves the process unencrypted: callers exchange ciphertext bound to the
// cluster secret.
type KeyProvider interface {
	EncryptKeyToBytes(secret string) ([]byte, error)
	// StageKeyFromBytes keeps a received validator key in the staged file; it
	// never arms the key file the node reads.
	StageKeyFromBytes(data []byte, secret string) error
}

// HealthProvider provides health status
type HealthProvider interface {
	IsHealthy() bool
	GetLastHeight() int64
}

// NodeStatusProvider provides node status
type NodeStatusProvider interface {
	IsActive() bool
	IsPrimary() bool
}

// RoleTransitions performs the role changes a peer asks for. Each call either
// completes the transition or returns why it did not.
type RoleTransitions interface {
	TakeOver() error
	ReleaseForFailback() error
}

// TransitionFailure is an error that carries the HTTP status and the reason
// code to answer a refused or failed role transition with.
// manager.TransitionError implements it; the server cannot name that type
// because the manager package imports the server.
type TransitionFailure interface {
	error
	HTTPStatus() int
	ReasonCode() string
}

// Server handles HTTP peer communication
type Server struct {
	port           int
	secret         string
	stateProvider  StateProvider
	keyProvider    KeyProvider
	healthProvider HealthProvider
	nodeStatus     NodeStatusProvider
	roles          RoleTransitions
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
	roles RoleTransitions,
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
		roles:          roles,
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

// verifyPeerAuth validates the HMAC signature and timestamp headers on a peer
// request against the endpoint's payload, rejecting unsigned, tampered,
// replayed, or cross-endpoint requests.
func (s *Server) verifyPeerAuth(r *http.Request, payload string) bool {
	signature := r.Header.Get(constants.HeaderSignature)
	timestamp, err := strconv.ParseInt(r.Header.Get(constants.HeaderTimestamp), 10, 64)
	if err != nil {
		return false
	}

	return crypto.VerifyTimedSignature(
		payload,
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
	if !s.verifyPeerAuth(r, constants.AuthPayloadValidatorKey) {
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

		if err := s.keyProvider.StageKeyFromBytes(body, s.secret); err != nil {
			s.logger.Error("Failed to stage received key: %v", err)
			http.Error(w, "Failed to stage key", http.StatusInternalServerError)
			return
		}

		s.logger.Info("Successfully received and staged validator key")
		w.WriteHeader(http.StatusOK)
		return
	}

	http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
}

// handleFailoverNotify asks this node to take over as the active validator.
func (s *Server) handleFailoverNotify(w http.ResponseWriter, r *http.Request) {
	if !s.acceptNotify(w, r, "failover", constants.AuthPayloadFailoverNotify) {
		return
	}
	s.logger.Info("Received failover notification from peer")
	s.writeTransitionResult(w, "takeover", s.roles.TakeOver())
}

// handleFailbackNotify asks this node to release validator duties so the
// primary can fail back. It answers only after the release has finished.
func (s *Server) handleFailbackNotify(w http.ResponseWriter, r *http.Request) {
	if !s.acceptNotify(w, r, "failback", constants.AuthPayloadFailbackNotify) {
		return
	}
	s.logger.Info("Received failback notification from peer")
	s.writeTransitionResult(w, "failback release", s.roles.ReleaseForFailback())
}

// acceptNotify admits a notify request that is a POST and carries a valid
// signature for payload. Otherwise it answers 405 or 401 and returns false.
func (s *Server) acceptNotify(w http.ResponseWriter, r *http.Request, endpoint, payload string) bool {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return false
	}
	if !s.verifyPeerAuth(r, payload) {
		s.logger.Warn("Rejected unauthenticated %s notify request from %s", endpoint, r.RemoteAddr)
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return false
	}
	return true
}

// writeTransitionResult answers 200 for a completed transition, the carried
// status with the reason code for a TransitionFailure, and 500 for any other
// error. Every non-2xx answer is logged with its reason.
func (s *Server) writeTransitionResult(w http.ResponseWriter, transition string, err error) {
	if err == nil {
		w.WriteHeader(http.StatusOK)
		return
	}

	status := http.StatusInternalServerError
	code := "internal_error"
	var failure TransitionFailure
	if errors.As(err, &failure) {
		status = failure.HTTPStatus()
		code = failure.ReasonCode()
	}
	s.logger.Error("Refused %s with %d %s: %v", transition, status, code, err)
	http.Error(w, code, status)
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
