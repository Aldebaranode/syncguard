package manager

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/health"
	"github.com/aldebaranode/syncguard/internal/logger"
	"github.com/aldebaranode/syncguard/internal/node"
	"github.com/aldebaranode/syncguard/internal/server"
	"github.com/aldebaranode/syncguard/internal/state"
)

// FailoverManager manages the failover process for validator nodes
type FailoverManager struct {
	cfg                *config.Config
	stateManager       *state.Manager
	keyManager         *state.KeyManager
	healthChecker      *health.Checker
	nodeManager        node.Manager
	server             *server.Server
	isActive           bool
	isPrimarySite      bool
	failbackInProgress bool
	failureCount       int
	mu                 sync.RWMutex
	logger             *logger.Logger
	stopCh             chan struct{}
}

// IsActive returns whether this node is currently active
func (fm *FailoverManager) IsActive() bool {
	fm.mu.RLock()
	defer fm.mu.RUnlock()
	return fm.isActive
}

// IsPrimary returns whether this is the primary site
func (fm *FailoverManager) IsPrimary() bool {
	return fm.isPrimarySite
}

// SetActive sets the active state of this node
func (fm *FailoverManager) SetActive(active bool) {
	fm.mu.Lock()
	defer fm.mu.Unlock()
	fm.isActive = active
}

// NewFailoverManager creates a new failover manager
func NewFailoverManager(cfg *config.Config) *FailoverManager {
	newLogger := logger.NewLogger(cfg)
	newLogger.WithModule("failover")

	fm := &FailoverManager{
		cfg:           cfg,
		stateManager:  state.NewManager(cfg.CometBFT.StatePath, cfg.CometBFT.BackupPath),
		keyManager:    state.NewKeyManager(cfg.CometBFT.KeyPath, cfg.CometBFT.BackupPath),
		healthChecker: health.NewChecker(cfg, cfg.CometBFT.RPCURL),
		isPrimarySite: cfg.Node.IsPrimary,
		isActive:      cfg.Node.Role == "active",
		logger:        newLogger,
		stopCh:        make(chan struct{}),
	}

	// Initialize node manager if enabled
	if cfg.Validator.Enabled {
		nodeLogger := logger.NewLogger(cfg)
		nodeLogger.WithModule("node")
		fm.nodeManager = node.NewManager(node.Config{
			Mode:         cfg.Validator.Mode,
			Binary:       cfg.Validator.Binary,
			Args:         cfg.Validator.Args,
			Container:    cfg.Validator.Container,
			ComposeFile:  cfg.Validator.ComposeFile,
			Service:      cfg.Validator.Service,
			StopTimeout:  time.Duration(cfg.Validator.StopTimeout * float64(time.Second)),
			RestartDelay: time.Duration(cfg.Validator.RestartDelay * float64(time.Second)),
		}, nodeLogger)
	}

	return fm
}

// Start begins the failover monitoring process
func (fm *FailoverManager) Start() error {
	fm.logger.Info("Starting failover manager - Primary: %v, Active: %v",
		fm.isPrimarySite, fm.isActive)

	// Start the validator node if wrapper is enabled
	if fm.nodeManager != nil {
		if err := fm.nodeManager.Start(); err != nil {
			return fmt.Errorf("failed to start validator node: %w", err)
		}
		// Wait for node to become healthy
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		if err := fm.nodeManager.WaitHealthy(ctx, fm.healthChecker.IsHealthy); err != nil {
			fm.logger.Warn("Node not healthy after start: %v", err)
		}
	}

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

	// Create and start peer communication server
	fm.server = server.NewServer(fm.cfg, fm.stateManager, fm.keyManager, fm.healthChecker, fm, fm.nodeManager)
	go func() {
		if err := fm.server.Start(); err != nil {
			fm.logger.Error("Server error: %v", err)
		}
	}()

	return nil
}

// Stop gracefully stops the failover manager
func (fm *FailoverManager) Stop() {
	close(fm.stopCh)
	fm.stateManager.ReleaseLock()
	// Stop the validator node if wrapper is enabled
	if fm.nodeManager != nil {
		if err := fm.nodeManager.Stop(); err != nil {
			fm.logger.Error("Failed to stop validator node: %v", err)
		}
	}
}

// monitorHealth continuously monitors node health
func (fm *FailoverManager) monitorHealth() {
	ticker := time.NewTicker(time.Duration(fm.cfg.Health.Interval * float64(time.Second)))
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

	// Log status every interval
	role := "passive"
	if fm.isActive {
		role = "active"
	}
	fm.logger.Info("[%s] height=%d peers=%d healthy=%v",
		role, nodeHealth.LatestHeight, nodeHealth.PeerCount, fm.healthChecker.IsHealthy())

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

	// If we're primary site and not active, consider failback (only start one goroutine)
	fm.mu.RLock()
	alreadyInProgress := fm.failbackInProgress
	fm.mu.RUnlock()

	if fm.isPrimarySite && !fm.isActive && !alreadyInProgress {
		fm.mu.Lock()
		fm.failbackInProgress = true
		fm.mu.Unlock()
		go fm.considerFailback()
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

	// Restart node to pick up disabled key
	if fm.nodeManager != nil {
		if err := fm.nodeManager.Restart(); err != nil {
			fm.logger.Error("Failed to restart node: %v", err)
		}
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
	defer func() {
		fm.mu.Lock()
		fm.failbackInProgress = false
		fm.mu.Unlock()
	}()

	fm.mu.RLock()
	isActive := fm.isActive
	fm.mu.RUnlock()

	if isActive {
		return
	}

	time.Sleep(time.Duration(fm.cfg.Failover.GracePeriod * float64(time.Second)))

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

	// Request key from peer (current active) before we take over
	if err := fm.requestKeyFromPeer(); err != nil {
		fm.logger.Error("Failed to get key from peer: %v", err)
		return
	}

	if err := fm.stateManager.AcquireLock(); err != nil {
		fm.logger.Error("Failed to acquire state lock: %v", err)
		return
	}

	if err := fm.syncStateFromPeer(); err != nil {
		fm.logger.Error("Failed to sync state from peer: %v", err)
		fm.stateManager.ReleaseLock()
		return
	}

	// Restart node to pick up the new key
	if fm.nodeManager != nil {
		if err := fm.nodeManager.Restart(); err != nil {
			fm.logger.Error("Failed to restart node: %v", err)
			fm.stateManager.ReleaseLock()
			return
		}
	}

	// Notify peer to release (they will swap their key to mock)
	fm.notifyPeerOfFailback()

	fm.isActive = true
	fm.failureCount = 0

	fm.logger.Info("Failback complete - node is now active")
}

// syncValidatorState periodically syncs validator state when passive
func (fm *FailoverManager) syncValidatorState() {
	ticker := time.NewTicker(time.Duration(fm.cfg.Failover.StateSyncInterval * float64(time.Second)))
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
