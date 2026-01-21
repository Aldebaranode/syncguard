package state

import (
	"testing"
)

func TestDoubleSignProtector_CanSign(t *testing.T) {
	protector := NewDoubleSignProtector()
	defer protector.Stop()

	// First signature should be allowed
	canSign, err := protector.CanSign(1000, 0, 1)
	if !canSign || err != nil {
		t.Errorf("First signature should be allowed: canSign=%v, err=%v", canSign, err)
	}

	// Record it
	if err := protector.RecordSignature(1000, 0, 1); err != nil {
		t.Fatalf("Failed to record signature: %v", err)
	}

	// Same height/round/step should be rejected
	canSign, err = protector.CanSign(1000, 0, 1)
	if canSign {
		t.Error("Duplicate signature should be rejected")
	}
	if err == nil {
		t.Error("Should return error for duplicate signature")
	}

	// Higher height should be allowed
	canSign, err = protector.CanSign(1001, 0, 1)
	if !canSign || err != nil {
		t.Errorf("Higher height should be allowed: canSign=%v, err=%v", canSign, err)
	}

	// Lower height should be rejected
	canSign, err = protector.CanSign(999, 0, 1)
	if canSign {
		t.Error("Lower height signature should be rejected")
	}
}

func TestDoubleSignProtector_ValidStepProgression(t *testing.T) {
	protector := NewDoubleSignProtector()
	defer protector.Stop()

	// Sign step 1
	protector.RecordSignature(1000, 0, 1)

	// Step 2 at same height/round should be allowed (valid progression)
	canSign, err := protector.CanSign(1000, 0, 2)
	if !canSign || err != nil {
		t.Errorf("Valid step progression should be allowed: canSign=%v, err=%v", canSign, err)
	}
}
