package manager

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/logger"
	"github.com/aldebaranode/syncguard/internal/state"
)

// newHangingPeer serves every request by blocking until the test ends, like a
// peer whose host stopped answering. It returns the peer address.
func newHangingPeer(t *testing.T) string {
	t.Helper()
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
	}))
	t.Cleanup(server.Close)
	// Registered after server.Close, so it runs first and unblocks the handlers.
	t.Cleanup(func() { close(release) })
	return strings.TrimPrefix(server.URL, "http://")
}

// TestPeerRequestsTimeOut verifies that state sync and key fetch give up on a
// peer that never answers instead of blocking the caller forever.
func TestPeerRequestsTimeOut(t *testing.T) {
	calls := map[string]func(fm *FailoverManager) error{
		"syncStateFromPeer":  func(fm *FailoverManager) error { return fm.syncStateFromPeer() },
		"requestKeyFromPeer": func(fm *FailoverManager) error { return fm.requestKeyFromPeer() },
	}
	for name, call := range calls {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			cfg := &config.Config{Secret: failbackTestSecret}
			cfg.Peers = []config.PeerConfig{{ID: "peer", Address: newHangingPeer(t)}}
			fm := &FailoverManager{
				cfg:          cfg,
				stateManager: state.NewManager(filepath.Join(dir, "priv_validator_state.json"), ""),
				keyManager:   state.NewKeyManager(filepath.Join(dir, "priv_validator_key.json"), dir, "", logger.NewLogger(cfg)),
				logger:       logger.NewLogger(cfg),
			}

			done := make(chan error, 1)
			go func() { done <- call(fm) }()

			select {
			case err := <-done:
				if err == nil {
					t.Fatal("a peer that never answers must produce an error")
				}
			case <-time.After(15 * time.Second):
				t.Fatal("request to a hanging peer did not time out within 15s")
			}
		})
	}
}
