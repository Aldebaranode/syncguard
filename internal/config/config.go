package config

import (
	"fmt"
	"io"
	"os"

	log "github.com/sirupsen/logrus"
	"github.com/spf13/viper"
)

// Config holds all configuration settings
type Config struct {
	Node      NodeConfig      `mapstructure:"node"`
	Validator ValidatorConfig `mapstructure:"validator"`
	Peers     []PeerConfig    `mapstructure:"peers"`
	CometBFT  CometBFTConfig  `mapstructure:"cometbft"`
	Health    HealthConfig    `mapstructure:"health"`
	Failover  FailoverConfig  `mapstructure:"failover"`
	Logging   LoggingConfig   `mapstructure:"logging"`
}

// ValidatorConfig controls the managed validator node process
type ValidatorConfig struct {
	Enabled      bool     `mapstructure:"enabled"`       // Enable node management
	Mode         string   `mapstructure:"mode"`          // "binary", "docker", or "docker-compose"
	Binary       string   `mapstructure:"binary"`        // Path to validator binary (binary mode)
	Args         []string `mapstructure:"args"`          // Command line arguments (binary mode)
	Container    string   `mapstructure:"container"`     // Container name or ID (docker mode)
	ComposeFile  string   `mapstructure:"compose_file"`  // Docker compose file path (docker-compose mode)
	Service      string   `mapstructure:"service"`       // Service name to restart (docker-compose mode)
	StopTimeout  float64  `mapstructure:"stop_timeout"`  // Seconds to wait for graceful stop
	RestartDelay float64  `mapstructure:"restart_delay"` // Seconds to wait before restart
}

// NodeConfig identifies this node
type NodeConfig struct {
	ID        string `mapstructure:"id"`
	Role      string `mapstructure:"role"`       // "active" or "passive"
	IsPrimary bool   `mapstructure:"is_primary"` // Primary site for failback priority
	Port      int    `mapstructure:"port"`
}

// PeerConfig defines a peer node
type PeerConfig struct {
	ID      string `mapstructure:"id"`
	Address string `mapstructure:"address"`
}

// CometBFTConfig holds CometBFT consensus layer settings
type CometBFTConfig struct {
	RPCURL     string `mapstructure:"rpc_url"`     // CometBFT RPC endpoint
	KeyPath    string `mapstructure:"key_path"`    // priv_validator_key.json path
	StatePath  string `mapstructure:"state_path"`  // priv_validator_state.json path
	BackupPath string `mapstructure:"backup_path"` // Backup location
}

// HealthConfig controls health checking behavior
type HealthConfig struct {
	Interval float64 `mapstructure:"interval"`  // Check interval in seconds
	MinPeers int     `mapstructure:"min_peers"` // Minimum peers to be healthy
	Timeout  float64 `mapstructure:"timeout"`   // HTTP timeout in seconds
}

// FailoverConfig controls failover behavior
type FailoverConfig struct {
	RetryAttempts     int     `mapstructure:"retry_attempts"`      // Retries before failover
	GracePeriod       float64 `mapstructure:"grace_period"`        // Failback wait time (seconds)
	StateSyncInterval float64 `mapstructure:"state_sync_interval"` // State sync interval (seconds)
}

// LoggingConfig controls logging behavior
type LoggingConfig struct {
	Level   string `mapstructure:"level"`
	File    string `mapstructure:"file"`
	Verbose bool   `mapstructure:"verbose"`
}

// Load reads and parses the configuration file
func Load(path string) (*Config, error) {
	viper.SetConfigFile(path)

	// Enable environment variable overrides (SYNCGUARD_NODE_ID, etc.)
	viper.SetEnvPrefix("SYNCGUARD")
	viper.AutomaticEnv()

	if err := viper.ReadInConfig(); err != nil {
		return nil, fmt.Errorf("failed to read config file: %w", err)
	}

	var cfg Config
	if err := viper.Unmarshal(&cfg); err != nil {
		return nil, fmt.Errorf("failed to unmarshal config: %w", err)
	}

	setDefaults(&cfg)

	if err := validate(&cfg); err != nil {
		return nil, fmt.Errorf("config validation error: %w", err)
	}

	initLogger(&cfg)

	return &cfg, nil
}

// setDefaults applies default values for missing fields
func setDefaults(cfg *Config) {
	if cfg.Node.Role == "" {
		cfg.Node.Role = "passive"
	}
	if cfg.Node.Port == 0 {
		cfg.Node.Port = 8080
	}
	if cfg.Health.Interval == 0 {
		cfg.Health.Interval = 5
	}
	if cfg.Health.MinPeers == 0 {
		cfg.Health.MinPeers = 1
	}
	if cfg.Health.Timeout == 0 {
		cfg.Health.Timeout = 5
	}
	if cfg.Failover.RetryAttempts == 0 {
		cfg.Failover.RetryAttempts = 3
	}
	if cfg.Failover.GracePeriod == 0 {
		cfg.Failover.GracePeriod = 60
	}
	if cfg.Failover.StateSyncInterval == 0 {
		cfg.Failover.StateSyncInterval = 5
	}
	if cfg.Logging.Level == "" {
		cfg.Logging.Level = "info"
	}
	if cfg.Logging.File == "" {
		cfg.Logging.File = "syncguard.log"
	}
	// Validator defaults
	if cfg.Validator.StopTimeout == 0 {
		cfg.Validator.StopTimeout = 30
	}
	if cfg.Validator.RestartDelay == 0 {
		cfg.Validator.RestartDelay = 2
	}
}

// validate checks required fields and valid values
func validate(cfg *Config) error {
	if cfg.Node.ID == "" {
		return fmt.Errorf("node.id is required")
	}
	if cfg.Node.Role != "active" && cfg.Node.Role != "passive" {
		return fmt.Errorf("node.role must be 'active' or 'passive'")
	}
	if cfg.CometBFT.RPCURL == "" {
		return fmt.Errorf("cometbft.rpc_url is required")
	}
	if cfg.CometBFT.StatePath == "" {
		return fmt.Errorf("cometbft.state_path is required")
	}
	// Validator config validation
	if cfg.Validator.Enabled {
		switch cfg.Validator.Mode {
		case "binary":
			if cfg.Validator.Binary == "" {
				return fmt.Errorf("validator.binary is required when mode is 'binary'")
			}
		case "docker":
			if cfg.Validator.Container == "" {
				return fmt.Errorf("validator.container is required when mode is 'docker'")
			}
		case "docker-compose":
			if cfg.Validator.ComposeFile == "" {
				return fmt.Errorf("validator.compose_file is required when mode is 'docker-compose'")
			}
			if cfg.Validator.Service == "" {
				return fmt.Errorf("validator.service is required when mode is 'docker-compose'")
			}
		default:
			return fmt.Errorf("validator.mode must be 'binary', 'docker', or 'docker-compose'")
		}
	}
	return nil
}

// initLogger configures the global logger
func initLogger(cfg *Config) {
	switch cfg.Logging.Level {
	case "debug":
		log.SetLevel(log.DebugLevel)
	case "info":
		log.SetLevel(log.InfoLevel)
	case "warn":
		log.SetLevel(log.WarnLevel)
	case "error":
		log.SetLevel(log.ErrorLevel)
	default:
		log.SetLevel(log.InfoLevel)
	}

	file, err := os.OpenFile(cfg.Logging.File, os.O_RDWR|os.O_CREATE|os.O_APPEND, 0666)
	if err != nil {
		log.Warnf("Failed to open log file %s: %v, using stdout only", cfg.Logging.File, err)
		return
	}

	log.SetOutput(io.MultiWriter(file, os.Stdout))
	log.SetFormatter(&log.TextFormatter{
		FullTimestamp:   true,
		TimestampFormat: "2006-01-02 15:04:05",
	})
}

// IsActive returns true if this node should be signing
func (c *Config) IsActive() bool {
	return c.Node.Role == "active"
}

// GetPeerAddress returns the first peer's address
func (c *Config) GetPeerAddress() string {
	if len(c.Peers) > 0 {
		return c.Peers[0].Address
	}
	return ""
}
