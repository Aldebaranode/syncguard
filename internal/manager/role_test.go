package manager

import (
	"fmt"
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

// TestRoleMatchesKeyAtStart verifies that a start is refused when the
// configured role contradicts the key at keyPath: an active node must hold the
// validator key and a passive node must not.
func TestRoleMatchesKeyAtStart(t *testing.T) {
	otherAddress := "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
	cases := []struct {
		name         string
		active       bool
		holdsKey     bool
		wantErrorFmt string
	}{
		{name: "active with validator key", active: true, holdsKey: true},
		{name: "passive with mock key", active: false, holdsKey: false},
		{name: "active with mock key", active: true, holdsKey: false,
			wantErrorFmt: "configured as active but %s does not hold validator key %s"},
		{name: "passive with validator key", active: false, holdsKey: true,
			wantErrorFmt: "configured as passive but %s holds validator key %s"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			keyPath := filepath.Join(dir, "priv_validator_key.json")
			cfg := &config.Config{}
			generated := newKeyManagerForGeneratedKey(t, keyPath, dir, logger.NewLogger(cfg))
			generatedKey, err := generated.LoadKey()
			if err != nil {
				t.Fatalf("failed to load generated key: %v", err)
			}
			validatorAddress := otherAddress
			if tc.holdsKey {
				validatorAddress = generatedKey.Address
			}
			cfg.CometBFT.KeyPath = keyPath
			cfg.CometBFT.ValidatorAddress = validatorAddress
			fm := &FailoverManager{
				cfg:        cfg,
				keyManager: state.NewKeyManager(keyPath, dir, validatorAddress, logger.NewLogger(cfg)),
				logger:     logger.NewLogger(cfg),
				isActive:   tc.active,
			}

			err = fm.verifyRoleMatchesKey()

			if tc.wantErrorFmt == "" {
				if err != nil {
					t.Fatalf("start refused: %v", err)
				}
				return
			}
			want := fmt.Sprintf(tc.wantErrorFmt, keyPath, validatorAddress)
			if err == nil || err.Error() != want {
				t.Fatalf("error = %v, want %q", err, want)
			}
		})
	}
}
