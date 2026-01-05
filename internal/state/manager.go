package state

import (
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"time"
)

// ValidatorState represents the priv_validator_state.json structure
type ValidatorState struct {
	Height    int64  `json:"height"`
	Round     int32  `json:"round"`
	Step      int8   `json:"step"`
	Signature []byte `json:"signature,omitempty"`
	SignBytes []byte `json:"signbytes,omitempty"`
}

// Manager handles validator state synchronization
type Manager struct {
	statePath    string
	backupPath   string
	lastSync     time.Time
	currentState *ValidatorState
	mu           sync.RWMutex
	lockFile     *os.File
}

// NewManager creates a new validator state manager
func NewManager(statePath, backupPath string) *Manager {
	return &Manager{
		statePath:  statePath,
		backupPath: backupPath,
	}
}

// LoadState reads the current validator state from disk
func (m *Manager) LoadState() (*ValidatorState, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	data, err := os.ReadFile(m.statePath)
	if err != nil {
		return nil, fmt.Errorf("failed to read state file: %w", err)
	}

	var state ValidatorState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, fmt.Errorf("failed to parse state file: %w", err)
	}

	m.currentState = &state
	return &state, nil
}

// SaveState writes the validator state to disk
func (m *Manager) SaveState(state *ValidatorState) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal state: %w", err)
	}

	// Write to temporary file first
	tmpFile := m.statePath + ".tmp"
	if err := os.WriteFile(tmpFile, data, 0600); err != nil {
		return fmt.Errorf("failed to write temp state file: %w", err)
	}

	// Atomic rename
	if err := os.Rename(tmpFile, m.statePath); err != nil {
		return fmt.Errorf("failed to rename state file: %w", err)
	}

	// Backup the state
	if m.backupPath != "" {
		if err := os.WriteFile(m.backupPath, data, 0600); err != nil {
			fmt.Printf("Warning: failed to write backup state: %v\n", err)
		}
	}

	m.currentState = state
	m.lastSync = time.Now()
	return nil
}

// AcquireLock obtains an exclusive lock on the state file
func (m *Manager) AcquireLock() error {
	lockPath := m.statePath + ".lock"
	file, err := os.OpenFile(lockPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		if os.IsExist(err) {
			return fmt.Errorf("state is already locked")
		}
		return fmt.Errorf("failed to acquire lock: %w", err)
	}

	m.lockFile = file
	pid := fmt.Sprintf("%d\n", os.Getpid())
	file.WriteString(pid)

	return nil
}

// ReleaseLock releases the exclusive lock on the state file
func (m *Manager) ReleaseLock() error {
	if m.lockFile == nil {
		return nil
	}

	m.lockFile.Close()
	lockPath := m.statePath + ".lock"
	if err := os.Remove(lockPath); err != nil {
		return fmt.Errorf("failed to remove lock file: %w", err)
	}

	m.lockFile = nil
	return nil
}

// CompareStates checks if it's safe to take over signing duties
func (m *Manager) CompareStates(localState, remoteState *ValidatorState) (bool, error) {
	// Never sign if remote is ahead
	if remoteState.Height > localState.Height {
		return false, fmt.Errorf("remote height %d is ahead of local height %d",
			remoteState.Height, localState.Height)
	}

	// If at same height, check round
	if remoteState.Height == localState.Height {
		if remoteState.Round > localState.Round {
			return false, fmt.Errorf("remote round %d is ahead of local round %d at height %d",
				remoteState.Round, localState.Round, localState.Height)
		}

		// If at same round, check step
		if remoteState.Round == localState.Round {
			if remoteState.Step >= localState.Step {
				return false, fmt.Errorf("remote step %d is >= local step %d at height %d, round %d",
					remoteState.Step, localState.Step, localState.Height, localState.Round)
			}
		}
	}

	return true, nil
}

// SyncFromRemote synchronizes state from the active node
func (m *Manager) SyncFromRemote(remoteState *ValidatorState) error {
	localState, err := m.LoadState()
	if err != nil {
		return fmt.Errorf("failed to load local state: %w", err)
	}

	// Check if it's safe to update
	canUpdate, err := m.CompareStates(localState, remoteState)
	if !canUpdate {
		return fmt.Errorf("unsafe to sync: %v", err)
	}

	return m.SaveState(remoteState)
}

// GetCurrentState returns the current state
func (m *Manager) GetCurrentState() *ValidatorState {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.currentState
}
