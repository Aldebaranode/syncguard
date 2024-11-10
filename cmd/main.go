package main

import (
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/aldebaranode/syncguard/internal/communication"
	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/failover"
	"github.com/aldebaranode/syncguard/internal/health"
	log "github.com/sirupsen/logrus"
)

func main() {
	// Load the configuration from config.yaml
	cfg, err := config.Load("config.yaml")
	if err != nil {
		log.Fatalf("Error loading config: %v", err)
	}

	// Start the inter-node communication server
	commServer := communication.NewServer(cfg)
	go func() {
		if err := commServer.Start(); err != nil {
			log.Fatalf("Communication server error: %v", err)
		}
	}()

	commClient := communication.NewClient(time.Minute * 3)

	// Initialize the health checker
	healthChecker := health.NewHealthChecker(cfg, commClient)

	// Initialize the failover manager
	failoverManager := failover.NewFailoverManager(cfg, healthChecker, commServer, commClient)

	// Start the failover manager
	go failoverManager.Run()

	waitForShutdown()
}

// waitForShutdown waits for an OS signal to gracefully shut down the application.
func waitForShutdown() {
	signalChan := make(chan os.Signal, 1)
	signal.Notify(signalChan, syscall.SIGINT, syscall.SIGTERM)

	sig := <-signalChan
	log.Infof("Received signal %s. Shutting down...\n", sig)
}
