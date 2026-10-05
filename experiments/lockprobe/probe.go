package main

import (
	"errors"
	"os"
	"strconv"
	"strings"
	"syscall"

	"github.com/aldebaranode/syncguard/internal/state"
)

// Outcomes of one probe.
const (
	outcomeRefused             = "refused"
	outcomeAcquiredAndReleased = "acquired_and_released"
	outcomePreconditionFailed  = "precondition_failed"
)

// probeResult is printed as one JSON object on stdout.
type probeResult struct {
	LockPath   string `json:"lock_path"`
	OwnerPID   int    `json:"owner_pid"`
	OwnerAlive bool   `json:"owner_alive"`
	Outcome    string `json:"outcome"`
	Error      string `json:"error"`
	// LockContentAfter is the lock file content after the attempt, empty when the file is gone.
	LockContentAfter string `json:"lock_content_after"`
}

// probe makes one AcquireLock call against statePath while another process
// holds its lock. It refuses to try when the lock file is missing or its owner
// is not running, so it never creates or breaks a lock on a live path. A lock
// it unexpectedly acquires is released at once.
func probe(statePath string) probeResult {
	lockPath := statePath + ".lock"
	result := probeResult{LockPath: lockPath}

	content, err := os.ReadFile(lockPath)
	if err != nil {
		return refuseToProbe(result, "no lock file: "+err.Error())
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(content)))
	if err != nil || pid <= 0 {
		return refuseToProbe(result, "lock file does not hold a PID")
	}
	result.OwnerPID = pid
	result.OwnerAlive = processAlive(pid)
	if !result.OwnerAlive {
		return refuseToProbe(result, "lock owner is not running")
	}

	manager := state.NewManager(statePath, "")
	if err := manager.AcquireLock(); err != nil {
		result.Outcome = outcomeRefused
		result.Error = err.Error()
		result.LockContentAfter = readLockContent(lockPath)
		return result
	}
	result.Outcome = outcomeAcquiredAndReleased
	if err := manager.ReleaseLock(); err != nil {
		result.Error = "release failed: " + err.Error()
	}
	result.LockContentAfter = readLockContent(lockPath)
	return result
}

// readLockContent returns the lock file content, or "" when it cannot be read.
func readLockContent(lockPath string) string {
	content, err := os.ReadFile(lockPath)
	if err != nil {
		return ""
	}
	return string(content)
}

// refuseToProbe marks the result as not attempted, with the reason.
func refuseToProbe(result probeResult, reason string) probeResult {
	result.Outcome = outcomePreconditionFailed
	result.Error = reason
	return result
}

// processAlive reports whether a process with this PID exists; EPERM means it
// exists but belongs to another user.
func processAlive(pid int) bool {
	err := syscall.Kill(pid, 0)
	return err == nil || errors.Is(err, syscall.EPERM)
}
