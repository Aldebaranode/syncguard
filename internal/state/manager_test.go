package state

import (
	"os"
	"path/filepath"
	"testing"
)

func TestManager_SaveAndLoad(t *testing.T) {
	tmpDir := t.TempDir()
	statePath := filepath.Join(tmpDir, "priv_validator_state.json")
	backupPath := filepath.Join(tmpDir, "backup_state.json")

	mgr := NewManager(statePath, backupPath)

	// Save state
	testState := &ValidatorState{
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

	mgr := NewManager(statePath, "")

	// Acquire lock
	if err := mgr.AcquireLock(); err != nil {
		t.Fatalf("Failed to acquire lock: %v", err)
	}

	// Try to acquire again - should fail
	mgr2 := NewManager(statePath, "")
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

	mgr := NewManager(statePath, "")

	tests := []struct {
		name        string
		local       *ValidatorState
		remote      *ValidatorState
		canTakeOver bool
	}{
		{
			name:        "local ahead in height",
			local:       &ValidatorState{Height: 1000, Round: 0, Step: 1},
			remote:      &ValidatorState{Height: 999, Round: 0, Step: 1},
			canTakeOver: true,
		},
		{
			name:        "remote ahead in height",
			local:       &ValidatorState{Height: 999, Round: 0, Step: 1},
			remote:      &ValidatorState{Height: 1000, Round: 0, Step: 1},
			canTakeOver: false,
		},
		{
			name:        "same height local ahead in round",
			local:       &ValidatorState{Height: 1000, Round: 2, Step: 1},
			remote:      &ValidatorState{Height: 1000, Round: 1, Step: 1},
			canTakeOver: true,
		},
		{
			name:        "same height remote ahead in round",
			local:       &ValidatorState{Height: 1000, Round: 1, Step: 1},
			remote:      &ValidatorState{Height: 1000, Round: 2, Step: 1},
			canTakeOver: false,
		},
		{
			name:        "same height/round local ahead in step",
			local:       &ValidatorState{Height: 1000, Round: 1, Step: 3},
			remote:      &ValidatorState{Height: 1000, Round: 1, Step: 2},
			canTakeOver: true,
		},
		{
			name:        "same height/round remote ahead in step",
			local:       &ValidatorState{Height: 1000, Round: 1, Step: 2},
			remote:      &ValidatorState{Height: 1000, Round: 1, Step: 3},
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
