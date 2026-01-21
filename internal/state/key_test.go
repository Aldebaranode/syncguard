package state

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/constants"
	"github.com/aldebaranode/syncguard/internal/logger"
)

func newTestKeyManager(t *testing.T) *KeyManager {
	tmpDir, err := os.MkdirTemp("", "key_test")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	t.Cleanup(func() { os.RemoveAll(tmpDir) })

	keyPath := filepath.Join(tmpDir, "priv_validator_key.json")
	backupPath := filepath.Join(tmpDir, "backups")

	// Ensure backup dir exists
	os.MkdirAll(backupPath, 0700)

	// Create a dummy config for logger
	cfg := &config.Config{
		Node:    config.NodeConfig{ID: "test-node"},
		Logging: config.LoggingConfig{Verbose: false},
	}
	l := logger.NewLogger(cfg)
	l.WithModule("test-key")

	return NewKeyManager(keyPath, backupPath, l)
}

func TestKeyInitialization(t *testing.T) {
	km := newTestKeyManager(t)

	// 1. Test Key Generation
	err := km.InitializeKey()
	if err != nil {
		t.Fatalf("Failed to initialize key: %v", err)
	}

	if !km.HasKey() {
		t.Fatal("HasKey returned false after initialization")
	}

	// 2. Load and Verify Structure
	key, err := km.LoadKey()
	if err != nil {
		t.Fatalf("Failed to load key: %v", err)
	}

	// Validate Address (40 hex chars)
	if len(key.Address) != 40 {
		t.Errorf("Expected address length 40, got %d: %s", len(key.Address), key.Address)
	}

	// Validate PubKey Structure
	var pubKey struct {
		Type  string `json:"type"`
		Value string `json:"value"`
	}
	if err := json.Unmarshal(key.PubKey, &pubKey); err != nil {
		t.Fatalf("Failed to parse PubKey: %v", err)
	}
	if pubKey.Type != constants.Secp256k1PubKeyType {
		t.Errorf("Expected PubKey type %s, got %s", constants.Secp256k1PubKeyType, pubKey.Type)
	}
	if pubKey.Value == "" {
		t.Error("PubKey value is empty")
	}

	// Validate PrivKey Structure
	var privKey struct {
		Type  string `json:"type"`
		Value string `json:"value"`
	}
	if err := json.Unmarshal(key.PrivKey, &privKey); err != nil {
		t.Fatalf("Failed to parse PrivKey: %v", err)
	}
	if privKey.Type != constants.Secp256k1PrivKeyType {
		t.Errorf("Expected PrivKey type %s, got %s", constants.Secp256k1PrivKeyType, privKey.Type)
	}
	if privKey.Value == "" {
		t.Error("PrivKey value is empty")
	}
}

func TestEncryptedKeyTransfer(t *testing.T) {
	// Sender
	senderKM := newTestKeyManager(t)
	if err := senderKM.InitializeKey(); err != nil {
		t.Fatalf("Failed to init sender key: %v", err)
	}

	// Receiver
	receiverKM := newTestKeyManager(t)

	secret := "super-secret-password"

	// 1. Encrypt (Sender)
	encryptedData, err := senderKM.EncryptKeyToBytes(secret)
	if err != nil {
		t.Fatalf("Failed to encrypt key: %v", err)
	}

	if len(encryptedData) == 0 {
		t.Fatal("Encrypted data is empty")
	}

	// 2. Decrypt (Receiver)
	err = receiverKM.DecryptKeyFromBytes(encryptedData, secret)
	if err != nil {
		t.Fatalf("Failed to decrypt key: %v", err)
	}

	// 3. Verify Match
	senderKey, _ := senderKM.LoadKey()
	receiverKey, _ := receiverKM.LoadKey()

	senderJSON, _ := json.Marshal(senderKey)
	receiverJSON, _ := json.Marshal(receiverKey)

	if string(senderJSON) != string(receiverJSON) {
		t.Errorf("Key mismatch after transfer.\nSender: %s\nReceiver: %s", senderJSON, receiverJSON)
	}
}

func TestEncryptedKeyTransferFailures(t *testing.T) {
	km := newTestKeyManager(t)
	if err := km.InitializeKey(); err != nil {
		t.Fatalf("Failed to init key: %v", err)
	}
	secret := "correct-secret"

	encrypted, _ := km.EncryptKeyToBytes(secret)

	// Test 1: Wrong Secret
	err := km.DecryptKeyFromBytes(encrypted, "wrong-secret")
	if err == nil {
		t.Error("Expected error with wrong secret, got nil")
	}

	// Test 2: Corrupted Data
	encrypted[len(encrypted)-1] ^= 0xFF // Flip last bit
	err = km.DecryptKeyFromBytes(encrypted, secret)
	if err == nil {
		t.Error("Expected error with corrupted data, got nil")
	}
}
