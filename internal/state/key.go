package state

import (
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/aldebaranode/syncguard/internal/constants"
	"github.com/aldebaranode/syncguard/internal/crypto"
	"github.com/aldebaranode/syncguard/internal/logger"
	k1 "github.com/cometbft/cometbft/crypto/secp256k1"
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
	logger     *logger.Logger
}

// NewKeyManager creates a new key manager
func NewKeyManager(keyPath string, backupPath string, logger *logger.Logger) *KeyManager {

	return &KeyManager{
		keyPath:    keyPath,
		backupPath: backupPath,
		logger:     logger,
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

func (km *KeyManager) InitializeKey() error {
	keyPath := km.keyPath
	if _, err := os.Stat(keyPath); err == nil {
		km.logger.Info("key found, using existing file: %s", keyPath)
		return nil
	}

	km.logger.Info("key not found, generating new key: %s", keyPath)

	// Generate secp256k1 private key (same as Story's k1.GenPrivKey())
	privKey := k1.GenPrivKey()
	pubKey := privKey.PubKey()

	// Address is first 20 bytes of SHA256(pubkey), uppercased hex
	address := strings.ToUpper(hex.EncodeToString(pubKey.Address()))

	key := &ValidatorKey{
		Address: address,
		PubKey:  json.RawMessage(fmt.Sprintf(`{"type":"%s","value":"%s"}`, constants.Secp256k1PubKeyType, base64.StdEncoding.EncodeToString(pubKey.Bytes()))),
		PrivKey: json.RawMessage(fmt.Sprintf(`{"type":"%s","value":"%s"}`, constants.Secp256k1PrivKeyType, base64.StdEncoding.EncodeToString(privKey.Bytes()))),
	}

	// Ensure directory exists
	dir := filepath.Dir(keyPath)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return fmt.Errorf("failed to create key directory: %w", err)
	}

	// Save the key
	if err := km.SaveKey(key); err != nil {
		return fmt.Errorf("failed to save generated key: %w", err)
	}

	km.logger.Info("generated new validator key with address: %s", address)
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

// EncryptKeyToBytes encrypts the key for transfer
func (km *KeyManager) EncryptKeyToBytes(secret string) ([]byte, error) {
	keyData, err := km.KeyToBytes()
	if err != nil {
		return nil, err
	}

	encryptedBytes, err := crypto.Encrypt(keyData, secret)
	if err != nil {
		return nil, err
	}

	return encryptedBytes, nil
}

// KeyFromBytes deserializes and saves the key from transfer
func (km *KeyManager) KeyFromBytes(data []byte) error {
	var key ValidatorKey
	if err := json.Unmarshal(data, &key); err != nil {
		return fmt.Errorf("invalid key data: %w", err)
	}

	return km.SaveKey(&key)
}

// DecryptKeyFromBytes decrypts the key from transfer
func (km *KeyManager) DecryptKeyFromBytes(data []byte, secret string) error {
	keyData, err := crypto.Decrypt(data, secret)
	if err != nil {
		return fmt.Errorf("failed to decrypt key: %w", err)
	}

	return km.KeyFromBytes(keyData)
}
