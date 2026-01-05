package state_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/aldebaranode/syncguard/internal/state"
)

func TestManager_SaveAndLoad(t *testing.T) {
	tmpDir := t.TempDir()
	statePath := filepath.Join(tmpDir, "priv_validator_state.json")
	backupPath := filepath.Join(tmpDir, "backup_state.json")

	mgr := state.NewManager(statePath, backupPath)

	// Save state
	testState := &state.ValidatorState{
		Height: 1000,
		Round:  1,
		Step:   3,
	}

	if err := mgr.SaveState(testState); err != nil {
		t.Fatalf("Failed to save state: %v", err)
	}

	// Verify file exists
	if _, err := os.Stat(statePath); os.IsNotExist(err) {
		t.Error("State file was not created")
	}

	// Load state
	loaded, err := mgr.LoadState()
	if err != nil {
		t.Fatalf("Failed to load state: %v", err)
	}

	if loaded.Height != testState.Height {
		t.Errorf("Height mismatch: got %d, want %d", loaded.Height, testState.Height)
	}
	if loaded.Round != testState.Round {
		t.Errorf("Round mismatch: got %d, want %d", loaded.Round, testState.Round)
	}
	if loaded.Step != testState.Step {
		t.Errorf("Step mismatch: got %d, want %d", loaded.Step, testState.Step)
	}
}

func TestManager_Lock(t *testing.T) {
	tmpDir := t.TempDir()
	statePath := filepath.Join(tmpDir, "priv_validator_state.json")

	mgr := state.NewManager(statePath, "")

	// Acquire lock
	if err := mgr.AcquireLock(); err != nil {
		t.Fatalf("Failed to acquire lock: %v", err)
	}

	// Try to acquire again - should fail
	mgr2 := state.NewManager(statePath, "")
	if err := mgr2.AcquireLock(); err == nil {
		t.Error("Second lock acquisition should have failed")
	}

	// Release and retry
	if err := mgr.ReleaseLock(); err != nil {
		t.Fatalf("Failed to release lock: %v", err)
	}

	if err := mgr2.AcquireLock(); err != nil {
		t.Errorf("Should be able to acquire lock after release: %v", err)
	}

	mgr2.ReleaseLock()
}

func TestManager_CompareStates(t *testing.T) {
	tmpDir := t.TempDir()
	statePath := filepath.Join(tmpDir, "priv_validator_state.json")

	mgr := state.NewManager(statePath, "")

	tests := []struct {
		name        string
		local       *state.ValidatorState
		remote      *state.ValidatorState
		canTakeOver bool
	}{
		{
			name:        "local ahead in height",
			local:       &state.ValidatorState{Height: 1000, Round: 0, Step: 1},
			remote:      &state.ValidatorState{Height: 999, Round: 0, Step: 1},
			canTakeOver: true,
		},
		{
			name:        "remote ahead in height",
			local:       &state.ValidatorState{Height: 999, Round: 0, Step: 1},
			remote:      &state.ValidatorState{Height: 1000, Round: 0, Step: 1},
			canTakeOver: false,
		},
		{
			name:        "same height local ahead in round",
			local:       &state.ValidatorState{Height: 1000, Round: 2, Step: 1},
			remote:      &state.ValidatorState{Height: 1000, Round: 1, Step: 1},
			canTakeOver: true,
		},
		{
			name:        "same height remote ahead in round",
			local:       &state.ValidatorState{Height: 1000, Round: 1, Step: 1},
			remote:      &state.ValidatorState{Height: 1000, Round: 2, Step: 1},
			canTakeOver: false,
		},
		{
			name:        "same height/round local ahead in step",
			local:       &state.ValidatorState{Height: 1000, Round: 1, Step: 3},
			remote:      &state.ValidatorState{Height: 1000, Round: 1, Step: 2},
			canTakeOver: true,
		},
		{
			name:        "same height/round remote ahead in step",
			local:       &state.ValidatorState{Height: 1000, Round: 1, Step: 2},
			remote:      &state.ValidatorState{Height: 1000, Round: 1, Step: 3},
			canTakeOver: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			canTakeOver, _ := mgr.CompareStates(tt.local, tt.remote)
			if canTakeOver != tt.canTakeOver {
				t.Errorf("CompareStates() = %v, want %v", canTakeOver, tt.canTakeOver)
			}
		})
	}
}

func TestDoubleSignProtector_CanSign(t *testing.T) {
	protector := state.NewDoubleSignProtector()
	defer protector.Stop()

	// First signature should be allowed
	canSign, err := protector.CanSign(1000, 0, 1)
	if !canSign || err != nil {
		t.Errorf("First signature should be allowed: canSign=%v, err=%v", canSign, err)
	}

	// Record it
	if err := protector.RecordSignature(1000, 0, 1); err != nil {
		t.Fatalf("Failed to record signature: %v", err)
	}

	// Same height/round/step should be rejected
	canSign, err = protector.CanSign(1000, 0, 1)
	if canSign {
		t.Error("Duplicate signature should be rejected")
	}
	if err == nil {
		t.Error("Should return error for duplicate signature")
	}

	// Higher height should be allowed
	canSign, err = protector.CanSign(1001, 0, 1)
	if !canSign || err != nil {
		t.Errorf("Higher height should be allowed: canSign=%v, err=%v", canSign, err)
	}

	// Lower height should be rejected
	canSign, err = protector.CanSign(999, 0, 1)
	if canSign {
		t.Error("Lower height signature should be rejected")
	}
}

func TestDoubleSignProtector_ValidStepProgression(t *testing.T) {
	protector := state.NewDoubleSignProtector()
	defer protector.Stop()

	// Sign step 1
	protector.RecordSignature(1000, 0, 1)

	// Step 2 at same height/round should be allowed (valid progression)
	canSign, err := protector.CanSign(1000, 0, 2)
	if !canSign || err != nil {
		t.Errorf("Valid step progression should be allowed: canSign=%v, err=%v", canSign, err)
	}
}
