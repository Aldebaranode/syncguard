package crypto

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// Sign generates an HMAC-SHA256 signature for the given data
func Sign(data, secret string) string {
	h := hmac.New(sha256.New, []byte(secret))

	h.Write([]byte(data))
	return hex.EncodeToString(h.Sum(nil))
}

// Verify checks if the signature matches the data and secret
func Verify(data, signature, secret string) bool {
	expectedSig := Sign(data, secret)

	// Convert both to bytes for constant-time comparison (prevents timing attacks)
	sigBytes, _ := hex.DecodeString(signature)
	expectBytes, _ := hex.DecodeString(expectedSig)

	fmt.Printf("Verifying data: %s\n", data)
	fmt.Printf("Verifying signature: %s\n", signature)
	fmt.Printf("Verifying secret: %s\n", secret)
	fmt.Printf("Verifying expected signature: %s\n", expectedSig)
	fmt.Printf("Verifying result: %t\n", hmac.Equal(sigBytes, expectBytes))
	return hmac.Equal(sigBytes, expectBytes)
}
