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

// DeleteKey disables signing by swapping real key with auto-generated mock key
func (km *KeyManager) DeleteKey() error {
	// Backup first
	if err := km.BackupKey(); err != nil {
		return fmt.Errorf("failed to backup before delete: %w", err)
	}

	// Save real key to .real
	realKeyPath := km.keyPath + ".real"
	if err := os.Rename(km.keyPath, realKeyPath); err != nil {
		return fmt.Errorf("failed to save real key: %w", err)
	}

	// Generate mock key with dummy values (different address prevents signing)
	mockKey := &ValidatorKey{
		Address: "48DC218393FCEEF56A37D963B804FAB92C62CA9D",
		PubKey:  json.RawMessage(`{"type":"tendermint/PubKeySecp256k1","value":"AvLo+lkg0UWozoI+pJzv1a7upt+HaMxZCdWgRxvZ8Cb1"}`),
		PrivKey: json.RawMessage(`{"type":"tendermint/PrivKeySecp256k1","value":"ansj9FenmlrmNrxi0BXgZ+YfJBSGZqy20i7/K7CdOiQ="}`),
	}

	mockData, err := json.MarshalIndent(mockKey, "", "  ")
	if err != nil {
		// Rollback
		os.Rename(realKeyPath, km.keyPath)
		return fmt.Errorf("failed to marshal mock key: %w", err)
	}

	if err := os.WriteFile(km.keyPath, mockData, 0600); err != nil {
		// Rollback
		os.Rename(realKeyPath, km.keyPath)
		return fmt.Errorf("failed to write mock key: %w", err)
	}

	return nil
}

// RestoreKey restores the validator key from .real (mock swap) or .disabled
func (km *KeyManager) RestoreKey() error {
	// Try .real first (mock key swap was used)
	realKeyPath := km.keyPath + ".real"
	if _, err := os.Stat(realKeyPath); err == nil {
		// Remove current mock key and restore real key
		if err := os.Remove(km.keyPath); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("failed to remove mock key: %w", err)
		}
		if err := os.Rename(realKeyPath, km.keyPath); err != nil {
			return fmt.Errorf("failed to restore real key: %w", err)
		}
		return nil
	}

	// Fallback: try .disabled
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
