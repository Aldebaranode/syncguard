package crypto

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"time"
)

// Sign generates an HMAC-SHA256 signature for the given data
func Sign(data, secret string) string {
	h := hmac.New(sha256.New, []byte(secret))

	h.Write([]byte(data))
	return hex.EncodeToString(h.Sum(nil))
}

// SignWithTimestamp generates an HMAC-SHA256 signature for the given data with timestamp
func SignWithTimestamp(data, secret string, timestamp int64) string {
	payload := data + strconv.FormatInt(timestamp, 10)
	return Sign(payload, secret)
}

// Verify checks if the signature matches the data and secret
func Verify(data, signature, secret string) bool {
	if data == "" || signature == "" || secret == "" {
		return false
	}

	expectedSig := Sign(data, secret)

	// Convert both to bytes for constant-time comparison (prevents timing attacks)
	sigBytes, err := hex.DecodeString(signature)
	if err != nil {
		return false
	}

	expectBytes, err := hex.DecodeString(expectedSig)
	if err != nil {
		return false
	}

	return hmac.Equal(sigBytes, expectBytes)
}

// VerifyTimedSignature checks if the signature matches the data and secret
func VerifyTimedSignature(data, signature, secret string, timestamp int64, timeoutMs int64) bool {

	if time.Since(time.Unix(timestamp, 0)).Milliseconds() > timeoutMs {
		return false
	}

	payload := data + strconv.FormatInt(timestamp, 10)
	return Verify(payload, signature, secret)
}
