package manager

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/constants"
	"github.com/aldebaranode/syncguard/internal/crypto"
	"github.com/aldebaranode/syncguard/internal/health"
	"github.com/aldebaranode/syncguard/internal/logger"
	"github.com/aldebaranode/syncguard/internal/node"
	"github.com/aldebaranode/syncguard/internal/server"
	"github.com/aldebaranode/syncguard/internal/state"
)

// peerRequestTimeout bounds state sync and key fetch requests to the peer, so a
// peer that stops answering cannot block the caller.
const peerRequestTimeout = 10 * time.Second

// startupHealthTimeout is the ceiling on the wait for the node to become
// healthy after syncguard starts it.
const startupHealthTimeout = 60 * time.Second

// nodeHealthChecker is the part of health.Checker the failover manager uses.
// PerformHealthCheck queries the node; IsHealthy and GetLastHeight read the
// result of the last check.
type nodeHealthChecker interface {
	PerformHealthCheck() (*health.NodeHealth, error)
	IsHealthy() bool
	GetLastHeight() int64
}

// FailoverManager manages the failover process for validator nodes
type FailoverManager struct {
	cfg                *config.Config
	stateManager       *state.Manager
	keyManager         *state.KeyManager
	healthChecker      nodeHealthChecker
	nodeManager        node.Manager
	server             *server.Server
	isActive           bool
	isPrimarySite      bool
	failbackInProgress bool
	failureCount       int
	warmupUntil        time.Time
	mu                 sync.RWMutex
	logger             *logger.Logger
	stopCh             chan struct{}
}

// BeginWarmup suppresses failure-triggered failover for the configured warm-up
// window. It is called whenever this node is (re)started as active so its
// restart downtime is not counted as a failure, which would otherwise start a
// failover ping-pong where each takeover restart re-triggers a failover.
func (fm *FailoverManager) BeginWarmup() {
	fm.mu.Lock()
	defer fm.mu.Unlock()
	fm.startWarmupLocked()
	fm.logger.Info("Entering post-restart warm-up for %.0fs", fm.cfg.Failover.WarmupPeriod)
}

// startWarmupLocked opens the warm-up window and clears the failure count.
// The caller must already hold fm.mu.
func (fm *FailoverManager) startWarmupLocked() {
	warmup := time.Duration(fm.cfg.Failover.WarmupPeriod * float64(time.Second))
	fm.warmupUntil = time.Now().Add(warmup)
	fm.failureCount = 0
}

// claimInitialRole takes the state lock when this node starts as active, so
// the lock layer protects the initial active period and not only takeovers.
func (fm *FailoverManager) claimInitialRole() error {
	if !fm.isActive {
		return nil
	}
	if err := fm.stateManager.AcquireLock(); err != nil {
		return fmt.Errorf("failed to acquire state lock as initial active node: %w", err)
	}
	return nil
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

	keyLogger := logger.NewLogger(cfg)
	keyLogger.WithModule("key-state")

	fm := &FailoverManager{
		cfg:          cfg,
		stateManager: state.NewManager(cfg.CometBFT.StatePath, cfg.CometBFT.BackupPath),
		keyManager: state.NewKeyManager(
			cfg.CometBFT.KeyPath,
			cfg.CometBFT.BackupPath,
			cfg.CometBFT.ValidatorAddress,
			keyLogger,
		),
		healthChecker: health.NewChecker(cfg, cfg.CometBFT.RPCURL),
		isPrimarySite: cfg.Node.IsPrimary,
		isActive:      cfg.Node.Role == constants.NodeStatusActive,
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

	// Initialize key
	if err := fm.keyManager.InitializeKey(); err != nil {
		return fmt.Errorf("failed to initialize key: %w", err)
	}

	if err := fm.claimInitialRole(); err != nil {
		return err
	}

	// Start the validator node if wrapper is enabled
	if fm.nodeManager != nil {
		if err := fm.nodeManager.Start(); err != nil {
			return fmt.Errorf("failed to start validator node: %w", err)
		}
		fm.waitForNodeHealthy()
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

// waitForNodeHealthy blocks until the node reports healthy or the startup
// ceiling passes. Each poll runs a fresh health check: the cached result is
// only filled by the monitor loop, which has not started yet.
func (fm *FailoverManager) waitForNodeHealthy() {
	ctx, cancel := context.WithTimeout(context.Background(), startupHealthTimeout)
	defer cancel()
	if err := fm.nodeManager.WaitHealthy(ctx, fm.checkHealthNow); err != nil {
		fm.logger.Warn("Node not healthy after start: %v", err)
	}
}

// checkHealthNow runs a fresh health check and reports whether the node is
// healthy.
func (fm *FailoverManager) checkHealthNow() bool {
	if _, err := fm.healthChecker.PerformHealthCheck(); err != nil {
		return false
	}
	return fm.healthChecker.IsHealthy()
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
	role := constants.NodeStatusPassive
	if fm.isActive {
		role = constants.NodeStatusActive
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
	// During the post-restart warm-up the node is expected to be unreachable
	// while it boots; do not count those failures toward a failover.
	if time.Now().Before(fm.warmupUntil) {
		fm.failureCount = 0
		fm.mu.Unlock()
		fm.logger.Info("In post-restart warm-up, ignoring health-check failure")
		return
	}
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

// initiateFailback returns the validator role to this primary node. The order
// is break-before-make: the peer must stop signing, and its final signing
// state must be synced, before this node restarts with the validator key.
// Before the peer confirms its release, any failure disarms the local key. After
// the confirmation this node is the only holder of the validator key, so a
// failure keeps the key and the next attempt reuses it instead of fetching the
// peer's mock key.
func (fm *FailoverManager) initiateFailback() {
	fm.mu.Lock()
	defer fm.mu.Unlock()

	if fm.isActive {
		return
	}

	fm.logger.Info("Initiating failback to primary")

	// A previous attempt may already hold the validator key after the peer
	// released; otherwise fetch it while the peer still holds it.
	holdsKey, err := fm.keyManager.HoldsValidatorKey()
	if err != nil {
		fm.logger.Error("Failed to inspect local key: %v", err)
		return
	}
	if !holdsKey {
		if err := fm.requestKeyFromPeer(); err != nil {
			fm.logger.Error("Failed to get key from peer: %v", err)
			return
		}
	}

	// The peer swaps to a mock key and restarts before it answers.
	if err := fm.notifyPeerOfFailback(); err != nil {
		fm.logger.Error("Peer did not release validator duties, aborting failback: %v", err)
		fm.disarmLocalKey()
		return
	}

	if err := fm.stateManager.AcquireLock(); err != nil {
		fm.keepKeyAfterRelease(err)
		return
	}

	// Taken after the peer stopped signing, so it holds the peer's last vote.
	if err := fm.syncStateFromPeer(); err != nil {
		fm.stateManager.ReleaseLock()
		fm.keepKeyAfterRelease(err)
		return
	}

	if fm.nodeManager != nil {
		if err := fm.nodeManager.Restart(); err != nil {
			fm.stateManager.ReleaseLock()
			fm.keepKeyAfterRelease(err)
			return
		}
	}

	fm.isActive = true
	// The node just restarted to load the real key; keep its boot time from
	// counting as a failure (avoids the failover ping-pong).
	fm.startWarmupLocked()

	fm.logger.Info("Failback complete - node is now active")
}

// keepKeyAfterRelease logs a failure that happened after the peer released. The
// validator key stays on this node because the peer no longer holds it.
func (fm *FailoverManager) keepKeyAfterRelease(cause error) {
	fm.logger.Error("Peer released validator duties; keeping validator key for the next failback attempt: %v", cause)
}

// disarmLocalKey swaps the real key fetched for an aborted failback back to a
// mock key, so a later restart cannot bring this node up as a second signer.
func (fm *FailoverManager) disarmLocalKey() {
	if err := fm.keyManager.DeleteKey(); err != nil {
		fm.logger.Error("Failed to disarm local key after aborted failback: %v", err)
	}
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

	client := &http.Client{Timeout: peerRequestTimeout}
	resp, err := client.Get(url)
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

// notifyPeerOfFailback asks the peer to release validator duties and waits for
// its confirmation. The peer restarts its node before answering, so the
// timeout covers a full graceful stop.
func (fm *FailoverManager) notifyPeerOfFailback() error {
	if len(fm.cfg.Peers) == 0 {
		return fmt.Errorf("no peer configured")
	}

	url := fmt.Sprintf("http://%s/failback_notify", fm.cfg.Peers[0].Address)
	req, err := http.NewRequest(http.MethodPost, url, nil)
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	stopTimeout := time.Duration(fm.cfg.Validator.StopTimeout * float64(time.Second))
	client := &http.Client{Timeout: stopTimeout + 30*time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("failed to reach peer: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("peer answered status %d", resp.StatusCode)
	}
	return nil
}

// signKeyTransferRequest attaches a timestamped HMAC signature to a key-transfer
// request so the receiving peer can authenticate it and reject replays.
func (fm *FailoverManager) signKeyTransferRequest(req *http.Request) {
	timestamp := time.Now().Unix()
	signature := crypto.SignWithTimestamp(constants.AuthPayloadValidatorKey, fm.cfg.Secret, timestamp)
	req.Header.Set(constants.HeaderSignature, signature)
	req.Header.Set(constants.HeaderTimestamp, strconv.FormatInt(timestamp, 10))
}

// transferKeyToPeer sends the validator key to the peer node
func (fm *FailoverManager) transferKeyToPeer() error {
	if len(fm.cfg.Peers) == 0 {
		return fmt.Errorf("no peer configured")
	}

	keyData, err := fm.keyManager.EncryptKeyToBytes(fm.cfg.Secret)
	if err != nil {
		return fmt.Errorf("failed to encrypt key: %w", err)
	}

	peerAddr := fm.cfg.Peers[0].Address
	url := fmt.Sprintf("http://%s/validator_key", peerAddr)

	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(keyData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	fm.signKeyTransferRequest(req)

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

	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}
	fm.signKeyTransferRequest(req)

	client := &http.Client{Timeout: peerRequestTimeout}
	resp, err := client.Do(req)
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

	if err := fm.keyManager.DecryptKeyFromBytes(body, fm.cfg.Secret); err != nil {
		return fmt.Errorf("failed to decrypt key: %w", err)
	}

	fm.logger.Info("Successfully retrieved validator key from peer")
	return nil
}
