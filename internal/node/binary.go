package node

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"time"

	"github.com/aldebaranode/syncguard/internal/logger"
)

// BinaryManager manages nodes by spawning a binary process directly
type BinaryManager struct {
	binary       string
	args         []string
	stopTimeout  time.Duration
	restartDelay time.Duration
	logger       *logger.Logger

	cmd     *exec.Cmd
	mu      sync.Mutex
	running bool
	exitCh  chan error
}

// NewBinaryManager creates a new binary manager
func NewBinaryManager(cfg Config, log *logger.Logger) *BinaryManager {
	return &BinaryManager{
		binary:       cfg.Binary,
		args:         cfg.Args,
		stopTimeout:  cfg.StopTimeout,
		restartDelay: cfg.RestartDelay,
		logger:       log,
		exitCh:       make(chan error, 1),
	}
}

func (m *BinaryManager) Start() error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.running {
		return fmt.Errorf("node already running")
	}

	m.logger.Info("Starting validator node: %s %v", m.binary, m.args)

	m.cmd = exec.Command(m.binary, m.args...)
	m.cmd.Stdout = os.Stdout
	m.cmd.Stderr = os.Stderr
	m.cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := m.cmd.Start(); err != nil {
		return fmt.Errorf("failed to start node: %w", err)
	}

	m.running = true
	m.logger.Info("Validator node started with PID %d", m.cmd.Process.Pid)

	go m.monitor()
	return nil
}

func (m *BinaryManager) Stop() error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if !m.running || m.cmd == nil || m.cmd.Process == nil {
		m.logger.Debug("Node not running, nothing to stop")
		return nil
	}

	pid := m.cmd.Process.Pid
	m.logger.Info("Stopping validator node (PID %d)...", pid)

	if err := m.cmd.Process.Signal(syscall.SIGTERM); err != nil {
		m.logger.Warn("Failed to send SIGTERM: %v", err)
	}

	done := make(chan error, 1)
	go func() {
		_, err := m.cmd.Process.Wait()
		done <- err
	}()

	select {
	case <-done:
		m.logger.Info("Validator node stopped gracefully")
	case <-time.After(m.stopTimeout):
		m.logger.Warn("Stop timeout, sending SIGKILL")
		syscall.Kill(-pid, syscall.SIGKILL)
		<-done
	}

	m.running = false
	m.cmd = nil
	return nil
}

func (m *BinaryManager) Restart() error {
	m.logger.Info("Restarting validator node...")

	if err := m.Stop(); err != nil {
		return fmt.Errorf("failed to stop node: %w", err)
	}

	time.Sleep(m.restartDelay)

	if err := m.Start(); err != nil {
		return fmt.Errorf("failed to start node: %w", err)
	}

	return nil
}

func (m *BinaryManager) IsRunning() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.running
}

func (m *BinaryManager) WaitHealthy(ctx context.Context, healthCheck func() bool) error {
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

func (m *BinaryManager) monitor() {
	if m.cmd == nil {
		return
	}

	err := m.cmd.Wait()

	m.mu.Lock()
	m.running = false
	m.mu.Unlock()

	if err != nil {
		m.logger.Error("Validator node exited with error: %v", err)
	} else {
		m.logger.Info("Validator node exited cleanly")
	}

	select {
	case m.exitCh <- err:
	default:
	}
}
