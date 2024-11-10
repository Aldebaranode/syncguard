package health

import (
	"fmt"
	"net/http"
	"sync"
	"time"

	log "github.com/sirupsen/logrus"

	"github.com/aldebaranode/syncguard/internal/communication"
	"github.com/aldebaranode/syncguard/internal/config"
)

// HealthChecker is responsible for checking the health of the local node
type HealthChecker struct {
	cfg           *config.Config
	isHealthy     bool
	statusMutex   sync.Mutex
	statusChannel chan bool             // Channel to notify status changes
	commClient    *communication.Client // To report health updates to peers
}

// NewHealthChecker initializes a new HealthChecker
func NewHealthChecker(cfg *config.Config, commClient *communication.Client) *HealthChecker {
	return &HealthChecker{
		cfg:           cfg,
		isHealthy:     true, // Assume node is healthy at startup
		statusChannel: make(chan bool),
		commClient:    commClient,
	}
}

// Start begins the periodic health check process
func (hc *HealthChecker) Start() {
	ticker := time.NewTicker(time.Duration(hc.cfg.Failover.HealthCheckInterval) * time.Second)
	defer ticker.Stop()

	for range ticker.C {
		hc.checkHealth()
	}
}

// checkHealth performs a health check on the local node
func (hc *HealthChecker) checkHealth() {
	var healthy bool
	var err error

	switch hc.cfg.Health.CheckType {
	case "http":
		healthy, err = hc.checkHTTPHealth()
	case "tcp":
		healthy, err = hc.checkTCPHealth()
	default:
		log.Printf("Unknown health check type: %s", hc.cfg.Health.CheckType)
		return
	}

	if err != nil {
		log.Printf("Health check error: %v", err)
		healthy = false
	}

	hc.updateHealthStatus(healthy)
}

// checkHTTPHealth performs a health check via HTTP
func (hc *HealthChecker) checkHTTPHealth() (bool, error) {
	url := fmt.Sprintf("http://%s:%d%s", hc.cfg.Health.NodeAddress, hc.cfg.Health.NodePort, hc.cfg.Health.CheckEndpoint)
	resp, err := http.Get(url)
	if err != nil || resp.StatusCode != http.StatusOK {
		return false, fmt.Errorf("failed HTTP health check: %w", err)
	}
	defer resp.Body.Close()
	return true, nil
}

// checkTCPHealth performs a health check via TCP (not implemented here, but it would use a TCP connection)
func (hc *HealthChecker) checkTCPHealth() (bool, error) {
	// Implement TCP health check if needed (using net.DialTimeout to check connection)
	return false, fmt.Errorf("TCP health check not implemented")
}

// updateHealthStatus updates the health status and notifies other nodes if status has changed
func (hc *HealthChecker) updateHealthStatus(healthy bool) {
	hc.statusMutex.Lock()
	defer hc.statusMutex.Unlock()

	if hc.isHealthy != healthy {
		hc.isHealthy = healthy
		hc.statusChannel <- healthy

		if healthy {
			log.Printf("Node is now healthy")
		} else {
			log.Printf("Node is now unhealthy")
		}

		// Notify peers of the new health status
		hc.notifyPeers(healthy)
	}
}

// notifyPeers sends the updated health status to peer nodes
func (hc *HealthChecker) notifyPeers(healthy bool) {
	for _, peer := range hc.cfg.Communication.Peers {
		status := communication.HealthStatus{
			NodeID:  hc.cfg.Server.ID,
			Healthy: healthy,
		}
		err := hc.commClient.SendHealthUpdate(peer.Address, status)
		if err != nil {
			log.Printf("Failed to send health update to %s: %v", peer.ID, err)
		}
	}
}

// GetStatusChannel returns the channel to listen for health status changes
func (hc *HealthChecker) GetStatusChannel() <-chan bool {
	return hc.statusChannel
}

// IsHealthy returns the current health status of the node
func (hc *HealthChecker) IsHealthy() bool {
	hc.statusMutex.Lock()
	defer hc.statusMutex.Unlock()
	return hc.isHealthy
}