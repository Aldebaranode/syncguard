package manager

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
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

// FailoverManager manages the failover process for validator nodes.
//
// Locking: transitionMu serializes every role transition (initiateFailover,
// initiateFailback, TakeOver, ReleaseForFailback) for its whole duration. mu
// guards isActive, failbackInProgress, failureCount and warmupUntil and is held
// only around reads and writes of those fields, never across a peer request or
// a node restart. Lock order: transitionMu, then mu.
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
	// nodeMayRunValidatorKey records that a takeover restart failed and the
	// node could not be confirmed stopped, so the running process may hold the
	// validator key although the key file is disarmed. Guarded by transitionMu.
	nodeMayRunValidatorKey bool
	transitionMu           sync.Mutex
	mu                     sync.RWMutex
	logger                 *logger.Logger
	stopCh                 chan struct{}
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

// endWarmup closes the warm-up window, so failures count again at once.
func (fm *FailoverManager) endWarmup() {
	fm.mu.Lock()
	defer fm.mu.Unlock()
	fm.warmupUntil = time.Time{}
}

// claimInitialRole takes the state lock when this node starts as active, so
// the lock layer protects the initial active period and not only takeovers.
func (fm *FailoverManager) claimInitialRole() error {
	if !fm.IsActive() {
		return nil
	}
	if err := fm.stateManager.AcquireLock(); err != nil {
		return fmt.Errorf("failed to acquire state lock as initial active node: %w", err)
	}
	return nil
}

// verifyRoleMatchesKey refuses a start whose configured role contradicts the
// key at keyPath: an active node must hold the validator key, and a passive
// node must not, or it would sign as soon as its node restarts.
func (fm *FailoverManager) verifyRoleMatchesKey() error {
	holdsKey, err := fm.keyManager.HoldsValidatorKey()
	if err != nil {
		return fmt.Errorf("failed to inspect key at start: %w", err)
	}
	keyPath := fm.cfg.CometBFT.KeyPath
	address := fm.cfg.CometBFT.ValidatorAddress
	if fm.IsActive() && !holdsKey {
		return fmt.Errorf("configured as active but %s does not hold validator key %s", keyPath, address)
	}
	if !fm.IsActive() && holdsKey {
		return fmt.Errorf("configured as passive but %s holds validator key %s", keyPath, address)
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

// setPassive marks this node passive and clears its failure count.
func (fm *FailoverManager) setPassive() {
	fm.mu.Lock()
	defer fm.mu.Unlock()
	fm.isActive = false
	fm.failureCount = 0
}

// releaseLock releases the state lock and logs a failure to do so.
func (fm *FailoverManager) releaseLock() {
	if err := fm.stateManager.ReleaseLock(); err != nil {
		fm.logger.Error("Failed to release state lock: %v", err)
	}
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
		fm.isPrimarySite, fm.IsActive())

	// Initialize key
	if err := fm.keyManager.InitializeKey(); err != nil {
		return fmt.Errorf("failed to initialize key: %w", err)
	}

	if err := fm.verifyRoleMatchesKey(); err != nil {
		return err
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
	if !fm.IsActive() {
		go fm.syncValidatorState()
	}

	// Create and start peer communication server
	fm.server = server.NewServer(fm.cfg, fm.stateManager, fm.keyManager, fm.healthChecker, fm, fm)
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
	if fm.IsActive() {
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
	// A passive primary considers failback; only one attempt runs at a time.
	fm.mu.Lock()
	fm.failureCount = 0
	startFailback := fm.isPrimarySite && !fm.isActive && !fm.failbackInProgress
	if startFailback {
		fm.failbackInProgress = true
	}
	fm.mu.Unlock()

	if startFailback {
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
		if fm.IsActive() {
			fm.logger.Error("Maximum failures reached, initiating failover")
			fm.initiateFailover()
		}
	}
}

// initiateFailover hands the validator role to the peer and fails closed: the
// peer is told to take over only after it holds the key, the local key file is
// disarmed, and the local node restarted onto the mock key or is confirmed
// stopped. Any earlier failure leaves this node active.
func (fm *FailoverManager) initiateFailover() {
	fm.transitionMu.Lock()
	defer fm.transitionMu.Unlock()

	if !fm.IsActive() {
		return
	}

	fm.logger.Info("Initiating failover - releasing validator duties")

	// Without the key the peer cannot take over; going passive would only
	// take the validator offline.
	if err := fm.transferKeyToPeer(); err != nil {
		fm.logger.Error("Aborting failover, staying active: key transfer to peer failed: %v", err)
		return
	}

	// The key staged on the peer stays inert until it is told to take over.
	if err := fm.keyManager.DeleteKey(); err != nil {
		fm.logger.Error("Aborting failover, staying active: failed to disarm local key: %v", err)
		return
	}

	if fm.nodeManager != nil {
		if err := fm.nodeManager.Restart(); err != nil {
			fm.logger.Error("Failed to restart node onto the mock key: %v", err)
			if !fm.stopNodeConfirmed() {
				fm.logger.Error("Aborting failover, staying active: node may still be signing with the validator key")
				// The key file goes back to match the process that may still
				// run with it and the active role this node keeps.
				if restoreErr := fm.keyManager.RestoreKey(); restoreErr != nil {
					fm.logger.Error("Failed to restore key after aborted failover: %v", restoreErr)
				}
				return
			}
			fm.logger.Warn("Node stopped after the failed restart; handing over to the peer")
		}
	}

	fm.releaseLock()

	if err := fm.notifyPeerOfFailover(); err != nil {
		fm.logger.Error("Peer did not take over; validator offline until failback: %v", err)
	}

	fm.setPassive()
	fm.logger.Info("Failover complete - node is now passive")
}

// stopNodeConfirmed stops the node after a failed restart and reports whether
// it is confirmed stopped: Stop succeeded and the node no longer runs.
func (fm *FailoverManager) stopNodeConfirmed() bool {
	if err := fm.nodeManager.Stop(); err != nil {
		fm.logger.Error("Failed to stop node: %v", err)
		return false
	}
	return !fm.nodeManager.IsRunning()
}

// holdsValidatorKeyToArm reports whether the key file or the staged file holds
// the validator key, so a takeover or failback can arm it.
func (fm *FailoverManager) holdsValidatorKeyToArm() (bool, error) {
	armed, err := fm.keyManager.HoldsValidatorKey()
	if err != nil {
		return false, err
	}
	if armed {
		return true, nil
	}
	return fm.keyManager.HoldsStagedValidatorKey()
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
// state must be synced, before this node restarts with the validator key. A
// fetched key is only staged; the key file is armed immediately before the
// restart, so an abort before the peer's release leaves nothing to disarm.
// After the release this node is the only holder of the validator key, so a
// failure keeps the key (staged or armed) and the next attempt reuses it
// instead of fetching again. When the peer has no key to serve, the local .real
// stash is staged, but only after the peer confirmed its release.
func (fm *FailoverManager) initiateFailback() {
	fm.transitionMu.Lock()
	defer fm.transitionMu.Unlock()

	if fm.IsActive() {
		return
	}

	fm.logger.Info("Initiating failback to primary")

	keyReady, err := fm.holdsValidatorKeyToArm()
	if err != nil {
		fm.logger.Error("Failed to inspect local key: %v", err)
		return
	}
	stageRealAfterRelease := false
	if !keyReady {
		if fetchErr := fm.requestKeyFromPeer(); fetchErr != nil {
			holdsReal, realErr := fm.keyManager.HoldsRealValidatorKey()
			if realErr != nil || !holdsReal {
				fm.logger.Error("Failed to get key from peer: %v", fetchErr)
				return
			}
			fm.logger.Warn("Failed to get key from peer, will use the local .real copy after the peer releases: %v", fetchErr)
			stageRealAfterRelease = true
		}
	}

	// The peer swaps to a mock key and restarts before it answers.
	if err := fm.notifyPeerOfFailback(); err != nil {
		fm.logger.Error("Peer did not release validator duties, aborting failback: %v", err)
		return
	}

	if stageRealAfterRelease {
		if err := fm.keyManager.StageFromRealKey(); err != nil {
			fm.keepKeyAfterRelease(err)
			return
		}
	}

	if err := fm.stateManager.AcquireLock(); err != nil {
		fm.keepKeyAfterRelease(err)
		return
	}

	// Taken after the peer stopped signing, so it holds the peer's last vote.
	if err := fm.syncStateFromPeer(); err != nil {
		fm.releaseLock()
		fm.keepKeyAfterRelease(err)
		return
	}

	if err := fm.keyManager.PromoteStagedKey(); err != nil {
		fm.releaseLock()
		fm.keepKeyAfterRelease(err)
		return
	}

	if fm.nodeManager != nil {
		if err := fm.nodeManager.Restart(); err != nil {
			fm.releaseLock()
			fm.keepKeyAfterRelease(err)
			return
		}
	}

	fm.mu.Lock()
	fm.isActive = true
	// The node just restarted to load the real key; keep its boot time from
	// counting as a failure (avoids the failover ping-pong).
	fm.startWarmupLocked()
	fm.mu.Unlock()

	fm.logger.Info("Failback complete - node is now active")
}

// keepKeyAfterRelease logs a failure that happened after the peer released. The
// validator key stays on this node because the peer no longer holds it.
func (fm *FailoverManager) keepKeyAfterRelease(cause error) {
	fm.logger.Error("Peer released validator duties; keeping validator key for the next failback attempt: %v", cause)
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

// notifyPeerOfFailover asks the peer to take over and waits for its answer.
func (fm *FailoverManager) notifyPeerOfFailover() error {
	return fm.notifyPeer("/failover_notify", constants.AuthPayloadFailoverNotify)
}

// notifyPeerOfFailback asks the peer to release validator duties and waits for
// its confirmation.
func (fm *FailoverManager) notifyPeerOfFailback() error {
	return fm.notifyPeer("/failback_notify", constants.AuthPayloadFailbackNotify)
}

// notifyPeer posts a role-transition request, signed over payload, to path on
// the peer. The peer
// restarts its node before it answers, so the timeout covers a full graceful
// stop. A transport failure or a non-2xx answer is an error that carries the
// peer's reason code.
func (fm *FailoverManager) notifyPeer(path, payload string) error {
	if len(fm.cfg.Peers) == 0 {
		return fmt.Errorf("no peer configured")
	}

	url := fmt.Sprintf("http://%s%s", fm.cfg.Peers[0].Address, path)
	req, err := http.NewRequest(http.MethodPost, url, nil)
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}
	fm.signPeerRequest(req, payload)

	stopTimeout := time.Duration(fm.cfg.Validator.StopTimeout * float64(time.Second))
	client := &http.Client{Timeout: stopTimeout + 30*time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("failed to reach peer: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		reason, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
		return fmt.Errorf("peer answered status %d: %s", resp.StatusCode, strings.TrimSpace(string(reason)))
	}
	return nil
}

// signPeerRequest attaches a timestamped HMAC signature over payload to a peer
// request so the receiving endpoint can authenticate it and reject replays.
func (fm *FailoverManager) signPeerRequest(req *http.Request, payload string) {
	timestamp := time.Now().Unix()
	signature := crypto.SignWithTimestamp(payload, fm.cfg.Secret, timestamp)
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
	fm.signPeerRequest(req, constants.AuthPayloadValidatorKey)

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
	fm.signPeerRequest(req, constants.AuthPayloadValidatorKey)

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

	if err := fm.keyManager.StageKeyFromBytes(body, fm.cfg.Secret); err != nil {
		return fmt.Errorf("failed to stage key: %w", err)
	}

	fm.logger.Info("Successfully retrieved validator key from peer")
	return nil
}
