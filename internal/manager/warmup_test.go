package manager

import (
	"testing"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/logger"
)

// newWarmupTestManager builds a minimal FailoverManager wired only with the
// fields the warm-up / failure-counting logic touches.
func newWarmupTestManager(retry int, warmup float64) *FailoverManager {
	cfg := &config.Config{}
	cfg.Failover.RetryAttempts = retry
	cfg.Failover.WarmupPeriod = warmup
	return &FailoverManager{
		cfg:      cfg,
		logger:   logger.NewLogger(&config.Config{}),
		isActive: true,
	}
}

// TestWarmupSuppressesFailover proves that failures during the post-restart
// warm-up window do not accumulate toward a failover. This guards against the
// ping-pong where a takeover restart immediately re-triggers a failover.
func TestWarmupSuppressesFailover(t *testing.T) {
	fm := newWarmupTestManager(3, 90)
	fm.BeginWarmup()

	// Far more failures than RetryAttempts, all inside the warm-up window.
	for i := 0; i < 10; i++ {
		fm.handleHealthCheckFailure()
	}

	if fm.failureCount != 0 {
		t.Fatalf("during warm-up failureCount must stay 0, got %d", fm.failureCount)
	}
}

// TestFailureCountingResumesAfterWarmup proves normal failure accumulation
// resumes once the warm-up window has elapsed.
func TestFailureCountingResumesAfterWarmup(t *testing.T) {
	fm := newWarmupTestManager(3, 90)
	fm.BeginWarmup()

	// Expire the warm-up window.
	fm.mu.Lock()
	fm.warmupUntil = time.Now().Add(-time.Second)
	fm.mu.Unlock()

	// Two failures (below the threshold of 3, so no failover is triggered).
	fm.handleHealthCheckFailure()
	fm.handleHealthCheckFailure()

	if fm.failureCount != 2 {
		t.Fatalf("after warm-up failures should accumulate, got %d", fm.failureCount)
	}
}
