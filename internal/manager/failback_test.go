package manager

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

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

// recordingNode is a node.Manager that records restarts and does nothing else.
type recordingNode struct{ log *eventLog }

// Start is a no-op.
func (n *recordingNode) Start() error { return nil }

// Stop is a no-op.
func (n *recordingNode) Stop() error { return nil }

// Restart records the restart.
func (n *recordingNode) Restart() error {
	n.log.add("local restart")
	return nil
}

// IsRunning always reports a running node.
func (n *recordingNode) IsRunning() bool { return true }

// WaitHealthy returns immediately.
func (n *recordingNode) WaitHealthy(ctx context.Context, healthCheck func() bool) error { return nil }

// newFakePeer serves the peer endpoints failback uses and returns the peer
// address and the address of the real key it hands out. releaseStatus is the
// answer to /failback_notify.
func newFakePeer(t *testing.T, events *eventLog, releaseStatus int) (string, string) {
	t.Helper()
	peerDir := t.TempDir()
	peerKeys := state.NewKeyManager(filepath.Join(peerDir, "priv_validator_key.json"), peerDir, logger.NewLogger(&config.Config{}))
	if err := peerKeys.InitializeKey(); err != nil {
		t.Fatalf("failed to create peer key: %v", err)
	}
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

// newFailbackTestManager builds a passive primary whose peer is peerAddr.
func newFailbackTestManager(t *testing.T, peerAddr string, events *eventLog) *FailoverManager {
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

	keys := state.NewKeyManager(filepath.Join(dir, "priv_validator_key.json"), dir, logger.NewLogger(cfg))
	if err := keys.InitializeKey(); err != nil {
		t.Fatalf("failed to create local mock key: %v", err)
	}

	return &FailoverManager{
		cfg:           cfg,
		stateManager:  state.NewManager(statePath, ""),
		keyManager:    keys,
		nodeManager:   &recordingNode{log: events},
		logger:        logger.NewLogger(cfg),
		isPrimarySite: true,
		isActive:      false,
	}
}

// TestFailbackReleasesPeerBeforeLocalRestart verifies the break-before-make
// order: the peer stops signing and its final state is synced before this
// node restarts with the real key, so the two nodes never sign together.
func TestFailbackReleasesPeerBeforeLocalRestart(t *testing.T) {
	events := &eventLog{}
	peerAddr, _ := newFakePeer(t, events, http.StatusOK)
	fm := newFailbackTestManager(t, peerAddr, events)

	fm.initiateFailback()

	want := []string{"GET /validator_key", "POST /failback_notify", "GET /validator_state", "local restart"}
	if got := events.list(); !reflect.DeepEqual(got, want) {
		t.Fatalf("failback order = %v, want %v", got, want)
	}
	if !fm.IsActive() {
		t.Fatal("node should be active after a successful failback")
	}
}

// TestFailbackAbortsWhenPeerDoesNotRelease verifies that when the peer cannot
// confirm its release, this node stays passive, does not restart, and does not
// keep the real key as its active key file.
func TestFailbackAbortsWhenPeerDoesNotRelease(t *testing.T) {
	events := &eventLog{}
	peerAddr, realAddress := newFakePeer(t, events, http.StatusInternalServerError)
	fm := newFailbackTestManager(t, peerAddr, events)

	fm.initiateFailback()

	for _, event := range events.list() {
		if event == "local restart" {
			t.Fatal("local node must not restart while the peer may still be signing")
		}
	}
	if fm.IsActive() {
		t.Fatal("node must stay passive when the peer did not release")
	}
	activeKey, err := fm.keyManager.LoadKey()
	if err != nil {
		t.Fatalf("failed to load local key: %v", err)
	}
	if activeKey.Address == realAddress {
		t.Fatalf("real key %s left as the active key after an aborted failback", realAddress)
	}
}
