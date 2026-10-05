package manager

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/logger"
	"github.com/aldebaranode/syncguard/internal/state"
)

const failbackTestSecret = "cluster-secret"

// eventLog records, in order, what failback does to the peer and the node.
type eventLog struct {
	mu     sync.Mutex
	events []string
}

// add appends one event.
func (l *eventLog) add(event string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.events = append(l.events, event)
}

// list returns a copy of the recorded events.
func (l *eventLog) list() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.events...)
}

// recordingNode is a node.Manager that records restarts. The next failRestarts
// restarts fail after being recorded.
type recordingNode struct {
	log          *eventLog
	failRestarts int
}

// Start is a no-op.
func (n *recordingNode) Start() error { return nil }

// Stop is a no-op.
func (n *recordingNode) Stop() error { return nil }

// Restart records the restart and fails while failRestarts is above zero.
func (n *recordingNode) Restart() error {
	n.log.add("local restart")
	if n.failRestarts > 0 {
		n.failRestarts--
		return errors.New("restart failed")
	}
	return nil
}

// IsRunning always reports a running node.
func (n *recordingNode) IsRunning() bool { return true }

// WaitHealthy polls healthCheck like the real node managers, but every 10ms,
// until it reports healthy or ctx ends.
func (n *recordingNode) WaitHealthy(ctx context.Context, healthCheck func() bool) error {
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			if healthCheck() {
				return nil
			}
		}
	}
}

// newKeyManagerForGeneratedKey creates a key at keyPath and returns a manager
// whose validator address is that key's address.
func newKeyManagerForGeneratedKey(t *testing.T, keyPath, dir string, log *logger.Logger) *state.KeyManager {
	t.Helper()
	generator := state.NewKeyManager(keyPath, dir, "", log)
	if err := generator.InitializeKey(); err != nil {
		t.Fatalf("failed to create key: %v", err)
	}
	key, err := generator.LoadKey()
	if err != nil {
		t.Fatalf("failed to load key: %v", err)
	}
	return state.NewKeyManager(keyPath, dir, key.Address, log)
}

// newFakePeer serves the peer endpoints failback uses and returns the peer
// address and the validator address its key carries. releaseStatus is the
// answer to /failback_notify.
func newFakePeer(t *testing.T, events *eventLog, releaseStatus int) (string, string) {
	t.Helper()
	peerDir := t.TempDir()
	peerKeys := newKeyManagerForGeneratedKey(t, filepath.Join(peerDir, "priv_validator_key.json"), peerDir, logger.NewLogger(&config.Config{}))
	realKey, err := peerKeys.LoadKey()
	if err != nil {
		t.Fatalf("failed to load peer key: %v", err)
	}
	encryptedKey, err := peerKeys.EncryptKeyToBytes(failbackTestSecret)
	if err != nil {
		t.Fatalf("failed to encrypt peer key: %v", err)
	}
	peerState, err := json.Marshal(&state.ValidatorState{Height: 10, Round: 0, Step: 3})
	if err != nil {
		t.Fatalf("failed to encode peer state: %v", err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		events.add(r.Method + " " + r.URL.Path)
		switch r.URL.Path {
		case "/validator_key":
			w.Write(encryptedKey)
		case "/validator_state":
			w.Write(peerState)
		case "/failback_notify":
			w.WriteHeader(releaseStatus)
		}
	}))
	t.Cleanup(server.Close)
	return strings.TrimPrefix(server.URL, "http://"), realKey.Address
}

// newFailbackTestManager builds a passive primary that holds a mock key and
// protects validatorAddress. It returns the manager and its key file path.
func newFailbackTestManager(t *testing.T, peerAddr, validatorAddress string, node *recordingNode) (*FailoverManager, string) {
	t.Helper()
	dir := t.TempDir()
	statePath := filepath.Join(dir, "priv_validator_state.json")
	if err := os.WriteFile(statePath, []byte(`{"height":"9","round":0,"step":3}`), 0600); err != nil {
		t.Fatalf("failed to write local state: %v", err)
	}

	cfg := &config.Config{Secret: failbackTestSecret}
	cfg.Peers = []config.PeerConfig{{ID: "peer", Address: peerAddr}}
	cfg.Validator.StopTimeout = 1
	cfg.Failover.WarmupPeriod = 90

	keyPath := filepath.Join(dir, "priv_validator_key.json")
	keys := state.NewKeyManager(keyPath, dir, validatorAddress, logger.NewLogger(cfg))
	if err := keys.InitializeKey(); err != nil {
		t.Fatalf("failed to create local mock key: %v", err)
	}

	return &FailoverManager{
		cfg:           cfg,
		stateManager:  state.NewManager(statePath, ""),
		keyManager:    keys,
		nodeManager:   node,
		logger:        logger.NewLogger(cfg),
		isPrimarySite: true,
		isActive:      false,
	}, keyPath
}

// readAddress returns the address stored in the key file at path.
func readAddress(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("failed to read %s: %v", path, err)
	}
	var key state.ValidatorKey
	if err := json.Unmarshal(data, &key); err != nil {
		t.Fatalf("failed to parse %s: %v", path, err)
	}
	return key.Address
}

// TestFailbackReleasesPeerBeforeLocalRestart verifies the break-before-make
// order: the peer stops signing and its final state is synced before this
// node restarts with the real key, so the two nodes never sign together.
func TestFailbackReleasesPeerBeforeLocalRestart(t *testing.T) {
	events := &eventLog{}
	peerAddr, validatorAddress := newFakePeer(t, events, http.StatusOK)
	fm, keyPath := newFailbackTestManager(t, peerAddr, validatorAddress, &recordingNode{log: events})

	fm.initiateFailback()

	want := []string{"GET /validator_key", "POST /failback_notify", "GET /validator_state", "local restart"}
	if got := events.list(); !reflect.DeepEqual(got, want) {
		t.Fatalf("failback order = %v, want %v", got, want)
	}
	if !fm.IsActive() {
		t.Fatal("node should be active after a successful failback")
	}
	if got := readAddress(t, keyPath); got != validatorAddress {
		t.Fatalf("active key %s, want validator key %s", got, validatorAddress)
	}
}

// TestFailbackAbortsWhenPeerDoesNotRelease verifies that when the peer cannot
// confirm its release, this node stays passive, does not restart, and does not
// keep the real key as its active key file, while .real still holds the key.
func TestFailbackAbortsWhenPeerDoesNotRelease(t *testing.T) {
	events := &eventLog{}
	peerAddr, validatorAddress := newFakePeer(t, events, http.StatusInternalServerError)
	fm, keyPath := newFailbackTestManager(t, peerAddr, validatorAddress, &recordingNode{log: events})

	fm.initiateFailback()

	for _, event := range events.list() {
		if event == "local restart" {
			t.Fatal("local node must not restart while the peer may still be signing")
		}
	}
	if fm.IsActive() {
		t.Fatal("node must stay passive when the peer did not release")
	}
	if got := readAddress(t, keyPath); got == validatorAddress {
		t.Fatalf("validator key %s left as the active key after an aborted failback", got)
	}
	if got := readAddress(t, keyPath+".real"); got != validatorAddress {
		t.Fatalf(".real holds %s, want validator key %s", got, validatorAddress)
	}
}

// TestFailbackRetryAfterPeerReleasedKeepsValidatorKey verifies that once the
// peer released, a failed restart keeps the validator key, and the next attempt
// reuses it without fetching from the peer, whose key is now a mock.
func TestFailbackRetryAfterPeerReleasedKeepsValidatorKey(t *testing.T) {
	events := &eventLog{}
	peerAddr, validatorAddress := newFakePeer(t, events, http.StatusOK)
	node := &recordingNode{log: events, failRestarts: 1}
	fm, keyPath := newFailbackTestManager(t, peerAddr, validatorAddress, node)

	fm.initiateFailback()

	if fm.IsActive() {
		t.Fatal("node must stay passive after a failed restart")
	}
	if got := readAddress(t, keyPath); got != validatorAddress {
		t.Fatalf("key path holds %s after the failed restart, want validator key %s", got, validatorAddress)
	}

	firstAttempt := len(events.list())
	fm.initiateFailback()

	want := []string{"POST /failback_notify", "GET /validator_state", "local restart"}
	if got := events.list()[firstAttempt:]; !reflect.DeepEqual(got, want) {
		t.Fatalf("retry events = %v, want %v", got, want)
	}
	if !fm.IsActive() {
		t.Fatal("node should be active after the retry")
	}
	if got := readAddress(t, keyPath); got != validatorAddress {
		t.Fatalf("active key %s, want validator key %s", got, validatorAddress)
	}
}

// TestFailbackRefusesNonValidatorKeyFromPeer verifies that a key whose address
// is not the configured validator address is never saved, notified on, or
// started with.
func TestFailbackRefusesNonValidatorKeyFromPeer(t *testing.T) {
	events := &eventLog{}
	peerAddr, _ := newFakePeer(t, events, http.StatusOK)
	otherAddress := "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
	fm, keyPath := newFailbackTestManager(t, peerAddr, otherAddress, &recordingNode{log: events})
	before, err := os.ReadFile(keyPath)
	if err != nil {
		t.Fatalf("failed to read local key: %v", err)
	}

	fm.initiateFailback()

	want := []string{"GET /validator_key"}
	if got := events.list(); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
	if fm.IsActive() {
		t.Fatal("node must stay passive when the peer served a non-validator key")
	}
	after, err := os.ReadFile(keyPath)
	if err != nil {
		t.Fatalf("failed to read local key after: %v", err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("local key file changed after a rejected key")
	}
}
