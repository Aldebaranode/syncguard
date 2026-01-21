package crypto

import (
	"fmt"
	"testing"
)

func TestAuthLifecycle(t *testing.T) {
	secret := "my-cluster-secret"
	data := "POST /validator_key 1731234567"

	result := Sign(data, secret)
	fmt.Printf("Result: %s\n", result)

	Verify(data, result, secret)
}
