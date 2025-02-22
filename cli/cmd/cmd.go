package cmd

import (
	"fmt"
	"os"
	"os/signal"
	"syscall"

	app "github.com/aldebaranode/syncguard/internal"
	"github.com/aldebaranode/syncguard/internal/config"
	log "github.com/sirupsen/logrus"
	"github.com/spf13/cobra"
)

var rootCmd = &cobra.Command{
	Use:   "syncguard",
	Short: "Maintaining harmony between nodes during transitions and failover.",
	Long: `Syncguard is a robust command-line utility designed to ensure high availability and operational continuity in distributed systems. It helps administrators manage transitions between nodes and automate failover processes during planned maintenance or unexpected failures.

For additional details on configuration and advanced usage scenarios, please refer to the official documentation or run 'syncguard --help'.`,
	Run: runCobraCommand,
}

type FlagOptions struct {
	configFile string
}

var cmdOptions = FlagOptions{
	configFile: "config.yaml",
}

func bindFlags() {
	rootCmd.Flags().StringVarP(&cmdOptions.configFile, "config", "c", "config.yaml", "Provide yaml config path")
}

func Execute() {
	bindFlags()
	if err := rootCmd.Execute(); err != nil {
		fmt.Println(err)
		os.Exit(1)
	}
}

func runCobraCommand(cmd *cobra.Command, args []string) {
	// Load the configuration from config.yaml
	cfg, err := config.Load(cmdOptions.configFile)
	if err != nil {
		log.Fatalf("Error loading config: %v", err)
	}

	go app.RunApp(cfg)
	go app.RunService()

	waitForShutdown()
}

// waitForShutdown waits for an OS signal to gracefully shut down the application.
func waitForShutdown() {
	signalChan := make(chan os.Signal, 1)
	signal.Notify(signalChan, syscall.SIGINT, syscall.SIGTERM)

	sig := <-signalChan
	log.Infof("Received signal %s. Shutting down...\n", sig)
}
