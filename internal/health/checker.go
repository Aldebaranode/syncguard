package health

import (
	"encoding/json"
	"fmt"
	"io/ioutil"
	"net/http"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/logger"
)

// NodeHealth represents the health status of a CometBFT node
type NodeHealth struct {
	Healthy      bool
	IsSyncing    bool
	LatestHeight int64
	PeerCount    int
	LastCheck    time.Time
}

// CometBFTStatus represents the response from CometBFT status endpoint
type CometBFTStatus struct {
	Result struct {
		SyncInfo struct {
			LatestBlockHeight string `json:"latest_block_height"`
			CatchingUp        bool   `json:"catching_up"`
		} `json:"sync_info"`
		NodeInfo struct {
			Network string `json:"network"`
			Version string `json:"version"`
		} `json:"node_info"`
	} `json:"result"`
}

// Checker checks the health of CometBFT nodes
type Checker struct {
	cfg         *config.Config
	cometRPCURL string
	client      *http.Client
	logger      *logger.Logger
	lastHealth  *NodeHealth
}

// NewChecker creates a new health checker
func NewChecker(cfg *config.Config, cometRPCURL string) *Checker {
	newLogger := logger.NewLogger(cfg)
	newLogger.WithModule("health")

	return &Checker{
		cfg:         cfg,
		cometRPCURL: cometRPCURL,
		client: &http.Client{
			Timeout: time.Duration(cfg.Health.Timeout) * time.Second,
		},
		logger: newLogger,
	}
}

// CheckStatus checks the CometBFT status endpoint
func (c *Checker) CheckStatus() (bool, int64, bool, error) {
	url := fmt.Sprintf("%s/status", c.cometRPCURL)

	resp, err := c.client.Get(url)
	if err != nil {
		return false, 0, false, fmt.Errorf("failed to query CometBFT: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return false, 0, false, fmt.Errorf("CometBFT returned status %d", resp.StatusCode)
	}

	body, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return false, 0, false, fmt.Errorf("failed to read response: %w", err)
	}

	var status CometBFTStatus
	if err := json.Unmarshal(body, &status); err != nil {
		return false, 0, false, fmt.Errorf("failed to parse status: %w", err)
	}

	var height int64
	fmt.Sscanf(status.Result.SyncInfo.LatestBlockHeight, "%d", &height)

	healthy := !status.Result.SyncInfo.CatchingUp

	return healthy, height, status.Result.SyncInfo.CatchingUp, nil
}

// CheckPeerCount checks the number of connected peers
func (c *Checker) CheckPeerCount() (int, error) {
	url := fmt.Sprintf("%s/net_info", c.cometRPCURL)

	resp, err := c.client.Get(url)
	if err != nil {
		return 0, fmt.Errorf("failed to query net_info: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("net_info returned status %d", resp.StatusCode)
	}

	body, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return 0, fmt.Errorf("failed to read response: %w", err)
	}

	var netInfo struct {
		Result struct {
			NPeers string `json:"n_peers"`
		} `json:"result"`
	}

	if err := json.Unmarshal(body, &netInfo); err != nil {
		return 0, fmt.Errorf("failed to parse net_info: %w", err)
	}

	var peers int
	fmt.Sscanf(netInfo.Result.NPeers, "%d", &peers)

	return peers, nil
}

// PerformHealthCheck performs a complete health check
func (c *Checker) PerformHealthCheck() (*NodeHealth, error) {
	nodeHealth := &NodeHealth{
		LastCheck: time.Now(),
	}

	// Check CometBFT status
	healthy, height, isSyncing, err := c.CheckStatus()
	if err != nil {
		c.logger.Error("CometBFT health check failed: %v", err)
		nodeHealth.Healthy = false
	} else {
		nodeHealth.Healthy = healthy
		nodeHealth.LatestHeight = height
		nodeHealth.IsSyncing = isSyncing
	}

	// Check peer count
	peers, err := c.CheckPeerCount()
	if err != nil {
		c.logger.Warn("Failed to get peer count: %v", err)
	} else {
		nodeHealth.PeerCount = peers
	}

	if c.cfg.Logging.Verbose {
		c.logger.Info("Health check - Healthy: %v, Syncing: %v, Height: %d, Peers: %d",
			nodeHealth.Healthy, nodeHealth.IsSyncing, nodeHealth.LatestHeight, nodeHealth.PeerCount)
	}

	c.lastHealth = nodeHealth
	return nodeHealth, nil
}

// IsHealthy returns true if the node is healthy and ready to sign
func (c *Checker) IsHealthy() bool {
	if c.lastHealth == nil {
		return false
	}

	minPeers := c.cfg.Health.MinPeers
	if minPeers == 0 {
		minPeers = 1
	}

	return c.lastHealth.Healthy &&
		!c.lastHealth.IsSyncing &&
		c.lastHealth.PeerCount >= minPeers
}

// GetLastHeight returns the last known block height
func (c *Checker) GetLastHeight() int64 {
	if c.lastHealth == nil {
		return 0
	}
	return c.lastHealth.LatestHeight
}
