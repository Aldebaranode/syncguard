package crypto

import (
	"bytes"
	"testing"
)

func TestCryptoLifeCycle(t *testing.T) {
	secret := "correct-horse-battery-staple"
	data := []byte("The nuclear launch codes are: 123456")

	// 1. Success Case
	encrypted, err := Encrypt(data, secret)
	if err != nil {
		t.Fatalf("Encryption failed: %v", err)
	}

	decrypted, err := Decrypt(encrypted, secret)
	if err != nil {
		t.Fatalf("Decryption failed: %v", err)
	}

	if !bytes.Equal(data, decrypted) {
		t.Errorf("Mismatch! Expected %s, got %s", data, decrypted)
	}
}

func TestSecurityFailures(t *testing.T) {
	secret := "correct-horse-battery-staple"
	data := []byte("Sensitive Data")

	encrypted, _ := Encrypt(data, secret)

	// 2. Wrong Password
	_, err := Decrypt(encrypted, "wrong-password")
	if err == nil {
		t.Error("Expected error for wrong password, got success")
	}

	// 3. Tampered Data (Bit Flip)
	// We flip the last byte (part of the Auth Tag or Ciphertext)
	tampered := make([]byte, len(encrypted))
	copy(tampered, encrypted)
	tampered[len(tampered)-1] ^= 0xFF

	_, err = Decrypt(tampered, secret)
	if err == nil {
		t.Error("Expected error for tampered data, got success")
	}
}
