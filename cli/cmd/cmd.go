package cmd

import (
	"os"
	"os/signal"
	"syscall"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/manager"
	log "github.com/sirupsen/logrus"
	"github.com/spf13/cobra"
)

var rootCmd = &cobra.Command{
	Use:   "syncguard",
	Short: "SyncGuard - Validator failover for CometBFT networks",
	Long: `SyncGuard is a high-availability failover system for CometBFT-based
validator nodes. It monitors node health and automatically handles
failover between active and passive validators while preventing double-signing.`,
	Run: runRootCommand,
}

var options struct {
	configFile string
	role       string
}

func init() {
	rootCmd.Flags().StringVarP(&options.configFile, "config", "c", "config.yaml",
		"Configuration file path")
	rootCmd.Flags().StringVarP(&options.role, "role", "r", "",
		"Override node role (active/passive)")
}

// Execute runs the root command
func Execute() {
	if err := rootCmd.Execute(); err != nil {
		log.Fatal(err)
	}
}

func runRootCommand(cmd *cobra.Command, args []string) {
	cfg, err := config.Load(options.configFile)
	if err != nil {
		log.Fatalf("Error loading config: %v", err)
	}

	// Override role if specified via CLI flag
	if options.role != "" {
		if options.role != "active" && options.role != "passive" {
			log.Fatal("Role must be 'active' or 'passive'")
		}
		cfg.Node.Role = options.role
	}

	// Initialize failover manager
	failoverManager := manager.NewFailoverManager(cfg)

	if err := failoverManager.Start(); err != nil {
		log.Fatalf("Failed to start failover manager: %v", err)
	}

	log.Info("SyncGuard failover manager started")
	log.Infof("Node: %s, Role: %s, Primary: %v", cfg.Node.ID, cfg.Node.Role, cfg.Node.IsPrimary)

	waitForShutdown(failoverManager)
}

func waitForShutdown(mgr *manager.FailoverManager) {
	signalChan := make(chan os.Signal, 1)
	signal.Notify(signalChan, syscall.SIGINT, syscall.SIGTERM)

	sig := <-signalChan
	log.Infof("Received signal %s. Shutting down...", sig)

	mgr.Stop()

	log.Info("SyncGuard stopped")
}
