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
	keyPath          string
	backupPath       string
	validatorAddress string
	logger           *logger.Logger
}

// NewKeyManager creates a key manager bound to the validator address it protects
func NewKeyManager(keyPath string, backupPath string, validatorAddress string, logger *logger.Logger) *KeyManager {
	return &KeyManager{
		keyPath:          keyPath,
		backupPath:       backupPath,
		validatorAddress: validatorAddress,
		logger:           logger,
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

// IsValidatorKey reports whether key carries the configured validator address.
func (km *KeyManager) IsValidatorKey(key *ValidatorKey) bool {
	if key == nil || key.Address == "" || km.validatorAddress == "" {
		return false
	}
	return strings.EqualFold(key.Address, km.validatorAddress)
}

// realKeyPath is where DeleteKey stashes the validator key it disarms.
func (km *KeyManager) realKeyPath() string {
	return km.keyPath + ".real"
}

// stagedKeyPath is where a received validator key waits until a takeover or
// failback promotes it. The node process never reads this file.
func (km *KeyManager) stagedKeyPath() string {
	return km.keyPath + ".staged"
}

// HoldsValidatorKey reports whether the key file holds the validator key. A
// missing file means false without an error; an unreadable key is an error.
func (km *KeyManager) HoldsValidatorKey() (bool, error) {
	return km.holdsValidatorKeyAt(km.keyPath)
}

// HoldsStagedValidatorKey reports whether the staged file holds the validator
// key, with the same semantics as HoldsValidatorKey.
func (km *KeyManager) HoldsStagedValidatorKey() (bool, error) {
	return km.holdsValidatorKeyAt(km.stagedKeyPath())
}

// HoldsRealValidatorKey reports whether the .real stash holds the validator
// key, with the same semantics as HoldsValidatorKey.
func (km *KeyManager) HoldsRealValidatorKey() (bool, error) {
	return km.holdsValidatorKeyAt(km.realKeyPath())
}

// holdsValidatorKeyAt reports whether the key file at path holds the validator
// key. A missing file means false without an error.
func (km *KeyManager) holdsValidatorKeyAt(path string) (bool, error) {
	key, err := km.loadKeyAt(path)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return km.IsValidatorKey(key), nil
}

// loadKeyAt reads and parses the key file at path. A missing file returns an
// error that satisfies os.IsNotExist.
func (km *KeyManager) loadKeyAt(path string) (*ValidatorKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var key ValidatorKey
	if err := json.Unmarshal(data, &key); err != nil {
		return nil, fmt.Errorf("failed to parse key file %s: %w", path, err)
	}
	return &key, nil
}

// SaveKey writes the validator key to disk
func (km *KeyManager) SaveKey(key *ValidatorKey) error {
	data, err := json.MarshalIndent(key, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal key: %w", err)
	}
	return writeKeyFile(km.keyPath, data)
}

// writeKeyFile writes data to path with mode 0600 through a temp file and a
// rename, so a reader never sees a partly written key.
func writeKeyFile(path string, data []byte) error {
	tmpFile := path + ".tmp"
	if err := os.WriteFile(tmpFile, data, 0600); err != nil {
		return fmt.Errorf("failed to write temp key file: %w", err)
	}
	if err := os.Rename(tmpFile, path); err != nil {
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

// DeleteKey disables signing (a no-op unless the key file holds the validator key) by swapping real key with auto-generated mock key
func (km *KeyManager) DeleteKey() error {
	// Already disarmed or missing: the stashed .real and .bak must stay intact,
	// so a mock key is never moved over them.
	holds, err := km.HoldsValidatorKey()
	if err != nil {
		return fmt.Errorf("failed to inspect key before delete: %w", err)
	}
	if !holds {
		return nil
	}

	// Backup first
	if err := km.BackupKey(); err != nil {
		return fmt.Errorf("failed to backup before delete: %w", err)
	}

	// Save real key to .real
	realKeyPath := km.realKeyPath()
	if err := os.Rename(km.keyPath, realKeyPath); err != nil {
		return fmt.Errorf("failed to save real key: %w", err)
	}

	// Generate a fresh random key with a DIFFERENT address. The node loads a
	// structurally valid key but cannot sign as the real validator, so signing
	// is effectively disabled. This is what prevents double-signing: the real
	// key now lives only on the peer, and this node can never equivocate with it.
	privKey := k1.GenPrivKey()
	pubKey := privKey.PubKey()
	mockKey := &ValidatorKey{
		Address: strings.ToUpper(hex.EncodeToString(pubKey.Address())),
		PubKey:  json.RawMessage(fmt.Sprintf(`{"type":"%s","value":"%s"}`, constants.Secp256k1PubKeyType, base64.StdEncoding.EncodeToString(pubKey.Bytes()))),
		PrivKey: json.RawMessage(fmt.Sprintf(`{"type":"%s","value":"%s"}`, constants.Secp256k1PrivKeyType, base64.StdEncoding.EncodeToString(privKey.Bytes()))),
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
	realKeyPath := km.realKeyPath()
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

// validatorKeyPath returns where this node keeps the validator key: the key
// path, the .real stash left by a release, or the staged file of a received
// key, in that order. It errors when none holds it.
func (km *KeyManager) validatorKeyPath() (string, error) {
	for _, path := range []string{km.keyPath, km.realKeyPath(), km.stagedKeyPath()} {
		key, err := km.loadKeyAt(path)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return "", err
		}
		if km.IsValidatorKey(key) {
			return path, nil
		}
	}
	return "", fmt.Errorf("no validator key on this node")
}

// EncryptKeyToBytes encrypts the validator key for transfer, serving it from
// the key path, the .real stash of a released node, or the staged file.
func (km *KeyManager) EncryptKeyToBytes(secret string) ([]byte, error) {
	path, err := km.validatorKeyPath()
	if err != nil {
		return nil, err
	}
	keyData, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	encryptedBytes, err := crypto.Encrypt(keyData, secret)
	if err != nil {
		return nil, err
	}

	return encryptedBytes, nil
}

// StageKeyFromBytes decrypts a transferred key and writes it to the staged
// file only when it is the validator key. The key file the node reads is never
// touched; a takeover or failback promotes the staged key.
func (km *KeyManager) StageKeyFromBytes(data []byte, secret string) error {
	keyData, err := crypto.Decrypt(data, secret)
	if err != nil {
		return fmt.Errorf("failed to decrypt key: %w", err)
	}

	var key ValidatorKey
	if err := json.Unmarshal(keyData, &key); err != nil {
		return fmt.Errorf("invalid key data: %w", err)
	}
	if !km.IsValidatorKey(&key) {
		return fmt.Errorf("received key %s is not the validator key %s", key.Address, km.validatorAddress)
	}

	return writeKeyFile(km.stagedKeyPath(), keyData)
}

// PromoteStagedKey arms the key file with the staged validator key. When the
// key file already holds the validator key, the staged copy is removed and the
// key file is kept. Without a staged validator key it returns an error.
func (km *KeyManager) PromoteStagedKey() error {
	armed, err := km.HoldsValidatorKey()
	if err != nil {
		return fmt.Errorf("failed to inspect key file before promotion: %w", err)
	}
	if armed {
		if err := os.Remove(km.stagedKeyPath()); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("failed to remove staged key: %w", err)
		}
		return nil
	}

	staged, err := km.HoldsStagedValidatorKey()
	if err != nil {
		return fmt.Errorf("failed to inspect staged key: %w", err)
	}
	if !staged {
		return fmt.Errorf("no staged validator key to promote")
	}
	// The key file holds a mock here, so the rename replaces nothing of value.
	if err := os.Rename(km.stagedKeyPath(), km.keyPath); err != nil {
		return fmt.Errorf("failed to promote staged key: %w", err)
	}
	return nil
}

// StageFromRealKey copies the .real stash into the staged file when it holds
// the validator key, leaving .real in place. It returns an error otherwise.
func (km *KeyManager) StageFromRealKey() error {
	holds, err := km.HoldsRealValidatorKey()
	if err != nil {
		return fmt.Errorf("failed to inspect %s: %w", km.realKeyPath(), err)
	}
	if !holds {
		return fmt.Errorf("%s does not hold the validator key %s", km.realKeyPath(), km.validatorAddress)
	}

	keyData, err := os.ReadFile(km.realKeyPath())
	if err != nil {
		return fmt.Errorf("failed to read %s: %w", km.realKeyPath(), err)
	}
	return writeKeyFile(km.stagedKeyPath(), keyData)
}
