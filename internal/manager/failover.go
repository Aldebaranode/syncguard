package manager

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/health"
	"github.com/aldebaranode/syncguard/internal/logger"
	"github.com/aldebaranode/syncguard/internal/state"
)

// FailoverManager manages the failover process for validator nodes
type FailoverManager struct {
	cfg           *config.Config
	stateManager  *state.Manager
	keyManager    *state.KeyManager
	healthChecker *health.Checker
	isActive      bool
	isPrimarySite bool
	failureCount  int
	mu            sync.RWMutex
	logger        *logger.Logger
	stopCh        chan struct{}
}

// NewFailoverManager creates a new failover manager
func NewFailoverManager(cfg *config.Config) *FailoverManager {
	newLogger := logger.NewLogger(cfg)
	newLogger.WithModule("failover")

	return &FailoverManager{
		cfg:           cfg,
		stateManager:  state.NewManager(cfg.CometBFT.StatePath, cfg.CometBFT.BackupPath),
		keyManager:    state.NewKeyManager(cfg.CometBFT.KeyPath, cfg.CometBFT.BackupPath),
		healthChecker: health.NewChecker(cfg, cfg.CometBFT.RPCURL),
		isPrimarySite: cfg.Node.IsPrimary,
		isActive:      cfg.Node.Role == "active",
		logger:        newLogger,
		stopCh:        make(chan struct{}),
	}
}

// Start begins the failover monitoring process
func (fm *FailoverManager) Start() error {
	fm.logger.Info("Starting failover manager - Primary: %v, Active: %v",
		fm.isPrimarySite, fm.isActive)

	// Load initial validator state
	if _, err := fm.stateManager.LoadState(); err != nil {
		return fmt.Errorf("failed to load validator state: %w", err)
	}

	// Start health monitoring
	go fm.monitorHealth()

	// Start state synchronization if we're passive
	if !fm.isActive {
		go fm.syncValidatorState()
	}

	// Start peer communication server
	go fm.startPeerServer()

	return nil
}

// Stop gracefully stops the failover manager
func (fm *FailoverManager) Stop() {
	close(fm.stopCh)
	fm.stateManager.ReleaseLock()
}

// monitorHealth continuously monitors node health
func (fm *FailoverManager) monitorHealth() {
	ticker := time.NewTicker(time.Duration(fm.cfg.Health.Interval) * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			fm.performHealthCheck()
		case <-fm.stopCh:
			return
		}
	}
}

// performHealthCheck executes health check and handles failures
func (fm *FailoverManager) performHealthCheck() {
	nodeHealth, err := fm.healthChecker.PerformHealthCheck()
	if err != nil {
		fm.logger.Error("Health check error: %v", err)
		fm.handleHealthCheckFailure()
		return
	}

	if fm.healthChecker.IsHealthy() {
		fm.handleHealthCheckSuccess()
	} else {
		fm.logger.Warn("Node unhealthy - Syncing: %v, Height: %d, Peers: %d",
			nodeHealth.IsSyncing, nodeHealth.LatestHeight, nodeHealth.PeerCount)
		fm.handleHealthCheckFailure()
	}
}

// handleHealthCheckSuccess processes successful health checks
func (fm *FailoverManager) handleHealthCheckSuccess() {
	fm.mu.Lock()
	fm.failureCount = 0
	fm.mu.Unlock()

	// If we're primary site and not active, consider failback
	if fm.isPrimarySite && !fm.isActive {
		fm.considerFailback()
	}
}

// handleHealthCheckFailure processes failed health checks
func (fm *FailoverManager) handleHealthCheckFailure() {
	fm.mu.Lock()
	fm.failureCount++
	failureCount := fm.failureCount
	fm.mu.Unlock()

	if failureCount >= fm.cfg.Failover.RetryAttempts {
		if fm.isActive {
			fm.logger.Error("Maximum failures reached, initiating failover")
			fm.initiateFailover()
		}
	}
}

// initiateFailover handles the failover from active to passive
func (fm *FailoverManager) initiateFailover() {
	fm.mu.Lock()
	defer fm.mu.Unlock()

	if !fm.isActive {
		return
	}

	fm.logger.Info("Initiating failover - releasing validator duties")

	// Transfer key to peer before releasing
	if err := fm.transferKeyToPeer(); err != nil {
		fm.logger.Error("Failed to transfer key to peer: %v", err)
		// Continue with failover anyway
	}

	// Disable local key
	if err := fm.keyManager.DeleteKey(); err != nil {
		fm.logger.Error("Failed to disable local key: %v", err)
	}

	if err := fm.stateManager.ReleaseLock(); err != nil {
		fm.logger.Error("Failed to release state lock: %v", err)
	}

	fm.notifyPeerOfFailover()

	fm.isActive = false
	fm.failureCount = 0

	fm.logger.Info("Failover complete - node is now passive")
}

// considerFailback evaluates whether to fail back to primary
func (fm *FailoverManager) considerFailback() {
	fm.mu.RLock()
	isActive := fm.isActive
	fm.mu.RUnlock()

	if isActive {
		return
	}

	time.Sleep(time.Duration(fm.cfg.Failover.GracePeriod) * time.Second)

	if fm.healthChecker.IsHealthy() {
		fm.logger.Info("Primary node healthy, initiating failback")
		fm.initiateFailback()
	}
}

// initiateFailback handles failing back to primary node
func (fm *FailoverManager) initiateFailback() {
	fm.mu.Lock()
	defer fm.mu.Unlock()

	if fm.isActive {
		return
	}

	fm.logger.Info("Initiating failback to primary")

	if err := fm.stateManager.AcquireLock(); err != nil {
		fm.logger.Error("Failed to acquire state lock: %v", err)
		return
	}

	if err := fm.syncStateFromPeer(); err != nil {
		fm.logger.Error("Failed to sync state from peer: %v", err)
		fm.stateManager.ReleaseLock()
		return
	}

	fm.notifyPeerOfFailback()

	fm.isActive = true
	fm.failureCount = 0

	fm.logger.Info("Failback complete - node is now active")
}

// syncValidatorState periodically syncs validator state when passive
func (fm *FailoverManager) syncValidatorState() {
	ticker := time.NewTicker(time.Duration(fm.cfg.Failover.StateSyncInterval) * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			fm.mu.RLock()
			isActive := fm.isActive
			fm.mu.RUnlock()

			if !isActive {
				if err := fm.syncStateFromPeer(); err != nil {
					fm.logger.Error("State sync error: %v", err)
				}
			}
		case <-fm.stopCh:
			return
		}
	}
}

// syncStateFromPeer fetches and syncs validator state from peer
func (fm *FailoverManager) syncStateFromPeer() error {
	if len(fm.cfg.Peers) == 0 {
		return fmt.Errorf("no peer configured")
	}

	peerAddr := fm.cfg.Peers[0].Address
	url := fmt.Sprintf("http://%s/validator_state", peerAddr)

	resp, err := http.Get(url)
	if err != nil {
		return fmt.Errorf("failed to fetch state from peer: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("peer returned status %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("failed to read response: %w", err)
	}

	var remoteState state.ValidatorState
	if err := json.Unmarshal(body, &remoteState); err != nil {
		return fmt.Errorf("failed to parse remote state: %w", err)
	}

	return fm.stateManager.SyncFromRemote(&remoteState)
}

// notifyPeerOfFailover notifies the peer node that we're failing over
func (fm *FailoverManager) notifyPeerOfFailover() {
	if len(fm.cfg.Peers) == 0 {
		return
	}

	peerAddr := fm.cfg.Peers[0].Address
	url := fmt.Sprintf("http://%s/failover_notify", peerAddr)

	req, _ := http.NewRequest(http.MethodPost, url, nil)
	client := &http.Client{Timeout: 5 * time.Second}

	if _, err := client.Do(req); err != nil {
		fm.logger.Error("Failed to notify peer of failover: %v", err)
	}
}

// notifyPeerOfFailback notifies the peer node that we're failing back
func (fm *FailoverManager) notifyPeerOfFailback() {
	if len(fm.cfg.Peers) == 0 {
		return
	}

	peerAddr := fm.cfg.Peers[0].Address
	url := fmt.Sprintf("http://%s/failback_notify", peerAddr)

	req, _ := http.NewRequest(http.MethodPost, url, nil)
	client := &http.Client{Timeout: 5 * time.Second}

	if _, err := client.Do(req); err != nil {
		fm.logger.Error("Failed to notify peer of failback: %v", err)
	}
}

// startPeerServer starts the HTTP server for peer communication
func (fm *FailoverManager) startPeerServer() {
	mux := http.NewServeMux()

	mux.HandleFunc("/validator_state", fm.handleValidatorStateRequest)
	mux.HandleFunc("/validator_key", fm.handleValidatorKeyRequest)
	mux.HandleFunc("/failover_notify", fm.handleFailoverNotification)
	mux.HandleFunc("/failback_notify", fm.handleFailbackNotification)
	mux.HandleFunc("/health", fm.handleHealthRequest)

	server := &http.Server{
		Addr:    fmt.Sprintf(":%d", fm.cfg.Node.Port),
		Handler: mux,
	}

	fm.logger.Info("Starting peer server on port %d", fm.cfg.Node.Port)
	if err := server.ListenAndServe(); err != nil {
		fm.logger.Error("Peer server error: %v", err)
	}
}

// handleValidatorStateRequest returns current validator state
func (fm *FailoverManager) handleValidatorStateRequest(w http.ResponseWriter, r *http.Request) {
	validatorState, err := fm.stateManager.LoadState()
	if err != nil {
		http.Error(w, "Failed to load state", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(validatorState)
}

// handleFailoverNotification processes failover notification from peer
func (fm *FailoverManager) handleFailoverNotification(w http.ResponseWriter, r *http.Request) {
	fm.logger.Info("Received failover notification from peer")

	fm.mu.Lock()
	defer fm.mu.Unlock()

	if !fm.isActive && fm.healthChecker.IsHealthy() {
		fm.logger.Info("Taking over validator duties")

		if err := fm.stateManager.AcquireLock(); err != nil {
			fm.logger.Error("Failed to acquire state lock: %v", err)
			http.Error(w, "Failed to acquire lock", http.StatusInternalServerError)
			return
		}

		fm.isActive = true
		fm.logger.Info("Successfully took over as active validator")
	}

	w.WriteHeader(http.StatusOK)
}

// handleFailbackNotification processes failback notification from peer
func (fm *FailoverManager) handleFailbackNotification(w http.ResponseWriter, r *http.Request) {
	fm.logger.Info("Received failback notification from peer")

	fm.mu.Lock()
	defer fm.mu.Unlock()

	if fm.isActive {
		fm.logger.Info("Releasing validator duties for failback")

		if err := fm.stateManager.ReleaseLock(); err != nil {
			fm.logger.Error("Failed to release state lock: %v", err)
		}

		fm.isActive = false
		fm.logger.Info("Successfully released validator duties")
	}

	w.WriteHeader(http.StatusOK)
}

// handleHealthRequest returns health status for peer monitoring
func (fm *FailoverManager) handleHealthRequest(w http.ResponseWriter, r *http.Request) {
	fm.mu.RLock()
	isActive := fm.isActive
	fm.mu.RUnlock()

	healthy := fm.healthChecker.IsHealthy()

	status := map[string]interface{}{
		"healthy": healthy,
		"active":  isActive,
		"primary": fm.isPrimarySite,
		"height":  fm.healthChecker.GetLastHeight(),
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(status)
}

// transferKeyToPeer sends the validator key to the peer node
func (fm *FailoverManager) transferKeyToPeer() error {
	if len(fm.cfg.Peers) == 0 {
		return fmt.Errorf("no peer configured")
	}

	keyData, err := fm.keyManager.KeyToBytes()
	if err != nil {
		return fmt.Errorf("failed to read key: %w", err)
	}

	peerAddr := fm.cfg.Peers[0].Address
	url := fmt.Sprintf("http://%s/validator_key", peerAddr)

	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(keyData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send key: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("peer returned status %d", resp.StatusCode)
	}

	fm.logger.Info("Successfully transferred validator key to peer")
	return nil
}

// handleValidatorKeyRequest receives the validator key from peer
func (fm *FailoverManager) handleValidatorKeyRequest(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet {
		// Return current key
		keyData, err := fm.keyManager.KeyToBytes()
		if err != nil {
			http.Error(w, "No key available", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write(keyData)
		return
	}

	if r.Method == http.MethodPost {
		// Receive key from peer
		fm.logger.Info("Receiving validator key from peer")

		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "Failed to read body", http.StatusBadRequest)
			return
		}

		if err := fm.keyManager.KeyFromBytes(body); err != nil {
			fm.logger.Error("Failed to save received key: %v", err)
			http.Error(w, "Failed to save key", http.StatusInternalServerError)
			return
		}

		fm.logger.Info("Successfully received and saved validator key")
		w.WriteHeader(http.StatusOK)
		return
	}

	http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
}

// requestKeyFromPeer requests the validator key from peer during failback
func (fm *FailoverManager) requestKeyFromPeer() error {
	if len(fm.cfg.Peers) == 0 {
		return fmt.Errorf("no peer configured")
	}

	peerAddr := fm.cfg.Peers[0].Address
	url := fmt.Sprintf("http://%s/validator_key", peerAddr)

	resp, err := http.Get(url)
	if err != nil {
		return fmt.Errorf("failed to request key from peer: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("peer returned status %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("failed to read key: %w", err)
	}

	if err := fm.keyManager.KeyFromBytes(body); err != nil {
		return fmt.Errorf("failed to save key: %w", err)
	}

	fm.logger.Info("Successfully retrieved validator key from peer")
	return nil
}
