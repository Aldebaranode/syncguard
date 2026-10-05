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

// recordingNode is a node.Manager that records restarts and stops. The next
// failRestarts restarts fail after being recorded.
type recordingNode struct {
	log          *eventLog
	failRestarts int
	// stuckRunning keeps the node running after Stop, like a container that
	// cannot be stopped.
	stuckRunning bool
	// failStop makes Stop return an error after it stopped the node, like a
	// container runtime whose answer was lost.
	failStop bool
	stopped  bool
	// keyPath, when set, makes every restart record the address in that key
	// file in restartKeys, so a test can tell which key the node loaded.
	keyPath     string
	restartKeys []string
}

// Start is a no-op.
func (n *recordingNode) Start() error { return nil }

// Stop records the stop and stops the node unless stuckRunning is set. It
// fails when failStop is set.
func (n *recordingNode) Stop() error {
	n.log.add("local stop")
	if !n.stuckRunning {
		n.stopped = true
	}
	if n.failStop {
		return errors.New("stop failed")
	}
	return nil
}

// Restart records the restart and fails while failRestarts is above zero.
func (n *recordingNode) Restart() error {
	n.log.add("local restart")
	if n.keyPath != "" {
		address, _ := keyAddressAt(n.keyPath)
		n.restartKeys = append(n.restartKeys, address)
	}
	if n.failRestarts > 0 {
		n.failRestarts--
		return errors.New("restart failed")
	}
	n.stopped = false
	return nil
}

// IsRunning reports whether the node has not been stopped.
func (n *recordingNode) IsRunning() bool { return !n.stopped }

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

// fakePeerReplies sets what the fake peer answers. A zero status means 200.
type fakePeerReplies struct {
	release    int  // POST /failback_notify
	receiveKey int  // POST /validator_key
	takeOver   int  // POST /failover_notify
	holdsNoKey bool // GET /validator_key answers 404
}

// statusOrOK returns status, or 200 when status is zero.
func statusOrOK(status int) int {
	if status == 0 {
		return http.StatusOK
	}
	return status
}

// newFakePeer serves the peer endpoints failover and failback use and returns
// the peer address and the validator address its key carries.
func newFakePeer(t *testing.T, events *eventLog, replies fakePeerReplies) (string, string) {
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
		switch {
		case r.URL.Path == "/validator_key" && r.Method == http.MethodGet && replies.holdsNoKey:
			http.Error(w, "No key available", http.StatusNotFound)
		case r.URL.Path == "/validator_key" && r.Method == http.MethodGet:
			w.Write(encryptedKey)
		case r.URL.Path == "/validator_key":
			w.WriteHeader(statusOrOK(replies.receiveKey))
		case r.URL.Path == "/validator_state":
			w.Write(peerState)
		case r.URL.Path == "/failback_notify":
			w.WriteHeader(statusOrOK(replies.release))
		case r.URL.Path == "/failover_notify":
			w.WriteHeader(statusOrOK(replies.takeOver))
		}
	}))
	t.Cleanup(server.Close)
	return strings.TrimPrefix(server.URL, "http://"), realKey.Address
}

// nodeFixture is a manager on disk plus the paths and the validator address
// the tests inspect.
type nodeFixture struct {
	fm               *FailoverManager
	dir              string
	keyPath          string
	statePath        string
	lockPath         string
	validatorAddress string
}

// newNodeFixture builds a passive primary whose key file holds a mock key and
// which protects validatorAddress. It talks to the peer at peerAddr, reports
// a healthy node, and makes node record the key it restarts onto.
func newNodeFixture(t *testing.T, peerAddr, validatorAddress string, node *recordingNode) *nodeFixture {
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
	node.keyPath = keyPath

	fm := &FailoverManager{
		cfg:           cfg,
		stateManager:  state.NewManager(statePath, ""),
		keyManager:    keys,
		healthChecker: &fakeHealthChecker{cachedHealthy: true},
		nodeManager:   node,
		logger:        logger.NewLogger(cfg),
		isPrimarySite: true,
		isActive:      false,
	}
	t.Cleanup(func() { fm.stateManager.ReleaseLock() })

	return &nodeFixture{
		fm:               fm,
		dir:              dir,
		keyPath:          keyPath,
		statePath:        statePath,
		lockPath:         statePath + ".lock",
		validatorAddress: validatorAddress,
	}
}

// stage writes the sealed validator key into the staged file through the
// same call the key transfer handler uses.
func (f *nodeFixture) stage(t *testing.T, sealed []byte) {
	t.Helper()
	if err := f.fm.keyManager.StageKeyFromBytes(sealed, failbackTestSecret); err != nil {
		t.Fatalf("failed to stage validator key: %v", err)
	}
}

// arm stages the sealed validator key and promotes it, so the key file holds
// the validator key while the node stays passive.
func (f *nodeFixture) arm(t *testing.T, sealed []byte) {
	t.Helper()
	f.stage(t, sealed)
	if err := f.fm.keyManager.PromoteStagedKey(); err != nil {
		t.Fatalf("failed to promote validator key: %v", err)
	}
}

// activate arms the node and makes it the active validator holding the lock.
func (f *nodeFixture) activate(t *testing.T, sealed []byte) {
	t.Helper()
	f.arm(t, sealed)
	if err := f.fm.stateManager.AcquireLock(); err != nil {
		t.Fatalf("failed to take the state lock: %v", err)
	}
	f.fm.SetActive(true)
}

// breakBackupPath rebuilds the key manager with a backup path that is a
// regular file, so every DeleteKey fails before it touches the key file.
func (f *nodeFixture) breakBackupPath(t *testing.T) {
	t.Helper()
	backupFile := filepath.Join(f.dir, "backup-is-a-file")
	if err := os.WriteFile(backupFile, []byte("not a directory"), 0600); err != nil {
		t.Fatalf("failed to write backup file: %v", err)
	}
	f.fm.keyManager = state.NewKeyManager(f.keyPath, backupFile, f.validatorAddress, logger.NewLogger(f.fm.cfg))
}

// lockHeld reports whether the state lock file exists.
func (f *nodeFixture) lockHeld(t *testing.T) bool {
	t.Helper()
	_, err := os.Stat(f.lockPath)
	if err != nil && !os.IsNotExist(err) {
		t.Fatalf("failed to stat lock file: %v", err)
	}
	return err == nil
}

// stagedFileExists reports whether the staged key file exists.
func (f *nodeFixture) stagedFileExists(t *testing.T) bool {
	t.Helper()
	_, err := os.Stat(f.keyPath + ".staged")
	if err != nil && !os.IsNotExist(err) {
		t.Fatalf("failed to stat staged file: %v", err)
	}
	return err == nil
}

// newValidatorKeySource generates a validator key in its own directory and
// returns its address and the key encrypted for transfer.
func newValidatorKeySource(t *testing.T) (string, []byte) {
	t.Helper()
	dir := t.TempDir()
	source := newKeyManagerForGeneratedKey(t, filepath.Join(dir, "priv_validator_key.json"), dir, logger.NewLogger(&config.Config{}))
	key, err := source.LoadKey()
	if err != nil {
		t.Fatalf("failed to load source key: %v", err)
	}
	sealed, err := source.EncryptKeyToBytes(failbackTestSecret)
	if err != nil {
		t.Fatalf("failed to encrypt source key: %v", err)
	}
	return key.Address, sealed
}

// keyAddressAt returns the address stored in the key file at path.
func keyAddressAt(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	var key state.ValidatorKey
	if err := json.Unmarshal(data, &key); err != nil {
		return "", err
	}
	return key.Address, nil
}

// readAddress returns the address stored in the key file at path.
func readAddress(t *testing.T, path string) string {
	t.Helper()
	address, err := keyAddressAt(path)
	if err != nil {
		t.Fatalf("failed to read key address from %s: %v", path, err)
	}
	return address
}

// TestFailbackReleasesPeerBeforeLocalRestart verifies the break-before-make
// order: the peer stops signing and its final state is synced before this
// node restarts with the real key, so the two nodes never sign together. The
// fetched key is armed only for that restart.
func TestFailbackReleasesPeerBeforeLocalRestart(t *testing.T) {
	events := &eventLog{}
	peerAddr, validatorAddress := newFakePeer(t, events, fakePeerReplies{})
	node := &recordingNode{log: events}
	f := newNodeFixture(t, peerAddr, validatorAddress, node)

	f.fm.initiateFailback()

	want := []string{"GET /validator_key", "POST /failback_notify", "GET /validator_state", "local restart"}
	if got := events.list(); !reflect.DeepEqual(got, want) {
		t.Fatalf("failback order = %v, want %v", got, want)
	}
	if !reflect.DeepEqual(node.restartKeys, []string{validatorAddress}) {
		t.Fatalf("node restarted onto %v, want the validator key %s", node.restartKeys, validatorAddress)
	}
	if !f.fm.IsActive() {
		t.Fatal("node should be active after a successful failback")
	}
	if got := readAddress(t, f.keyPath); got != validatorAddress {
		t.Fatalf("active key %s, want validator key %s", got, validatorAddress)
	}
	if f.stagedFileExists(t) {
		t.Fatal("staged file left after the key was promoted")
	}
}

// TestFailbackAbortsWhenPeerDoesNotRelease verifies that when the peer cannot
// confirm its release, this node stays passive, does not restart, and never
// armed its key file: the fetched key waits in the staged file.
func TestFailbackAbortsWhenPeerDoesNotRelease(t *testing.T) {
	events := &eventLog{}
	peerAddr, validatorAddress := newFakePeer(t, events, fakePeerReplies{release: http.StatusInternalServerError})
	f := newNodeFixture(t, peerAddr, validatorAddress, &recordingNode{log: events})
	before, err := os.ReadFile(f.keyPath)
	if err != nil {
		t.Fatalf("failed to read local key: %v", err)
	}

	f.fm.initiateFailback()

	for _, event := range events.list() {
		if event == "local restart" {
			t.Fatal("local node must not restart while the peer may still be signing")
		}
	}
	if f.fm.IsActive() {
		t.Fatal("node must stay passive when the peer did not release")
	}
	after, err := os.ReadFile(f.keyPath)
	if err != nil {
		t.Fatalf("failed to read local key after: %v", err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("key file changed although the peer did not release")
	}
	if got := readAddress(t, f.keyPath+".staged"); got != validatorAddress {
		t.Fatalf("staged file holds %s, want validator key %s", got, validatorAddress)
	}
}

// TestFailbackRetryAfterPeerReleasedKeepsValidatorKey verifies that once the
// peer released, a failed restart keeps the validator key, and the next attempt
// reuses it without fetching from the peer.
func TestFailbackRetryAfterPeerReleasedKeepsValidatorKey(t *testing.T) {
	events := &eventLog{}
	peerAddr, validatorAddress := newFakePeer(t, events, fakePeerReplies{})
	f := newNodeFixture(t, peerAddr, validatorAddress, &recordingNode{log: events, failRestarts: 1})

	f.fm.initiateFailback()

	if f.fm.IsActive() {
		t.Fatal("node must stay passive after a failed restart")
	}
	if got := readAddress(t, f.keyPath); got != validatorAddress {
		t.Fatalf("key path holds %s after the failed restart, want validator key %s", got, validatorAddress)
	}

	firstAttempt := len(events.list())
	f.fm.initiateFailback()

	want := []string{"POST /failback_notify", "GET /validator_state", "local restart"}
	if got := events.list()[firstAttempt:]; !reflect.DeepEqual(got, want) {
		t.Fatalf("retry events = %v, want %v", got, want)
	}
	if !f.fm.IsActive() {
		t.Fatal("node should be active after the retry")
	}
	if got := readAddress(t, f.keyPath); got != validatorAddress {
		t.Fatalf("active key %s, want validator key %s", got, validatorAddress)
	}
}

// TestFailbackRetryReusesStagedKey verifies that a failure after the peer
// released but before the promotion keeps the key staged, and the next attempt
// promotes it without fetching from the peer.
func TestFailbackRetryReusesStagedKey(t *testing.T) {
	events := &eventLog{}
	peerAddr, validatorAddress := newFakePeer(t, events, fakePeerReplies{})
	f := newNodeFixture(t, peerAddr, validatorAddress, &recordingNode{log: events})
	// Another owner holds the state lock, so the first attempt fails after the
	// peer released.
	otherOwner := state.NewManager(f.statePath, "")
	if err := otherOwner.AcquireLock(); err != nil {
		t.Fatalf("failed to take the lock for another owner: %v", err)
	}

	f.fm.initiateFailback()

	if f.fm.IsActive() {
		t.Fatal("node must stay passive while the lock is held elsewhere")
	}
	if got := readAddress(t, f.keyPath); got == validatorAddress {
		t.Fatal("key file armed although the failback did not reach the restart")
	}
	if got := readAddress(t, f.keyPath+".staged"); got != validatorAddress {
		t.Fatalf("staged file holds %s, want validator key %s", got, validatorAddress)
	}

	if err := otherOwner.ReleaseLock(); err != nil {
		t.Fatalf("failed to release the other owner's lock: %v", err)
	}
	firstAttempt := len(events.list())
	f.fm.initiateFailback()

	want := []string{"POST /failback_notify", "GET /validator_state", "local restart"}
	if got := events.list()[firstAttempt:]; !reflect.DeepEqual(got, want) {
		t.Fatalf("retry events = %v, want %v", got, want)
	}
	if !f.fm.IsActive() {
		t.Fatal("node should be active after the retry")
	}
	if got := readAddress(t, f.keyPath); got != validatorAddress {
		t.Fatalf("active key %s, want validator key %s", got, validatorAddress)
	}
}

// TestFailbackRefusesNonValidatorKeyFromPeer verifies that a key whose address
// is not the configured validator address is never staged, notified on, or
// started with.
func TestFailbackRefusesNonValidatorKeyFromPeer(t *testing.T) {
	events := &eventLog{}
	peerAddr, _ := newFakePeer(t, events, fakePeerReplies{})
	otherAddress := "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
	f := newNodeFixture(t, peerAddr, otherAddress, &recordingNode{log: events})
	before, err := os.ReadFile(f.keyPath)
	if err != nil {
		t.Fatalf("failed to read local key: %v", err)
	}

	f.fm.initiateFailback()

	want := []string{"GET /validator_key"}
	if got := events.list(); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
	if f.fm.IsActive() {
		t.Fatal("node must stay passive when the peer served a non-validator key")
	}
	after, err := os.ReadFile(f.keyPath)
	if err != nil {
		t.Fatalf("failed to read local key after: %v", err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("local key file changed after a rejected key")
	}
	if f.stagedFileExists(t) {
		t.Fatal("a rejected key was staged")
	}
}

// TestFailbackUsesLocalRealKeyWhenPeerHasNone verifies that a primary whose
// only validator key is its own .real stash fails back with it, but stages it
// only after the peer confirmed its release.
func TestFailbackUsesLocalRealKeyWhenPeerHasNone(t *testing.T) {
	// newReleasedPrimary builds a primary whose validator key sits only in
	// .real, as after a failover, talking to a peer that holds no key.
	newReleasedPrimary := func(t *testing.T, events *eventLog, replies fakePeerReplies) *nodeFixture {
		t.Helper()
		replies.holdsNoKey = true
		peerAddr, _ := newFakePeer(t, events, replies)
		validatorAddress, sealed := newValidatorKeySource(t)
		f := newNodeFixture(t, peerAddr, validatorAddress, &recordingNode{log: events})
		f.arm(t, sealed)
		if err := f.fm.keyManager.DeleteKey(); err != nil {
			t.Fatalf("failed to disarm the key: %v", err)
		}
		return f
	}

	t.Run("peer releases", func(t *testing.T) {
		events := &eventLog{}
		f := newReleasedPrimary(t, events, fakePeerReplies{})

		f.fm.initiateFailback()

		want := []string{"GET /validator_key", "POST /failback_notify", "GET /validator_state", "local restart"}
		if got := events.list(); !reflect.DeepEqual(got, want) {
			t.Fatalf("failback events = %v, want %v", got, want)
		}
		if !f.fm.IsActive() {
			t.Fatal("node should be active after failing back with its .real key")
		}
		if got := readAddress(t, f.keyPath); got != f.validatorAddress {
			t.Fatalf("active key %s, want validator key %s", got, f.validatorAddress)
		}
		if got := readAddress(t, f.keyPath+".real"); got != f.validatorAddress {
			t.Fatalf(".real holds %s, want validator key %s", got, f.validatorAddress)
		}
	})

	t.Run("peer does not release", func(t *testing.T) {
		events := &eventLog{}
		f := newReleasedPrimary(t, events, fakePeerReplies{release: http.StatusInternalServerError})

		f.fm.initiateFailback()

		if f.fm.IsActive() {
			t.Fatal("node must stay passive when the peer did not release")
		}
		if f.stagedFileExists(t) {
			t.Fatal(".real was staged before the peer confirmed its release")
		}
		if got := readAddress(t, f.keyPath); got == f.validatorAddress {
			t.Fatal("key file armed although the peer did not release")
		}
	})
}
