package manager

import (
	"errors"
	"net/http"
	"reflect"
	"testing"
	"time"

	"github.com/aldebaranode/syncguard/internal/state"
)

// requireTransitionError fails the test unless err is a *TransitionError with
// the given code and status.
func requireTransitionError(t *testing.T, err error, code string, status int) {
	t.Helper()
	var transitionErr *TransitionError
	if !errors.As(err, &transitionErr) {
		t.Fatalf("error = %v, want a *TransitionError with code %s", err, code)
	}
	if transitionErr.Code != code || transitionErr.Status != status {
		t.Fatalf("transition error = %s/%d, want %s/%d", transitionErr.Code, transitionErr.Status, code, status)
	}
}

// warmupOpen reports whether the post-restart warm-up window is open.
func warmupOpen(fm *FailoverManager) bool {
	fm.mu.RLock()
	defer fm.mu.RUnlock()
	return time.Now().Before(fm.warmupUntil)
}

// TestTakeOverPromotesStagedKeyAndGoesActive verifies that a takeover arms the
// key file with the staged validator key for the restart and goes active
// under the state lock and the warm-up window.
func TestTakeOverPromotesStagedKeyAndGoesActive(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	node := &recordingNode{log: events}
	f := newNodeFixture(t, "", validatorAddress, node)
	f.stage(t, sealed)

	if err := f.fm.TakeOver(); err != nil {
		t.Fatalf("TakeOver failed: %v", err)
	}

	if got := events.list(); !reflect.DeepEqual(got, []string{"local restart"}) {
		t.Fatalf("events = %v, want one local restart", got)
	}
	if !reflect.DeepEqual(node.restartKeys, []string{validatorAddress}) {
		t.Fatalf("node restarted onto %v, want the validator key %s", node.restartKeys, validatorAddress)
	}
	if !f.fm.IsActive() {
		t.Fatal("node must be active after the takeover")
	}
	if f.stagedFileExists(t) {
		t.Fatal("staged file left after the takeover promoted it")
	}
	if !f.lockHeld(t) {
		t.Fatal("active node must hold the state lock")
	}
	if !warmupOpen(f.fm) {
		t.Fatal("warm-up window must be open after the takeover restart")
	}
}

// TestTakeOverRefusesWithoutValidatorKey verifies that a node with neither a
// staged nor an armed validator key refuses, does not restart, and stays
// passive.
func TestTakeOverRefusesWithoutValidatorKey(t *testing.T) {
	validatorAddress, _ := newValidatorKeySource(t)
	events := &eventLog{}
	f := newNodeFixture(t, "", validatorAddress, &recordingNode{log: events})

	err := f.fm.TakeOver()

	requireTransitionError(t, err, "no_validator_key", http.StatusConflict)
	if got := events.list(); len(got) != 0 {
		t.Fatalf("events = %v, want none", got)
	}
	if f.fm.IsActive() {
		t.Fatal("node must stay passive without the validator key")
	}
	if f.lockHeld(t) {
		t.Fatal("a refused takeover must not keep the state lock")
	}
}

// TestTakeOverRefusesWhenUnhealthy verifies that an unhealthy node refuses
// with not_healthy, does not restart, and keeps the staged key inert.
func TestTakeOverRefusesWhenUnhealthy(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	f := newNodeFixture(t, "", validatorAddress, &recordingNode{log: events})
	f.stage(t, sealed)
	f.fm.healthChecker = &fakeHealthChecker{cachedHealthy: false}

	err := f.fm.TakeOver()

	requireTransitionError(t, err, "not_healthy", http.StatusConflict)
	if got := events.list(); len(got) != 0 {
		t.Fatalf("events = %v, want none", got)
	}
	if f.fm.IsActive() {
		t.Fatal("an unhealthy node must stay passive")
	}
	if got := readAddress(t, f.keyPath); got == validatorAddress {
		t.Fatal("key file armed by a refused takeover")
	}
	if !f.stagedFileExists(t) {
		t.Fatal("a refused takeover must leave the staged key in place")
	}
}

// TestTakeOverRefusesWhenLockUnavailable verifies that a node that cannot take
// the state lock refuses with lock_unavailable before arming its key file.
func TestTakeOverRefusesWhenLockUnavailable(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	f := newNodeFixture(t, "", validatorAddress, &recordingNode{log: events})
	f.stage(t, sealed)
	otherOwner := state.NewManager(f.statePath, "")
	if err := otherOwner.AcquireLock(); err != nil {
		t.Fatalf("failed to take the lock for another owner: %v", err)
	}
	defer otherOwner.ReleaseLock()

	err := f.fm.TakeOver()

	requireTransitionError(t, err, "lock_unavailable", http.StatusConflict)
	if got := events.list(); len(got) != 0 {
		t.Fatalf("events = %v, want none", got)
	}
	if got := readAddress(t, f.keyPath); got == validatorAddress {
		t.Fatal("key file armed by a refused takeover")
	}
	if f.fm.IsActive() {
		t.Fatal("node must stay passive without the lock")
	}
}

// TestTakeOverRestartFailureDisarmsStopsAndReleases verifies that a failed
// takeover restart disarms the key file, stops the node, releases the lock,
// closes the warm-up window, and stays passive, without losing the key.
func TestTakeOverRestartFailureDisarmsStopsAndReleases(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	f := newNodeFixture(t, "", validatorAddress, &recordingNode{log: events, failRestarts: 1})
	f.stage(t, sealed)

	err := f.fm.TakeOver()

	requireTransitionError(t, err, "restart_failed", http.StatusInternalServerError)
	if got := events.list(); !reflect.DeepEqual(got, []string{"local restart", "local stop"}) {
		t.Fatalf("events = %v, want a restart then a stop", got)
	}
	if got := readAddress(t, f.keyPath); got == validatorAddress {
		t.Fatal("key file still holds the validator key after a failed takeover")
	}
	if got := readAddress(t, f.keyPath+".real"); got != validatorAddress {
		t.Fatalf(".real holds %s, want validator key %s", got, validatorAddress)
	}
	if f.lockHeld(t) {
		t.Fatal("a failed takeover must release the state lock")
	}
	if f.fm.IsActive() {
		t.Fatal("node must stay passive after a failed takeover")
	}
	if warmupOpen(f.fm) {
		t.Fatal("a failed takeover must close the warm-up window")
	}
}

// TestReleaseForFailbackPassiveHoldingKeyRestartsOntoMock verifies that a
// passive node whose key file holds the validator key still releases it: it
// restarts onto a mock key instead of answering at once.
func TestReleaseForFailbackPassiveHoldingKeyRestartsOntoMock(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	node := &recordingNode{log: events}
	f := newNodeFixture(t, "", validatorAddress, node)
	f.arm(t, sealed)

	if err := f.fm.ReleaseForFailback(); err != nil {
		t.Fatalf("ReleaseForFailback failed: %v", err)
	}

	if len(node.restartKeys) != 1 || node.restartKeys[0] == validatorAddress {
		t.Fatalf("node restarted onto %v, want one restart onto a mock key", node.restartKeys)
	}
	if got := readAddress(t, f.keyPath); got == validatorAddress {
		t.Fatal("key file still holds the validator key after the release")
	}
	if got := readAddress(t, f.keyPath+".real"); got != validatorAddress {
		t.Fatalf(".real holds %s, want validator key %s", got, validatorAddress)
	}
	if f.fm.IsActive() {
		t.Fatal("node must be passive after the release")
	}
}

// TestReleaseForFailbackSuccessReleasesLock verifies that an active node
// restarts onto a mock key, releases the state lock, and goes passive.
func TestReleaseForFailbackSuccessReleasesLock(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	node := &recordingNode{log: events}
	f := newNodeFixture(t, "", validatorAddress, node)
	f.activate(t, sealed)

	if err := f.fm.ReleaseForFailback(); err != nil {
		t.Fatalf("ReleaseForFailback failed: %v", err)
	}

	if len(node.restartKeys) != 1 || node.restartKeys[0] == validatorAddress {
		t.Fatalf("node restarted onto %v, want one restart onto a mock key", node.restartKeys)
	}
	if f.lockHeld(t) {
		t.Fatal("released node must not hold the state lock")
	}
	if f.fm.IsActive() {
		t.Fatal("node must be passive after the release")
	}
}

// TestReleaseForFailbackDisarmFailureDoesNotRestart verifies that a release
// whose disarm fails answers disarm_failed without restarting or changing
// role.
func TestReleaseForFailbackDisarmFailureDoesNotRestart(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	f := newNodeFixture(t, "", validatorAddress, &recordingNode{log: events})
	f.activate(t, sealed)
	f.breakBackupPath(t)

	err := f.fm.ReleaseForFailback()

	requireTransitionError(t, err, "disarm_failed", http.StatusInternalServerError)
	if got := events.list(); len(got) != 0 {
		t.Fatalf("events = %v, want none", got)
	}
	if !f.fm.IsActive() {
		t.Fatal("node must stay active when it could not disarm")
	}
	if got := readAddress(t, f.keyPath); got != validatorAddress {
		t.Fatalf("key file holds %s, want validator key %s", got, validatorAddress)
	}
	if !f.lockHeld(t) {
		t.Fatal("node that stays active must keep the state lock")
	}
}

// TestReleaseForFailbackRestartFailureRestoresKey verifies that when the
// release restart fails the key file is restored to match the process that
// may still run with it, and the node stays active.
func TestReleaseForFailbackRestartFailureRestoresKey(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	f := newNodeFixture(t, "", validatorAddress, &recordingNode{log: events, failRestarts: 1})
	f.activate(t, sealed)

	err := f.fm.ReleaseForFailback()

	requireTransitionError(t, err, "restart_failed", http.StatusInternalServerError)
	if !f.fm.IsActive() {
		t.Fatal("node must stay active when it could not stop signing")
	}
	if got := readAddress(t, f.keyPath); got != validatorAddress {
		t.Fatalf("key file holds %s, want the restored validator key %s", got, validatorAddress)
	}
}

// TestReleaseForFailbackPassiveWithoutKeyIsNoop verifies that a passive node
// whose key file holds a mock has nothing to release and does not restart,
// even when it keeps a staged key.
func TestReleaseForFailbackPassiveWithoutKeyIsNoop(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	f := newNodeFixture(t, "", validatorAddress, &recordingNode{log: events})
	f.stage(t, sealed)

	if err := f.fm.ReleaseForFailback(); err != nil {
		t.Fatalf("ReleaseForFailback failed: %v", err)
	}

	if got := events.list(); len(got) != 0 {
		t.Fatalf("events = %v, want none", got)
	}
	if f.fm.IsActive() {
		t.Fatal("node must stay passive")
	}
}

// TestReleaseForFailbackRestartsNodeThatMayStillSign verifies that after a
// takeover restart failed and the node could not be confirmed stopped, a
// failback release restarts the node onto the mock key before it answers,
// because the running process may have loaded the validator key.
func TestReleaseForFailbackRestartsNodeThatMayStillSign(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	node := &recordingNode{log: events, failRestarts: 1, stuckRunning: true}
	f := newNodeFixture(t, "", validatorAddress, node)
	f.stage(t, sealed)
	requireTransitionError(t, f.fm.TakeOver(), "restart_failed", http.StatusInternalServerError)
	afterTakeOver := len(events.list())

	if err := f.fm.ReleaseForFailback(); err != nil {
		t.Fatalf("ReleaseForFailback failed: %v", err)
	}

	if got := events.list()[afterTakeOver:]; !reflect.DeepEqual(got, []string{"local restart"}) {
		t.Fatalf("release events = %v, want one local restart", got)
	}
	if last := node.restartKeys[len(node.restartKeys)-1]; last == validatorAddress {
		t.Fatal("release restarted the node onto the validator key")
	}

	// The restart replaced the process, so a second release has nothing to do.
	afterRelease := len(events.list())
	if err := f.fm.ReleaseForFailback(); err != nil {
		t.Fatalf("second ReleaseForFailback failed: %v", err)
	}
	if got := events.list()[afterRelease:]; len(got) != 0 {
		t.Fatalf("second release events = %v, want none", got)
	}
}

// TestReleaseForFailbackRestartFailureKeepsDisarmedKeyFile verifies that a
// failed release restart restores only a key file the release itself disarmed:
// a key file that was already disarmed is never armed again.
func TestReleaseForFailbackRestartFailureKeepsDisarmedKeyFile(t *testing.T) {
	validatorAddress, sealed := newValidatorKeySource(t)
	events := &eventLog{}
	node := &recordingNode{log: events, failRestarts: 2, stuckRunning: true}
	f := newNodeFixture(t, "", validatorAddress, node)
	f.stage(t, sealed)
	requireTransitionError(t, f.fm.TakeOver(), "restart_failed", http.StatusInternalServerError)

	err := f.fm.ReleaseForFailback()

	requireTransitionError(t, err, "restart_failed", http.StatusInternalServerError)
	if got := readAddress(t, f.keyPath); got == validatorAddress {
		t.Fatal("failed release armed a key file that was already disarmed")
	}
	if got := readAddress(t, f.keyPath+".real"); got != validatorAddress {
		t.Fatalf(".real holds %s, want validator key %s", got, validatorAddress)
	}
}
