package state

import (
	"encoding/json"
	"fmt"
	"os"
)

// ValidatorKey represents the priv_validator_key.json structure
type ValidatorKey struct {
	Address string          `json:"address"`
	PubKey  json.RawMessage `json:"pub_key"`
	PrivKey json.RawMessage `json:"priv_key"`
}

// KeyManager handles validator key operations
type KeyManager struct {
	keyPath    string
	backupPath string
}

// NewKeyManager creates a new key manager
func NewKeyManager(keyPath, backupPath string) *KeyManager {
	return &KeyManager{
		keyPath:    keyPath,
		backupPath: backupPath,
	}
}

// LoadKey reads the validator key from disk
func (km *KeyManager) LoadKey() (*ValidatorKey, error) {
	data, err := os.ReadFile(km.keyPath)
	if err != nil {
		return nil, fmt.Errorf("failed to read key file: %w", err)
	}

	var key ValidatorKey
	if err := json.Unmarshal(data, &key); err != nil {
		return nil, fmt.Errorf("failed to parse key file: %w", err)
	}

	return &key, nil
}

// SaveKey writes the validator key to disk
func (km *KeyManager) SaveKey(key *ValidatorKey) error {
	data, err := json.MarshalIndent(key, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal key: %w", err)
	}

	// Write to temp file first
	tmpFile := km.keyPath + ".tmp"
	if err := os.WriteFile(tmpFile, data, 0600); err != nil {
		return fmt.Errorf("failed to write temp key file: %w", err)
	}

	// Atomic rename
	if err := os.Rename(tmpFile, km.keyPath); err != nil {
		return fmt.Errorf("failed to rename key file: %w", err)
	}

	return nil
}

// BackupKey creates a backup of the current key
func (km *KeyManager) BackupKey() error {
	if km.backupPath == "" {
		return nil
	}

	key, err := km.LoadKey()
	if err != nil {
		return err
	}

	data, err := json.MarshalIndent(key, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal key: %w", err)
	}

	backupFile := km.backupPath + "/priv_validator_key.json.bak"
	if err := os.WriteFile(backupFile, data, 0600); err != nil {
		return fmt.Errorf("failed to write backup key: %w", err)
	}

	return nil
}

// DeleteKey removes the validator key (disables signing)
func (km *KeyManager) DeleteKey() error {
	// Backup first
	if err := km.BackupKey(); err != nil {
		return fmt.Errorf("failed to backup before delete: %w", err)
	}

	// Rename to .disabled instead of deleting
	disabledPath := km.keyPath + ".disabled"
	if err := os.Rename(km.keyPath, disabledPath); err != nil {
		return fmt.Errorf("failed to disable key: %w", err)
	}

	return nil
}

// RestoreKey restores the validator key from .disabled
func (km *KeyManager) RestoreKey() error {
	disabledPath := km.keyPath + ".disabled"

	if _, err := os.Stat(disabledPath); os.IsNotExist(err) {
		return fmt.Errorf("no disabled key to restore")
	}

	if err := os.Rename(disabledPath, km.keyPath); err != nil {
		return fmt.Errorf("failed to restore key: %w", err)
	}

	return nil
}

// HasKey checks if the key file exists
func (km *KeyManager) HasKey() bool {
	_, err := os.Stat(km.keyPath)
	return err == nil
}

// KeyToBytes serializes the key for transfer
func (km *KeyManager) KeyToBytes() ([]byte, error) {
	return os.ReadFile(km.keyPath)
}

// KeyFromBytes deserializes and saves the key from transfer
func (km *KeyManager) KeyFromBytes(data []byte) error {
	var key ValidatorKey
	if err := json.Unmarshal(data, &key); err != nil {
		return fmt.Errorf("invalid key data: %w", err)
	}

	return km.SaveKey(&key)
}
