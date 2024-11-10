package config

import (
	"fmt"
	"io"
	"os"

	log "github.com/sirupsen/logrus"
	"gopkg.in/yaml.v2"
)

// Config struct holds all configuration settings with default values
type Config struct {
	Server        ServerConfig        `yaml:"server"`
	Failover      FailoverConfig      `yaml:"failover"`
	Communication CommunicationConfig `yaml:"communication"`
	Health        HealthConfig        `yaml:"health"`
	Logging       LoggingConfig       `yaml:"logging"`
	Database      DatabaseConfig      `yaml:"database"`
}

type ServerConfig struct {
	ID   string `yaml:"id"`
	Role string `yaml:"role"`
	Port int    `yaml:"port"`
}

type FailoverConfig struct {
	HealthCheckInterval int `yaml:"health_check_interval"`
	FailoverTimeout     int `yaml:"failover_timeout"`
	RetryAttempts       int `yaml:"retry_attempts"`
	FallbackGracePeriod int `yaml:"fallback_grace_period"`
}

type CommunicationConfig struct {
	Peers    []PeerConfig `yaml:"peers"`
	Protocol string       `yaml:"protocol"`
	Timeout  int          `yaml:"timeout"`
}

type PeerConfig struct {
	ID      string `yaml:"id"`
	Address string `yaml:"address"`
}

type HealthConfig struct {
	CheckEndpoint string `yaml:"check_endpoint"`
	CheckType     string `yaml:"check_type"`
	NodeAddress   string `yaml:"node_address"`
	NodePort      int    `yaml:"node_port"`
}

type LoggingConfig struct {
	Level        string `yaml:"level"`
	LogFile      string `yaml:"log_file"`
	EnableAlerts bool   `yaml:"enable_alerts"`
}

type DatabaseConfig struct {
	Type             string `yaml:"type"`
	ConnectionString string `yaml:"connection_string"`
}

// Load reads and parses the YAML configuration file, setting default values if needed.
func Load(path string) (*Config, error) {
	// Read file content
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("failed to read config file: %w", err)
	}

	// Unmarshal YAML data into the Config struct
	var cfg Config
	err = yaml.Unmarshal(data, &cfg)
	if err != nil {
		return nil, fmt.Errorf("failed to unmarshal yaml: %w", err)
	}

	// Set default values
	SetDefaults(&cfg)

	// Validate configuration values
	err = validateConfig(&cfg)
	if err != nil {
		return nil, fmt.Errorf("config validation error: %w", err)
	}

	return &cfg, nil
}

// setDefaults sets default values for any fields that are not provided in the configuration file.
func SetDefaults(cfg *Config) {
	// Server defaults
	if cfg.Server.Role == "" {
		cfg.Server.Role = "backup" // Default role
	}
	if cfg.Server.Port == 0 {
		cfg.Server.Port = 8080 // Default server port
	}

	// Failover defaults
	if cfg.Failover.HealthCheckInterval == 0 {
		cfg.Failover.HealthCheckInterval = 5 // Default interval in seconds
	}
	if cfg.Failover.FailoverTimeout == 0 {
		cfg.Failover.FailoverTimeout = 10 // Default failover timeout in seconds
	}
	if cfg.Failover.RetryAttempts == 0 {
		cfg.Failover.RetryAttempts = 3 // Default retry attempts
	}
	if cfg.Failover.FallbackGracePeriod == 0 {
		cfg.Failover.FallbackGracePeriod = 5 // Default fallback grace period in seconds
	}

	// Communication defaults
	if cfg.Communication.Protocol == "" {
		cfg.Communication.Protocol = "grpc" // Default protocol
	}
	if cfg.Communication.Timeout == 0 {
		cfg.Communication.Timeout = 3 // Default timeout in seconds
	}

	// Health check defaults
	if cfg.Health.CheckEndpoint == "" {
		cfg.Health.CheckEndpoint = "/health" // Default health check endpoint
	}
	if cfg.Health.CheckType == "" {
		cfg.Health.CheckType = "http" // Default check type
	}
	if cfg.Health.NodeAddress == "" {
		cfg.Health.NodeAddress = "127.0.0.1" // Default local node address
	}
	if cfg.Health.NodePort == 0 {
		cfg.Health.NodePort = 9000 // Default node port
	}

	// Logging defaults
	if cfg.Logging.Level == "" {
		cfg.Logging.Level = "info" // Default log level
	}
	if cfg.Logging.LogFile == "" {
		cfg.Logging.LogFile = "failover.log" // Default log file location
	}

	initLogger(cfg)
}

// validateConfig performs basic validation of config values.
func validateConfig(cfg *Config) error {
	if cfg.Server.Port == 0 {
		return fmt.Errorf("server port must be specified")
	}
	if cfg.Failover.HealthCheckInterval <= 0 {
		return fmt.Errorf("failover health_check_interval must be greater than zero")
	}
	if cfg.Communication.Protocol != "grpc" && cfg.Communication.Protocol != "http" {
		return fmt.Errorf("communication protocol must be either 'grpc' or 'http'")
	}
	return nil
}

// initLogger initializes the logging settings.
func initLogger(cfg *Config) {
	level := cfg.Logging.Level
	logFile := cfg.Logging.LogFile

	switch level {
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

	file, err := os.OpenFile(logFile, os.O_RDWR|os.O_CREATE|os.O_APPEND, 0666)
	if err != nil {
		log.Fatalf("Failed to open log file %s: %v", logFile, err)
	}
	multiWriter := io.MultiWriter(file, os.Stdout)
	log.SetOutput(multiWriter)

	log.SetFormatter(&log.TextFormatter{
		FullTimestamp:   true,
		TimestampFormat: "2006-01-02 15:04:05",
		ForceColors:     true,
		DisableColors:   false,
	})
}
