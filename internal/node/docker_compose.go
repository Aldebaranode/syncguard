package node

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"time"

	"github.com/aldebaranode/syncguard/internal/logger"
)

// DockerComposeManager manages nodes via docker-compose commands
type DockerComposeManager struct {
	composeFile string
	service     string
	stopTimeout time.Duration
	logger      *logger.Logger
}

// NewDockerComposeManager creates a new docker-compose manager
func NewDockerComposeManager(cfg Config, log *logger.Logger) *DockerComposeManager {
	return &DockerComposeManager{
		composeFile: cfg.ComposeFile,
		service:     cfg.Service,
		stopTimeout: cfg.StopTimeout,
		logger:      log,
	}
}

func (m *DockerComposeManager) Start() error {
	m.logger.Info("Starting validator via docker-compose: %s (service: %s)", m.composeFile, m.service)

	cmd := exec.Command("docker", "compose", "-f", m.composeFile, "up", "-d", m.service)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		return fmt.Errorf("docker-compose up failed: %w", err)
	}

	m.logger.Info("Docker-compose service %s started", m.service)
	return nil
}

func (m *DockerComposeManager) Stop() error {
	m.logger.Info("Stopping validator via docker-compose: %s (service: %s)", m.composeFile, m.service)

	cmd := exec.Command("docker", "compose", "-f", m.composeFile, "stop",
		"-t", fmt.Sprintf("%d", int(m.stopTimeout.Seconds())), m.service)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		return fmt.Errorf("docker-compose stop failed: %w", err)
	}

	m.logger.Info("Docker-compose service %s stopped", m.service)
	return nil
}

func (m *DockerComposeManager) Restart() error {
	m.logger.Info("Restarting validator via docker-compose: %s (service: %s)", m.composeFile, m.service)

	cmd := exec.Command("docker", "compose", "-f", m.composeFile, "restart",
		"-t", fmt.Sprintf("%d", int(m.stopTimeout.Seconds())), m.service)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		return fmt.Errorf("docker-compose restart failed: %w", err)
	}

	m.logger.Info("Docker-compose service %s restarted", m.service)
	return nil
}

func (m *DockerComposeManager) IsRunning() bool {
	cmd := exec.Command("docker", "compose", "-f", m.composeFile, "ps", "-q", m.service)
	output, err := cmd.Output()
	return err == nil && len(output) > 0
}

func (m *DockerComposeManager) WaitHealthy(ctx context.Context, healthCheck func() bool) error {
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			if healthCheck() {
				m.logger.Info("Validator node is healthy")
				return nil
			}
			m.logger.Debug("Waiting for node to become healthy...")
		}
	}
}
