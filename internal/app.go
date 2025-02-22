package app

import (
	"log"
	"time"

	"github.com/aldebaranode/syncguard/internal/communication"
	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/failover"
	"github.com/aldebaranode/syncguard/internal/health"
)

func RunApp(config *config.Config) {
	// Start the inter-node communication server
	commServer := communication.NewServer(config)
	go func() {
		if err := commServer.Start(); err != nil {
			log.Fatalf("Communication server error: %v", err)
		}
	}()

	commClient := communication.NewClient(config, time.Minute*3)

	// Initialize the health checker
	healthChecker := health.NewHealthChecker(config, commClient)

	// Initialize the failover manager
	failoverManager := failover.NewFailoverManager(config, healthChecker, commServer, commClient)

	// Start the failover manager
	failoverManager.Run()
}

func RunService() {

}
