package node

import (
	"context"
	"time"

	"github.com/aldebaranode/syncguard/internal/constants"
	"github.com/aldebaranode/syncguard/internal/logger"
)

// Manager defines the interface for node lifecycle management
type Manager interface {
	Start() error
	Stop() error
	Restart() error
	IsRunning() bool
	WaitHealthy(ctx context.Context, healthCheck func() bool) error
}

// Config holds node manager configuration
type Config struct {
	Mode         constants.NodeManagerType
	Binary       string // Binary mode: path to executable
	Args         []string
	Container    string // Docker mode: container name or ID
	ComposeFile  string // Docker Compose mode: path to compose file
	Service      string // Docker Compose mode: service name
	StopTimeout  time.Duration
	RestartDelay time.Duration
}

// NewManager creates the appropriate manager based on mode (Factory)
func NewManager(cfg Config, log *logger.Logger) Manager {
	if cfg.StopTimeout == 0 {
		cfg.StopTimeout = 30 * time.Second
	}
	if cfg.RestartDelay == 0 {
		cfg.RestartDelay = 2 * time.Second
	}

	switch cfg.Mode {
	case "docker":
		mgr, err := NewDockerManager(cfg, log)
		if err != nil {
			log.Error("Failed to create Docker manager: %v, falling back to docker-compose", err)
			return NewDockerComposeManager(cfg, log)
		}
		return mgr
	case "docker-compose":
		return NewDockerComposeManager(cfg, log)
	default: // "binary"
		return NewBinaryManager(cfg, log)
	}
}
