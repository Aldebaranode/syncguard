package crypto

import (
	"fmt"
	"testing"
	"time"
)

func TestAuthValidSignature(t *testing.T) {
	secret := "my-cluster-secret"
	data := "POST /validator_key 1731234567"

	result := Sign(data, secret)

	if !Verify(data, result, secret) {
		t.Error("Signature verification failed")
	}
}

func TestAuthInvalidSignature_InvalidSecret(t *testing.T) {
	secret := "my-cluster-secret"
	data := "POST /validator_key 1731234567"

	result := Sign(data, secret)

	invalidSecret := "invalid-secret"
	if Verify(data, result, invalidSecret) {
		t.Error("Expected verification to fail for invalid secret")
	}
}

func TestAuthInvalidSignature_InvalidSignature(t *testing.T) {
	secret := "my-cluster-secret"
	data := "POST /validator_key 1731234567"

	invalidSignature := "invalid-signature"
	if Verify(data, invalidSignature, secret) {
		t.Error("Expected verification to fail for invalid signature")
	}
}

func TestAuthInvalidSignature_InvalidData(t *testing.T) {
	secret := "my-cluster-secret"
	data := "POST /validator_key 1731234567"

	result := Sign(data, secret)

	invalidData := "invalid-data"
	if Verify(invalidData, result, secret) {
		t.Error("Expected verification to fail for invalid data")
	}
}

func TestAuthInvalidSignature_EmptyStrings(t *testing.T) {
	secret := ""
	data := ""

	result := Sign(data, secret)

	fmt.Printf("signature for empty secret and data %s\n", result)

	if Verify(data, result, secret) {
		t.Error("Expected verification to fail for empty strings")
	}
}

// TestVerifyTimedSignatureRejectsFutureTimestamp verifies that a timestamp
// further in the future than the timeout is rejected, so a request signed
// ahead of time cannot be replayed until that time arrives.
func TestVerifyTimedSignatureRejectsFutureTimestamp(t *testing.T) {
	secret := "my-cluster-secret"
	payload := "SYNCGUARD_VALIDATOR_KEY"
	timeoutMs := int64(30000)

	now := time.Now().Unix()
	if !VerifyTimedSignature(payload, SignWithTimestamp(payload, secret, now), secret, now, timeoutMs) {
		t.Fatal("a current timestamp must be accepted")
	}

	future := time.Now().Add(time.Hour).Unix()
	if VerifyTimedSignature(payload, SignWithTimestamp(payload, secret, future), secret, future, timeoutMs) {
		t.Fatal("a timestamp one hour in the future must be rejected")
	}
}
