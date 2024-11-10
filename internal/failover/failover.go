package failover

import (
	"sync"
	"time"

	log "github.com/sirupsen/logrus"

	"github.com/aldebaranode/syncguard/internal/communication"
	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/health"
)

// FailoverManager manages the failover and fallback process for nodes
type FailoverManager struct {
	cfg           *config.Config
	healthChecker *health.HealthChecker
	commServer    *communication.Server
	commClient    *communication.Client
	isPrimary     bool // Tracks if the node is currently acting as primary
	mutex         sync.Mutex
}

// NewFailoverManager initializes a new FailoverManager
func NewFailoverManager(cfg *config.Config, healthChecker *health.HealthChecker, commServer *communication.Server, commClient *communication.Client) *FailoverManager {
	return &FailoverManager{
		cfg:           cfg,
		healthChecker: healthChecker,
		commServer:    commServer,
		isPrimary:     cfg.Server.Role == "primary",
	}
}

// Run starts the failover manager, monitoring for status changes
func (fm *FailoverManager) Run() error {
	go fm.healthChecker.Start()
	go fm.monitorHealthStatus()

	go fm.monitorFallbackSignals()

	select {}
}

// monitorHealthStatus listens for health status changes and triggers failover if needed
func (fm *FailoverManager) monitorHealthStatus() {
	for status := range fm.healthChecker.GetStatusChannel() {
		if fm.isPrimary && !status {
			log.Println("Primary node has become unhealthy. Initiating failover.")
			fm.initiateFailover()
		} else if !fm.isPrimary && status && fm.cfg.Server.Role == "primary" {
			log.Println("Primary node is back online. Initiating fallback to primary.")
			fm.initiateFallback()
		}
	}
}

// monitorFallbackSignals listens for fallback triggers from peer nodes
func (fm *FailoverManager) monitorFallbackSignals() {
	// for nodeID := range fm.commServer.fallbackSignal {
	// 	log.Printf("Received failover trigger from node %s", nodeID)
	// 	if !fm.isPrimary {
	// 		log.Printf("Promoting this node to primary in response to failover trigger.")
	// 		fm.promoteToPrimary()
	// 	}
	// }
}

// initiateFailover promotes a backup node to primary if the current primary is down
func (fm *FailoverManager) initiateFailover() {
	fm.mutex.Lock()
	defer fm.mutex.Unlock()

	if !fm.isPrimary {
		return
	}

	fm.notifyPeersOfRoleChange("backup")

	// Demote this node to backup
	fm.isPrimary = false
	log.Println("Node demoted to backup after failover.")
}

// initiateFallback promotes this node back to primary after it recovers from a failure
func (fm *FailoverManager) initiateFallback() {
	fm.mutex.Lock()
	defer fm.mutex.Unlock()

	if fm.isPrimary {
		return
	}

	time.Sleep(time.Duration(fm.cfg.Failover.FallbackGracePeriod) * time.Second)

	fm.notifyPeersOfRoleChange("primary")

	// Promote this node to primary
	fm.isPrimary = true
	log.Println("Node promoted back to primary after fallback.")
}

// promoteToPrimary promotes this backup node to primary
func (fm *FailoverManager) promoteToPrimary() {
	fm.mutex.Lock()
	defer fm.mutex.Unlock()

	if fm.isPrimary {
		return
	}

	fm.notifyPeersOfRoleChange("primary")

	// Promote this node to primary
	fm.isPrimary = true
	log.Println("Node promoted to primary as part of failover.")
}

// notifyPeersOfRoleChange notifies peer nodes about this node's role change
func (fm *FailoverManager) notifyPeersOfRoleChange(newRole string) {
	for _, peer := range fm.cfg.Communication.Peers {
		status := communication.HealthStatus{
			NodeID:  fm.cfg.Server.ID,
			Healthy: newRole == "primary",
		}
		err := fm.commClient.SendHealthUpdate(peer.Address, status)
		if err != nil {
			log.Printf("Failed to notify %s about role change to %s: %v", peer.ID, newRole, err)
		}
	}
}
