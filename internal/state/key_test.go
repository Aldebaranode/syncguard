package state

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/constants"
	"github.com/aldebaranode/syncguard/internal/logger"
)

// newKeyManagerWithAddress builds a key manager in a fresh temp dir that
// protects the given validator address. It writes no key.
func newKeyManagerWithAddress(t *testing.T, validatorAddress string) *KeyManager {
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

	return NewKeyManager(keyPath, backupPath, validatorAddress, l)
}

// newTestKeyManager returns a key manager that already holds a generated
// validator key and is configured with that key's address.
func newTestKeyManager(t *testing.T) *KeyManager {
	t.Helper()
	generator := newKeyManagerWithAddress(t, "")
	if err := generator.InitializeKey(); err != nil {
		t.Fatalf("failed to generate key: %v", err)
	}
	key, err := generator.LoadKey()
	if err != nil {
		t.Fatalf("failed to load generated key: %v", err)
	}
	generator.validatorAddress = key.Address
	return generator
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
	senderKey0, _ := senderKM.LoadKey()
	receiverKM := newKeyManagerWithAddress(t, senderKey0.Address)

	secret := "super-secret-password"

	// 1. Encrypt (Sender)
	encryptedData, err := senderKM.EncryptKeyToBytes(secret)
	if err != nil {
		t.Fatalf("Failed to encrypt key: %v", err)
	}

	if len(encryptedData) == 0 {
		t.Fatal("Encrypted data is empty")
	}

	// 2. Decrypt and stage (Receiver)
	err = receiverKM.StageKeyFromBytes(encryptedData, secret)
	if err != nil {
		t.Fatalf("Failed to decrypt key: %v", err)
	}

	// 3. Verify Match
	senderKey, _ := senderKM.LoadKey()
	receiverKey := readKeyFile(t, receiverKM.keyPath+".staged")

	senderJSON, _ := json.Marshal(senderKey)
	receiverJSON, _ := json.Marshal(receiverKey)

	if string(senderJSON) != string(receiverJSON) {
		t.Errorf("Key mismatch after transfer.\nSender: %s\nReceiver: %s", senderJSON, receiverJSON)
	}
}

// TestDeleteKeyDisablesSigning verifies that disabling the key swaps in a
// throwaway key with a DIFFERENT address (so the node can no longer sign as the
// real validator) while preserving the real key for later restore. This is the
// core double-signing safeguard: the real identity must not remain on a node
// that has handed it off to a peer.
func TestDeleteKeyDisablesSigning(t *testing.T) {
	km := newTestKeyManager(t)
	if err := km.InitializeKey(); err != nil {
		t.Fatalf("Failed to init key: %v", err)
	}

	realKey, _ := km.LoadKey()

	if err := km.DeleteKey(); err != nil {
		t.Fatalf("DeleteKey failed: %v", err)
	}

	// The active key must now have a different address (cannot sign as real validator).
	mockKey, err := km.LoadKey()
	if err != nil {
		t.Fatalf("Failed to load key after delete: %v", err)
	}
	if mockKey.Address == realKey.Address {
		t.Fatalf("mock key shares the real address %s: signing NOT disabled (double-sign risk)", realKey.Address)
	}

	// The real key must be preserved so failback can restore it.
	restored, err := os.ReadFile(km.keyPath + ".real")
	if err != nil {
		t.Fatalf("real key not preserved at .real: %v", err)
	}
	var preserved ValidatorKey
	if err := json.Unmarshal(restored, &preserved); err != nil {
		t.Fatalf("preserved real key unparsable: %v", err)
	}
	if preserved.Address != realKey.Address {
		t.Fatalf("preserved key address %s != original %s", preserved.Address, realKey.Address)
	}

	// RestoreKey must bring back the real identity.
	if err := km.RestoreKey(); err != nil {
		t.Fatalf("RestoreKey failed: %v", err)
	}
	back, _ := km.LoadKey()
	if back.Address != realKey.Address {
		t.Fatalf("after restore, address %s != original %s", back.Address, realKey.Address)
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
	err := km.StageKeyFromBytes(encrypted, "wrong-secret")
	if err == nil {
		t.Error("Expected error with wrong secret, got nil")
	}

	// Test 2: Corrupted Data
	encrypted[len(encrypted)-1] ^= 0xFF // Flip last bit
	err = km.StageKeyFromBytes(encrypted, secret)
	if err == nil {
		t.Error("Expected error with corrupted data, got nil")
	}
}

// TestStageKeyFromBytesRejectsNonValidatorKey verifies that a received key
// with a different address is refused, the key file is untouched, and no
// staged file is written.
func TestStageKeyFromBytesRejectsNonValidatorKey(t *testing.T) {
	km := newTestKeyManager(t)
	before, err := os.ReadFile(km.keyPath)
	if err != nil {
		t.Fatalf("failed to read key file: %v", err)
	}

	other := newTestKeyManager(t)
	ciphertext, err := other.EncryptKeyToBytes("secret")
	if err != nil {
		t.Fatalf("failed to encrypt other key: %v", err)
	}

	err = km.StageKeyFromBytes(ciphertext, "secret")
	want := "received key " + other.validatorAddress + " is not the validator key " + km.validatorAddress
	if err == nil || err.Error() != want {
		t.Fatalf("error = %v, want %q", err, want)
	}

	after, err := os.ReadFile(km.keyPath)
	if err != nil {
		t.Fatalf("failed to read key file after: %v", err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("key file changed after a rejected key")
	}
	if _, err := os.Stat(km.keyPath + ".staged"); !os.IsNotExist(err) {
		t.Fatalf("staged file written for a rejected key, stat err = %v", err)
	}
}

// readKeyFile parses the key stored at path.
func readKeyFile(t *testing.T, path string) ValidatorKey {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("failed to read %s: %v", path, err)
	}
	var key ValidatorKey
	if err := json.Unmarshal(data, &key); err != nil {
		t.Fatalf("failed to parse %s: %v", path, err)
	}
	return key
}

// TestDeleteKeyKeepsStashedValidatorKey verifies that disarming twice never
// overwrites the stashed validator key or its backup with a mock key.
func TestDeleteKeyKeepsStashedValidatorKey(t *testing.T) {
	km := newTestKeyManager(t)

	for i := 0; i < 2; i++ {
		if err := km.DeleteKey(); err != nil {
			t.Fatalf("DeleteKey #%d failed: %v", i+1, err)
		}
	}

	real := readKeyFile(t, km.keyPath+".real")
	if real.Address != km.validatorAddress {
		t.Fatalf(".real holds %s, want validator key %s", real.Address, km.validatorAddress)
	}
	backup := readKeyFile(t, filepath.Join(km.backupPath, "priv_validator_key.json.bak"))
	if backup.Address != km.validatorAddress {
		t.Fatalf(".bak holds %s, want validator key %s", backup.Address, km.validatorAddress)
	}
}

// TestEncryptKeyToBytesServesStashedValidatorKey verifies that a released node
// still serves the validator key from .real.
func TestEncryptKeyToBytesServesStashedValidatorKey(t *testing.T) {
	km := newTestKeyManager(t)
	if err := km.DeleteKey(); err != nil {
		t.Fatalf("DeleteKey failed: %v", err)
	}

	ciphertext, err := km.EncryptKeyToBytes("secret")
	if err != nil {
		t.Fatalf("EncryptKeyToBytes failed: %v", err)
	}

	receiver := newKeyManagerWithAddress(t, km.validatorAddress)
	if err := receiver.StageKeyFromBytes(ciphertext, "secret"); err != nil {
		t.Fatalf("receiver rejected the served key: %v", err)
	}
	got := readKeyFile(t, receiver.keyPath+".staged")
	if got.Address != km.validatorAddress {
		t.Fatalf("served key address %s, want %s", got.Address, km.validatorAddress)
	}
}

// newReceiverWithMockKey returns a key manager that protects a fresh sender's
// validator key and holds a mock key at keyPath, plus that validator key
// encrypted with secret.
func newReceiverWithMockKey(t *testing.T, secret string) (*KeyManager, []byte) {
	t.Helper()
	sender := newTestKeyManager(t)
	ciphertext, err := sender.EncryptKeyToBytes(secret)
	if err != nil {
		t.Fatalf("failed to encrypt sender key: %v", err)
	}
	receiver := newKeyManagerWithAddress(t, sender.validatorAddress)
	if err := receiver.InitializeKey(); err != nil {
		t.Fatalf("failed to create mock key: %v", err)
	}
	return receiver, ciphertext
}

// TestStageKeyFromBytesWritesOnlyStagedFile verifies that a received validator
// key lands in the staged file with mode 0600 and the key file the node reads
// stays byte-identical.
func TestStageKeyFromBytesWritesOnlyStagedFile(t *testing.T) {
	km, ciphertext := newReceiverWithMockKey(t, "secret")
	before, err := os.ReadFile(km.keyPath)
	if err != nil {
		t.Fatalf("failed to read key file: %v", err)
	}

	if err := km.StageKeyFromBytes(ciphertext, "secret"); err != nil {
		t.Fatalf("StageKeyFromBytes failed: %v", err)
	}

	after, err := os.ReadFile(km.keyPath)
	if err != nil {
		t.Fatalf("failed to read key file after: %v", err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("staging changed the key file the node reads")
	}
	if staged := readKeyFile(t, km.keyPath+".staged"); staged.Address != km.validatorAddress {
		t.Fatalf("staged file holds %s, want validator key %s", staged.Address, km.validatorAddress)
	}
	info, err := os.Stat(km.keyPath + ".staged")
	if err != nil {
		t.Fatalf("failed to stat staged file: %v", err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatalf("staged file mode %v, want 0600", info.Mode().Perm())
	}
	if holds, err := km.HoldsStagedValidatorKey(); err != nil || !holds {
		t.Fatalf("HoldsStagedValidatorKey = %v, %v; want true, nil", holds, err)
	}
	if holds, err := km.HoldsValidatorKey(); err != nil || holds {
		t.Fatalf("HoldsValidatorKey = %v, %v; want false, nil", holds, err)
	}
}

// TestPromoteStagedKeyMovesKeyIntoPlace verifies that promotion moves a staged
// validator key over the mock at keyPath, keeps an already armed keyPath, and
// fails without a staged validator key.
func TestPromoteStagedKeyMovesKeyIntoPlace(t *testing.T) {
	t.Run("staged key replaces mock", func(t *testing.T) {
		km, ciphertext := newReceiverWithMockKey(t, "secret")
		if err := km.StageKeyFromBytes(ciphertext, "secret"); err != nil {
			t.Fatalf("StageKeyFromBytes failed: %v", err)
		}

		if err := km.PromoteStagedKey(); err != nil {
			t.Fatalf("PromoteStagedKey failed: %v", err)
		}

		if got := readKeyFile(t, km.keyPath); got.Address != km.validatorAddress {
			t.Fatalf("key file holds %s, want validator key %s", got.Address, km.validatorAddress)
		}
		if _, err := os.Stat(km.keyPath + ".staged"); !os.IsNotExist(err) {
			t.Fatalf("staged file left after promotion, stat err = %v", err)
		}
	})

	t.Run("key file already holds validator key", func(t *testing.T) {
		km := newTestKeyManager(t)
		ciphertext, err := km.EncryptKeyToBytes("secret")
		if err != nil {
			t.Fatalf("failed to encrypt key: %v", err)
		}
		if err := km.StageKeyFromBytes(ciphertext, "secret"); err != nil {
			t.Fatalf("StageKeyFromBytes failed: %v", err)
		}
		before, err := os.ReadFile(km.keyPath)
		if err != nil {
			t.Fatalf("failed to read key file: %v", err)
		}

		if err := km.PromoteStagedKey(); err != nil {
			t.Fatalf("PromoteStagedKey failed: %v", err)
		}

		after, err := os.ReadFile(km.keyPath)
		if err != nil {
			t.Fatalf("failed to read key file after: %v", err)
		}
		if !bytes.Equal(before, after) {
			t.Fatal("promotion changed a key file that already held the validator key")
		}
		if _, err := os.Stat(km.keyPath + ".staged"); !os.IsNotExist(err) {
			t.Fatalf("staged file left after promotion, stat err = %v", err)
		}
	})

	t.Run("no staged key", func(t *testing.T) {
		km, _ := newReceiverWithMockKey(t, "secret")
		before, err := os.ReadFile(km.keyPath)
		if err != nil {
			t.Fatalf("failed to read key file: %v", err)
		}

		if err := km.PromoteStagedKey(); err == nil {
			t.Fatal("expected an error without a staged validator key, got nil")
		}

		after, err := os.ReadFile(km.keyPath)
		if err != nil {
			t.Fatalf("failed to read key file after: %v", err)
		}
		if !bytes.Equal(before, after) {
			t.Fatal("failed promotion changed the key file")
		}
	})
}

// TestEncryptKeyToBytesServesStagedValidatorKey verifies that a passive node
// whose only validator key is staged still serves it to the peer.
func TestEncryptKeyToBytesServesStagedValidatorKey(t *testing.T) {
	km, ciphertext := newReceiverWithMockKey(t, "secret")
	if err := km.StageKeyFromBytes(ciphertext, "secret"); err != nil {
		t.Fatalf("StageKeyFromBytes failed: %v", err)
	}

	served, err := km.EncryptKeyToBytes("secret")
	if err != nil {
		t.Fatalf("EncryptKeyToBytes failed: %v", err)
	}

	receiver := newKeyManagerWithAddress(t, km.validatorAddress)
	if err := receiver.StageKeyFromBytes(served, "secret"); err != nil {
		t.Fatalf("receiver rejected the served key: %v", err)
	}
	if got := readKeyFile(t, receiver.keyPath+".staged"); got.Address != km.validatorAddress {
		t.Fatalf("served key address %s, want %s", got.Address, km.validatorAddress)
	}
}

// TestStageFromRealKey verifies that the .real stash of a released node is
// copied into the staged file, leaving .real and the mock at keyPath in place,
// and that staging fails when .real does not hold the validator key.
func TestStageFromRealKey(t *testing.T) {
	t.Run("real stash holds validator key", func(t *testing.T) {
		km := newTestKeyManager(t)
		if err := km.DeleteKey(); err != nil {
			t.Fatalf("DeleteKey failed: %v", err)
		}
		mockBefore, err := os.ReadFile(km.keyPath)
		if err != nil {
			t.Fatalf("failed to read key file: %v", err)
		}
		if holds, err := km.HoldsRealValidatorKey(); err != nil || !holds {
			t.Fatalf("HoldsRealValidatorKey = %v, %v; want true, nil", holds, err)
		}

		if err := km.StageFromRealKey(); err != nil {
			t.Fatalf("StageFromRealKey failed: %v", err)
		}

		if got := readKeyFile(t, km.keyPath+".staged"); got.Address != km.validatorAddress {
			t.Fatalf("staged file holds %s, want validator key %s", got.Address, km.validatorAddress)
		}
		if got := readKeyFile(t, km.keyPath+".real"); got.Address != km.validatorAddress {
			t.Fatalf(".real holds %s after staging, want validator key %s", got.Address, km.validatorAddress)
		}
		mockAfter, err := os.ReadFile(km.keyPath)
		if err != nil {
			t.Fatalf("failed to read key file after: %v", err)
		}
		if !bytes.Equal(mockBefore, mockAfter) {
			t.Fatal("staging from .real changed the key file the node reads")
		}
	})

	t.Run("no real stash", func(t *testing.T) {
		km, _ := newReceiverWithMockKey(t, "secret")
		if holds, err := km.HoldsRealValidatorKey(); err != nil || holds {
			t.Fatalf("HoldsRealValidatorKey = %v, %v; want false, nil", holds, err)
		}

		if err := km.StageFromRealKey(); err == nil {
			t.Fatal("expected an error without a validator key in .real, got nil")
		}
		if _, err := os.Stat(km.keyPath + ".staged"); !os.IsNotExist(err) {
			t.Fatalf("staged file written without a validator key in .real, stat err = %v", err)
		}
	})
}
