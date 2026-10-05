package manager

import (
	"sync"
	"testing"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/health"
	"github.com/aldebaranode/syncguard/internal/logger"
)

// fakeHealthChecker mirrors health.Checker: IsHealthy reports the cached
// result of the last PerformHealthCheck, which observes nodeUp.
type fakeHealthChecker struct {
	mu            sync.Mutex
	nodeUp        bool
	cachedHealthy bool
}

// PerformHealthCheck runs a fresh check and caches what it observed.
func (f *fakeHealthChecker) PerformHealthCheck() (*health.NodeHealth, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cachedHealthy = f.nodeUp
	return &health.NodeHealth{Healthy: f.nodeUp}, nil
}

// IsHealthy reports the cached result.
func (f *fakeHealthChecker) IsHealthy() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.cachedHealthy
}

// GetLastHeight returns a fixed height.
func (f *fakeHealthChecker) GetLastHeight() int64 { return 1 }

// TestStartupWaitRunsFreshHealthCheck verifies that the startup wait runs a
// fresh health check on each poll, so it returns once the node is up instead
// of waiting for a cached result that only the monitor loop would fill.
func TestStartupWaitRunsFreshHealthCheck(t *testing.T) {
	cfg := &config.Config{}
	fm := &FailoverManager{
		cfg:           cfg,
		healthChecker: &fakeHealthChecker{nodeUp: true},
		nodeManager:   &recordingNode{log: &eventLog{}},
		logger:        logger.NewLogger(cfg),
	}

	done := make(chan struct{})
	go func() {
		fm.waitForNodeHealthy()
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("startup wait still blocked after 5s although the node is up")
	}
}
