package node

import (
	"context"
	"fmt"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"

	"github.com/aldebaranode/syncguard/internal/logger"
)

// DockerManager manages nodes via Docker SDK
type DockerManager struct {
	client      *client.Client
	containerID string
	stopTimeout time.Duration
	logger      *logger.Logger
}

// NewDockerManager creates a new Docker SDK manager
func NewDockerManager(cfg Config, log *logger.Logger) (*DockerManager, error) {
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return nil, fmt.Errorf("failed to create docker client: %w", err)
	}

	return &DockerManager{
		client:      cli,
		containerID: cfg.Container,
		stopTimeout: cfg.StopTimeout,
		logger:      log,
	}, nil
}

func (m *DockerManager) Start() error {
	m.logger.Info("Starting container: %s", m.containerID)

	ctx := context.Background()
	if err := m.client.ContainerStart(ctx, m.containerID, container.StartOptions{}); err != nil {
		return fmt.Errorf("failed to start container: %w", err)
	}

	m.logger.Info("Container %s started", m.containerID)
	return nil
}

func (m *DockerManager) Stop() error {
	m.logger.Info("Stopping container: %s", m.containerID)

	ctx := context.Background()
	timeout := int(m.stopTimeout.Seconds())
	if err := m.client.ContainerStop(ctx, m.containerID, container.StopOptions{Timeout: &timeout}); err != nil {
		return fmt.Errorf("failed to stop container: %w", err)
	}

	m.logger.Info("Container %s stopped", m.containerID)
	return nil
}

func (m *DockerManager) Restart() error {
	m.logger.Info("Restarting container: %s", m.containerID)

	ctx := context.Background()
	timeout := int(m.stopTimeout.Seconds())
	if err := m.client.ContainerRestart(ctx, m.containerID, container.StopOptions{Timeout: &timeout}); err != nil {
		return fmt.Errorf("failed to restart container: %w", err)
	}

	m.logger.Info("Container %s restarted", m.containerID)
	return nil
}

func (m *DockerManager) IsRunning() bool {
	ctx := context.Background()
	info, err := m.client.ContainerInspect(ctx, m.containerID)
	if err != nil {
		return false
	}
	return info.State.Running
}

func (m *DockerManager) WaitHealthy(ctx context.Context, healthCheck func() bool) error {
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

// Close closes the Docker client connection
func (m *DockerManager) Close() error {
	return m.client.Close()
}
