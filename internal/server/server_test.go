package server

import (
	"bytes"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/constants"
	"github.com/aldebaranode/syncguard/internal/crypto"
	"github.com/aldebaranode/syncguard/internal/logger"
	"github.com/aldebaranode/syncguard/internal/state"
)

// mockKeyProvider simulates the validator key store using the real cipher so
// the test exercises the actual encrypt/decrypt round-trip over the wire.
type mockKeyProvider struct {
	plaintext []byte
	restored  bool
}

func (m *mockKeyProvider) EncryptKeyToBytes(secret string) ([]byte, error) {
	return crypto.Encrypt(m.plaintext, secret)
}

func (m *mockKeyProvider) DecryptKeyFromBytes(data []byte, secret string) error {
	pt, err := crypto.Decrypt(data, secret)
	if err != nil {
		return err
	}
	m.plaintext = pt
	return nil
}

func (m *mockKeyProvider) DeleteKey() error { return nil }

// newTestServer wires a Server with only the fields the key handler needs.
func newTestServer(secret string, kp KeyProvider) *Server {
	return &Server{
		secret:      secret,
		keyProvider: kp,
		logger:      logger.NewLogger(&config.Config{}),
	}
}

// signedRequest builds a key-transfer request carrying a valid HMAC signature.
func signedRequest(method, secret string, body []byte) *http.Request {
	ts := time.Now().Unix()
	sig := crypto.SignWithTimestamp(constants.AuthPayloadValidatorKey, secret, ts)
	req := httptest.NewRequest(method, "/validator_key", bytes.NewReader(body))
	req.Header.Set(constants.HeaderSignature, sig)
	req.Header.Set(constants.HeaderTimestamp, strconv.FormatInt(ts, 10))
	return req
}

// TestHandleValidatorKey_RoundTrip proves the key crosses the wire encrypted:
// GET returns ciphertext, POST decrypts it back to the original key.
func TestHandleValidatorKey_RoundTrip(t *testing.T) {
	secret := "cluster-secret"
	original := []byte(`{"priv_key":"abc123"}`)

	// Active node exposes the key via GET (must be encrypted on the wire).
	active := newTestServer(secret, &mockKeyProvider{plaintext: original})
	getRec := httptest.NewRecorder()
	active.handleValidatorKey(getRec, signedRequest(http.MethodGet, secret, nil))

	if getRec.Code != http.StatusOK {
		t.Fatalf("GET expected 200, got %d", getRec.Code)
	}
	if bytes.Equal(getRec.Body.Bytes(), original) {
		t.Fatal("GET returned plaintext key; expected ciphertext over the wire")
	}

	// Passive node receives that ciphertext via POST and decrypts it.
	passive := newTestServer(secret, &mockKeyProvider{})
	postRec := httptest.NewRecorder()
	passive.handleValidatorKey(postRec, signedRequest(http.MethodPost, secret, getRec.Body.Bytes()))

	if postRec.Code != http.StatusOK {
		t.Fatalf("POST expected 200, got %d", postRec.Code)
	}
	stored := passive.keyProvider.(*mockKeyProvider).plaintext
	if !bytes.Equal(stored, original) {
		t.Fatalf("decrypted key mismatch: got %q want %q", stored, original)
	}
}

// TestHandleValidatorKey_RejectsUnsigned ensures an unauthenticated request
// cannot read or push the validator key.
func TestHandleValidatorKey_RejectsUnsigned(t *testing.T) {
	s := newTestServer("cluster-secret", &mockKeyProvider{plaintext: []byte("key")})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/validator_key", nil)
	s.handleValidatorKey(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for unsigned request, got %d", rec.Code)
	}
}

// TestHandleValidatorKey_RejectsWrongSecret ensures a request signed with the
// wrong cluster secret is rejected even though its headers are well-formed.
func TestHandleValidatorKey_RejectsWrongSecret(t *testing.T) {
	s := newTestServer("cluster-secret", &mockKeyProvider{plaintext: []byte("key")})

	rec := httptest.NewRecorder()
	s.handleValidatorKey(rec, signedRequest(http.MethodGet, "attacker-secret", nil))

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for wrong-secret request, got %d", rec.Code)
	}
}

// RestoreKey records that the key swap was rolled back.
func (m *mockKeyProvider) RestoreKey() error {
	m.restored = true
	return nil
}

// fakeNodeStatus is a NodeStatusProvider with a settable active flag.
type fakeNodeStatus struct{ active bool }

// IsActive reports the stored flag.
func (f *fakeNodeStatus) IsActive() bool { return f.active }

// IsPrimary reports a backup node.
func (f *fakeNodeStatus) IsPrimary() bool { return false }

// SetActive stores the flag.
func (f *fakeNodeStatus) SetActive(active bool) { f.active = active }

// BeginWarmup is a no-op.
func (f *fakeNodeStatus) BeginWarmup() {}

// failingRestarter is a NodeRestarter whose restart always fails.
type failingRestarter struct{}

// Restart always fails.
func (failingRestarter) Restart() error { return errors.New("container restart failed") }

// noopStateProvider is a StateProvider without real state.
type noopStateProvider struct{}

// LoadState returns an empty state.
func (noopStateProvider) LoadState() (*state.ValidatorState, error) {
	return &state.ValidatorState{}, nil
}

// AcquireLock always succeeds.
func (noopStateProvider) AcquireLock() error { return nil }

// ReleaseLock always succeeds.
func (noopStateProvider) ReleaseLock() error { return nil }

// TestHandleFailbackNotify_RefusesWhenRestartFails verifies that the active
// node answers 500, stays active, and restores its real key file when it
// cannot restart onto the mock key, so the primary never starts a second signer.
func TestHandleFailbackNotify_RefusesWhenRestartFails(t *testing.T) {
	keys := &mockKeyProvider{plaintext: []byte("key")}
	status := &fakeNodeStatus{active: true}
	s := &Server{
		keyProvider:   keys,
		stateProvider: noopStateProvider{},
		nodeStatus:    status,
		nodeRestarter: failingRestarter{},
		logger:        logger.NewLogger(&config.Config{}),
	}

	rec := httptest.NewRecorder()
	s.handleFailbackNotify(rec, httptest.NewRequest(http.MethodPost, "/failback_notify", nil))

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("expected 500 when the release restart fails, got %d", rec.Code)
	}
	if !status.active {
		t.Fatal("node must stay active when it could not stop signing")
	}
	if !keys.restored {
		t.Fatal("real key file must be restored when the release restart fails")
	}
}
