package manager

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/logger"
	"github.com/aldebaranode/syncguard/internal/state"
)

// newRoleTestManager builds a FailoverManager wired only with what the
// initial-role logic touches.
func newRoleTestManager(statePath string, active bool) *FailoverManager {
	cfg := &config.Config{}
	return &FailoverManager{
		cfg:          cfg,
		stateManager: state.NewManager(statePath, ""),
		logger:       logger.NewLogger(cfg),
		isActive:     active,
	}
}

// TestClaimInitialRoleLocksStateWhenActive verifies that a node starting as
// active takes the state lock, so the lock layer covers the initial active
// period and its failover release is not a no-op.
func TestClaimInitialRoleLocksStateWhenActive(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "priv_validator_state.json")
	fm := newRoleTestManager(statePath, true)

	if err := fm.claimInitialRole(); err != nil {
		t.Fatalf("claimInitialRole failed: %v", err)
	}
	defer fm.stateManager.ReleaseLock()

	if _, err := os.Stat(statePath + ".lock"); err != nil {
		t.Fatalf("active node must hold the state lock after startup: %v", err)
	}
}

// TestClaimInitialRoleLeavesPassiveUnlocked verifies that a passive node does
// not take the lock at startup.
func TestClaimInitialRoleLeavesPassiveUnlocked(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "priv_validator_state.json")
	fm := newRoleTestManager(statePath, false)

	if err := fm.claimInitialRole(); err != nil {
		t.Fatalf("claimInitialRole failed: %v", err)
	}

	if _, err := os.Stat(statePath + ".lock"); !os.IsNotExist(err) {
		t.Fatalf("passive node must not hold the state lock, stat err = %v", err)
	}
}
