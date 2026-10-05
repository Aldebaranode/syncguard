package manager

import (
	"errors"
	"fmt"
)

// TakeOver makes this passive node the active validator after the peer failed
// over to it. The staged validator key is armed immediately before the
// restart. A refusal leaves the key file disarmed and the staged key in place:
// it is inert and lets a later failback recover the key from this node.
func (fm *FailoverManager) TakeOver() error {
	fm.transitionMu.Lock()
	defer fm.transitionMu.Unlock()

	if fm.IsActive() {
		return nil
	}

	if !fm.healthChecker.IsHealthy() {
		fm.logger.Warn("Refusing takeover: local node is not healthy")
		return newTransitionError(CodeNotHealthy, errors.New("local node is not healthy"))
	}

	holdsKey, err := fm.holdsValidatorKeyToArm()
	if err != nil {
		return newTransitionError(CodeNoValidatorKey, fmt.Errorf("failed to inspect keys: %w", err))
	}
	if !holdsKey {
		return newTransitionError(CodeNoValidatorKey, errors.New("neither the key file nor the staged file holds the validator key"))
	}

	if err := fm.stateManager.AcquireLock(); err != nil {
		return newTransitionError(CodeLockUnavailable, err)
	}

	if err := fm.keyManager.PromoteStagedKey(); err != nil {
		fm.releaseLock()
		return newTransitionError(CodeNoValidatorKey, err)
	}

	fm.logger.Info("Taking over validator duties")
	// Opened before the restart so the node's boot time is never counted as a
	// failure (prevents the failover ping-pong).
	fm.BeginWarmup()

	if fm.nodeManager != nil {
		if err := fm.nodeManager.Restart(); err != nil {
			fm.abortTakeover()
			return newTransitionError(CodeRestartFailed, err)
		}
	}

	fm.nodeMayRunValidatorKey = false
	fm.SetActive(true)
	fm.logger.Info("Successfully took over as active validator")
	return nil
}

// abortTakeover undoes a takeover whose restart failed: it disarms the key
// file, stops the node, releases the lock, and closes the warm-up window. A
// node that cannot be confirmed stopped is recorded as possibly running with
// the validator key, so a failback release restarts it before answering.
func (fm *FailoverManager) abortTakeover() {
	if err := fm.keyManager.DeleteKey(); err != nil {
		fm.logger.Error("Failed to disarm key after failed takeover restart: %v", err)
	}
	if fm.nodeManager != nil && !fm.stopNodeConfirmed() {
		fm.nodeMayRunValidatorKey = true
		fm.logger.Error("Node still running after failed takeover restart; it may be signing with the validator key")
	}
	fm.releaseLock()
	fm.endWarmup()
}

// ReleaseForFailback stops this node from signing so the primary can fail
// back. It returns nil without action only when the node is passive, its key
// file does not hold the validator key, and its process is not suspected of
// running with it; otherwise the node restarts onto a mock key first.
func (fm *FailoverManager) ReleaseForFailback() error {
	fm.transitionMu.Lock()
	defer fm.transitionMu.Unlock()

	holdsKey, err := fm.keyManager.HoldsValidatorKey()
	if err != nil {
		return newTransitionError(CodeDisarmFailed, fmt.Errorf("failed to inspect key file: %w", err))
	}
	if !fm.IsActive() && !holdsKey && !fm.nodeMayRunValidatorKey {
		return nil
	}

	fm.logger.Info("Releasing validator duties for failback")

	if err := fm.keyManager.DeleteKey(); err != nil {
		return newTransitionError(CodeDisarmFailed, err)
	}

	if fm.nodeManager != nil {
		if err := fm.nodeManager.Restart(); err != nil {
			// The running node may still hold the validator key, so a key file
			// this call disarmed goes back to match it.
			if holdsKey {
				if restoreErr := fm.keyManager.RestoreKey(); restoreErr != nil {
					fm.logger.Error("Failed to restore key after failed release: %v", restoreErr)
				}
			}
			return newTransitionError(CodeRestartFailed, err)
		}
	}

	fm.nodeMayRunValidatorKey = false
	fm.releaseLock()
	fm.setPassive()
	fm.logger.Info("Successfully released validator duties")
	return nil
}
