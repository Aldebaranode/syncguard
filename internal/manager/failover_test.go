package manager

import (
	"net/http"
	"reflect"
	"testing"
)

// newFailoverFixture builds an active node that holds the validator key and
// the state lock and fails over to a fake peer answering with replies.
func newFailoverFixture(t *testing.T, events *eventLog, replies fakePeerReplies, node *recordingNode) *nodeFixture {
	t.Helper()
	peerAddr, _ := newFakePeer(t, events, replies)
	validatorAddress, sealed := newValidatorKeySource(t)
	f := newNodeFixture(t, peerAddr, validatorAddress, node)
	f.activate(t, sealed)
	return f
}

// requireStillActive fails the test unless the node is active, holds the
// state lock, and its key file still holds the validator key.
func requireStillActive(t *testing.T, f *nodeFixture) {
	t.Helper()
	if !f.fm.IsActive() {
		t.Fatal("node must stay active after an aborted failover")
	}
	if !f.lockHeld(t) {
		t.Fatal("node that stays active must keep the state lock")
	}
	if got := readAddress(t, f.keyPath); got != f.validatorAddress {
		t.Fatalf("key file holds %s, want validator key %s", got, f.validatorAddress)
	}
}

// TestFailoverOrder verifies that a failover hands the key to the peer, then
// restarts onto a mock key, and only then tells the peer to take over.
func TestFailoverOrder(t *testing.T) {
	events := &eventLog{}
	node := &recordingNode{log: events}
	f := newFailoverFixture(t, events, fakePeerReplies{}, node)

	f.fm.initiateFailover()

	want := []string{"POST /validator_key", "local restart", "POST /failover_notify"}
	if got := events.list(); !reflect.DeepEqual(got, want) {
		t.Fatalf("failover order = %v, want %v", got, want)
	}
	if len(node.restartKeys) != 1 || node.restartKeys[0] == f.validatorAddress {
		t.Fatalf("node restarted onto %v, want one restart onto a mock key", node.restartKeys)
	}
	if f.fm.IsActive() {
		t.Fatal("node must be passive after the failover")
	}
	if f.lockHeld(t) {
		t.Fatal("passive node must release the state lock")
	}
}

// TestFailoverAbortsWhenTransferFails verifies that a failover whose key
// transfer fails stays active without disarming, restarting, or notifying,
// because the peer could not take over without the key.
func TestFailoverAbortsWhenTransferFails(t *testing.T) {
	events := &eventLog{}
	f := newFailoverFixture(t, events, fakePeerReplies{receiveKey: http.StatusInternalServerError}, &recordingNode{log: events})

	f.fm.initiateFailover()

	if got := events.list(); !reflect.DeepEqual(got, []string{"POST /validator_key"}) {
		t.Fatalf("events = %v, want only the key transfer", got)
	}
	requireStillActive(t, f)
}

// TestFailoverAbortsWhenDisarmFails verifies that a failover whose disarm
// fails stays active without restarting or notifying the peer.
func TestFailoverAbortsWhenDisarmFails(t *testing.T) {
	events := &eventLog{}
	f := newFailoverFixture(t, events, fakePeerReplies{}, &recordingNode{log: events})
	f.breakBackupPath(t)

	f.fm.initiateFailover()

	if got := events.list(); !reflect.DeepEqual(got, []string{"POST /validator_key"}) {
		t.Fatalf("events = %v, want only the key transfer", got)
	}
	requireStillActive(t, f)
}

// TestFailoverDoesNotNotifyWhenRestartFailsAndNodeStillRuns verifies that when
// the restart fails and the node keeps running after a stop, the failover
// stops there: the old process may still sign, so the peer is not told to
// take over.
func TestFailoverDoesNotNotifyWhenRestartFailsAndNodeStillRuns(t *testing.T) {
	events := &eventLog{}
	node := &recordingNode{log: events, failRestarts: 1, stuckRunning: true}
	f := newFailoverFixture(t, events, fakePeerReplies{}, node)

	f.fm.initiateFailover()

	want := []string{"POST /validator_key", "local restart", "local stop"}
	if got := events.list(); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
	// The key file is restored to match the process that may still sign and
	// the active role the node keeps.
	requireStillActive(t, f)
}

// TestFailoverDoesNotNotifyWhenStopFails verifies that a stop that reports an
// error does not count as a confirmed stop, even when the node then looks
// stopped: on doubt the peer is not told to take over.
func TestFailoverDoesNotNotifyWhenStopFails(t *testing.T) {
	events := &eventLog{}
	node := &recordingNode{log: events, failRestarts: 1, failStop: true}
	f := newFailoverFixture(t, events, fakePeerReplies{}, node)

	f.fm.initiateFailover()

	want := []string{"POST /validator_key", "local restart", "local stop"}
	if got := events.list(); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
	requireStillActive(t, f)
}

// TestFailoverContinuesWhenRestartFailsButNodeStops verifies that a failed
// restart followed by a confirmed stop still hands over: nothing signs here.
func TestFailoverContinuesWhenRestartFailsButNodeStops(t *testing.T) {
	events := &eventLog{}
	f := newFailoverFixture(t, events, fakePeerReplies{}, &recordingNode{log: events, failRestarts: 1})

	f.fm.initiateFailover()

	want := []string{"POST /validator_key", "local restart", "local stop", "POST /failover_notify"}
	if got := events.list(); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
	if f.fm.IsActive() {
		t.Fatal("node must be passive after handing over")
	}
}

// TestFailoverGoesPassiveWhenPeerRefusesTakeOver verifies that a refused
// takeover does not undo the local release: the node is disarmed and passive.
func TestFailoverGoesPassiveWhenPeerRefusesTakeOver(t *testing.T) {
	events := &eventLog{}
	f := newFailoverFixture(t, events, fakePeerReplies{takeOver: http.StatusConflict}, &recordingNode{log: events})

	f.fm.initiateFailover()

	if f.fm.IsActive() {
		t.Fatal("node must be passive after it disarmed and restarted")
	}
	if got := readAddress(t, f.keyPath); got == f.validatorAddress {
		t.Fatal("key file holds the validator key on a passive node")
	}
}

// TestNotifyPeerOfFailoverReportsRefusal verifies that the takeover request
// reports a non-2xx answer as an error and a 200 as success.
func TestNotifyPeerOfFailoverReportsRefusal(t *testing.T) {
	refusing := newFailoverFixture(t, &eventLog{}, fakePeerReplies{takeOver: http.StatusConflict}, &recordingNode{log: &eventLog{}})
	if err := refusing.fm.notifyPeerOfFailover(); err == nil {
		t.Fatal("a 409 answer must be reported as an error")
	}

	accepting := newFailoverFixture(t, &eventLog{}, fakePeerReplies{}, &recordingNode{log: &eventLog{}})
	if err := accepting.fm.notifyPeerOfFailover(); err != nil {
		t.Fatalf("a 200 answer must be success, got %v", err)
	}
}
