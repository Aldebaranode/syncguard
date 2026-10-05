package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"testing"
)

// TestProbeRefusedWhileOwnerAlive verifies that a second acquisition fails
// while a live process owns the lock, and that the lock file is untouched.
func TestProbeRefusedWhileOwnerAlive(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "priv_validator_state.json")
	owner := []byte(strconv.Itoa(os.Getpid()) + "\n")
	writeLock(t, statePath, owner)

	result := probe(statePath)

	if result.Outcome != outcomeRefused {
		t.Fatalf("outcome = %q (%s), want %q", result.Outcome, result.Error, outcomeRefused)
	}
	if result.Error != "state is already locked" {
		t.Fatalf("error = %q, want %q", result.Error, "state is already locked")
	}
	if !result.OwnerAlive || result.OwnerPID != os.Getpid() {
		t.Fatalf("owner = %d alive=%v, want %d alive", result.OwnerPID, result.OwnerAlive, os.Getpid())
	}
	assertLockContent(t, statePath, owner)
	if result.LockContentAfter != string(owner) {
		t.Fatalf("lock_content_after = %q, want %q", result.LockContentAfter, owner)
	}
}

// TestProbeDoesNotRunWithoutLock verifies that the probe never creates a lock
// on a path nobody holds.
func TestProbeDoesNotRunWithoutLock(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "priv_validator_state.json")

	result := probe(statePath)

	if result.Outcome != outcomePreconditionFailed {
		t.Fatalf("outcome = %q, want %q", result.Outcome, outcomePreconditionFailed)
	}
	if _, err := os.Stat(statePath + ".lock"); !os.IsNotExist(err) {
		t.Fatalf("probe created a lock file: %v", err)
	}
}

// TestProbeDoesNotBreakStaleLock verifies that a lock left by an exited
// process is reported, not reclaimed.
func TestProbeDoesNotBreakStaleLock(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "priv_validator_state.json")
	owner := []byte(strconv.Itoa(exitedPID(t)) + "\n")
	writeLock(t, statePath, owner)

	result := probe(statePath)

	if result.Outcome != outcomePreconditionFailed || result.OwnerAlive {
		t.Fatalf("outcome = %q alive=%v, want %q and not alive", result.Outcome, result.OwnerAlive, outcomePreconditionFailed)
	}
	assertLockContent(t, statePath, owner)
}

// writeLock plants a lock file with the given content.
func writeLock(t *testing.T, statePath string, content []byte) {
	t.Helper()
	if err := os.WriteFile(statePath+".lock", content, 0600); err != nil {
		t.Fatalf("failed to plant lock: %v", err)
	}
}

// assertLockContent fails the test when the lock file is missing or changed.
func assertLockContent(t *testing.T, statePath string, want []byte) {
	t.Helper()
	got, err := os.ReadFile(statePath + ".lock")
	if err != nil {
		t.Fatalf("lock file missing after probe: %v", err)
	}
	if string(got) != string(want) {
		t.Fatalf("lock content = %q, want %q", got, want)
	}
}

// exitedPID runs a short process to completion and returns its PID.
func exitedPID(t *testing.T) int {
	t.Helper()
	cmd := exec.Command("true")
	if err := cmd.Run(); err != nil {
		t.Fatalf("failed to run true: %v", err)
	}
	return cmd.ProcessState.Pid()
}
