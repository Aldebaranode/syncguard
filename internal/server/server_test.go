package server

import (
	"bytes"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/constants"
	"github.com/aldebaranode/syncguard/internal/crypto"
	"github.com/aldebaranode/syncguard/internal/logger"
)

// mockKeyProvider simulates the validator key store using the real cipher so
// the test exercises the actual encrypt/decrypt round-trip over the wire.
type mockKeyProvider struct {
	plaintext []byte
	staged    []byte
}

// EncryptKeyToBytes encrypts the stored plaintext with the real cipher.
func (m *mockKeyProvider) EncryptKeyToBytes(secret string) ([]byte, error) {
	return crypto.Encrypt(m.plaintext, secret)
}

// StageKeyFromBytes decrypts data with the real cipher and keeps the
// plaintext as the staged key.
func (m *mockKeyProvider) StageKeyFromBytes(data []byte, secret string) error {
	pt, err := crypto.Decrypt(data, secret)
	if err != nil {
		return err
	}
	m.staged = pt
	return nil
}

// newTestServer wires a Server with only the fields the key handler needs.
func newTestServer(secret string, kp KeyProvider) *Server {
	return &Server{
		secret:      secret,
		keyProvider: kp,
		logger:      logger.NewLogger(&config.Config{}),
	}
}

// signedPeerRequest builds a request to path signed over payload with secret.
func signedPeerRequest(method, path, payload, secret string, body []byte) *http.Request {
	ts := time.Now().Unix()
	sig := crypto.SignWithTimestamp(payload, secret, ts)
	req := httptest.NewRequest(method, path, bytes.NewReader(body))
	req.Header.Set(constants.HeaderSignature, sig)
	req.Header.Set(constants.HeaderTimestamp, strconv.FormatInt(ts, 10))
	return req
}

// signedRequest builds a key-transfer request carrying a valid HMAC signature.
func signedRequest(method, secret string, body []byte) *http.Request {
	return signedPeerRequest(method, "/validator_key", constants.AuthPayloadValidatorKey, secret, body)
}

// notifySecret is the cluster secret of the notify handler tests.
const notifySecret = "cluster-secret"

// newNotifyTestServer wires a Server with the secret and the role transitions.
func newNotifyTestServer(roles RoleTransitions) *Server {
	s := newRolesTestServer(roles)
	s.secret = notifySecret
	return s
}

// TestHandleValidatorKey_RoundTrip proves the key crosses the wire encrypted:
// GET returns ciphertext, POST decrypts it back to the original key and stages
// it instead of arming the key file.
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

	// Passive node receives that ciphertext via POST, decrypts it, and stages it.
	passive := newTestServer(secret, &mockKeyProvider{})
	postRec := httptest.NewRecorder()
	passive.handleValidatorKey(postRec, signedRequest(http.MethodPost, secret, getRec.Body.Bytes()))

	if postRec.Code != http.StatusOK {
		t.Fatalf("POST expected 200, got %d", postRec.Code)
	}
	receiver, ok := passive.keyProvider.(*mockKeyProvider)
	if !ok {
		t.Fatalf("key provider is %T, want *mockKeyProvider", passive.keyProvider)
	}
	if receiver.plaintext != nil {
		t.Fatal("POST wrote the key the node reads instead of staging it")
	}
	stored := receiver.staged
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

// fakeRoles is a RoleTransitions that records calls and returns preset errors.
type fakeRoles struct {
	takeOverErr error
	releaseErr  error
	takeOvers   int
	releases    int
}

// TakeOver records the call and returns takeOverErr.
func (f *fakeRoles) TakeOver() error {
	f.takeOvers++
	return f.takeOverErr
}

// ReleaseForFailback records the call and returns releaseErr.
func (f *fakeRoles) ReleaseForFailback() error {
	f.releases++
	return f.releaseErr
}

// fakeTransitionFailure is a TransitionFailure with a fixed status and code.
type fakeTransitionFailure struct {
	status int
	code   string
}

// Error describes the failure.
func (f fakeTransitionFailure) Error() string { return "transition failed: " + f.code }

// HTTPStatus returns the preset status.
func (f fakeTransitionFailure) HTTPStatus() int { return f.status }

// ReasonCode returns the preset code.
func (f fakeTransitionFailure) ReasonCode() string { return f.code }

// newRolesTestServer wires a Server with only the role transitions.
func newRolesTestServer(roles RoleTransitions) *Server {
	return &Server{
		roles:  roles,
		logger: logger.NewLogger(&config.Config{}),
	}
}

// transitionCase is one domain result and the answer the handler must give.
type transitionCase struct {
	name       string
	err        error
	wantStatus int
	wantBody   string
}

// transitionCases covers a typed failure (also when wrapped), any other
// error, and success.
var transitionCases = []transitionCase{
	{
		name:       "typed conflict",
		err:        fakeTransitionFailure{status: http.StatusConflict, code: "not_healthy"},
		wantStatus: http.StatusConflict,
		wantBody:   "not_healthy",
	},
	{
		name:       "wrapped typed failure",
		err:        fmt.Errorf("wrapped: %w", fakeTransitionFailure{status: http.StatusInternalServerError, code: "restart_failed"}),
		wantStatus: http.StatusInternalServerError,
		wantBody:   "restart_failed",
	},
	{
		name:       "untyped error",
		err:        errors.New("unexpected"),
		wantStatus: http.StatusInternalServerError,
		wantBody:   "internal_error",
	},
	{
		name:       "success",
		err:        nil,
		wantStatus: http.StatusOK,
	},
}

// requireAnswer fails the test unless rec carries the case's status and body.
func requireAnswer(t *testing.T, rec *httptest.ResponseRecorder, tc transitionCase) {
	t.Helper()
	if rec.Code != tc.wantStatus {
		t.Fatalf("status = %d, want %d", rec.Code, tc.wantStatus)
	}
	if !strings.Contains(rec.Body.String(), tc.wantBody) {
		t.Fatalf("body = %q, want it to contain %q", rec.Body.String(), tc.wantBody)
	}
}

// TestHandleFailoverNotify_MapsTransitionError verifies that the takeover
// handler only delegates to TakeOver and maps its result to the answer.
func TestHandleFailoverNotify_MapsTransitionError(t *testing.T) {
	for _, tc := range transitionCases {
		t.Run(tc.name, func(t *testing.T) {
			roles := &fakeRoles{takeOverErr: tc.err}
			s := newNotifyTestServer(roles)

			rec := httptest.NewRecorder()
			s.handleFailoverNotify(rec, signedPeerRequest(http.MethodPost, "/failover_notify", constants.AuthPayloadFailoverNotify, notifySecret, nil))

			requireAnswer(t, rec, tc)
			if roles.takeOvers != 1 || roles.releases != 0 {
				t.Fatalf("calls = %d takeovers, %d releases; want 1, 0", roles.takeOvers, roles.releases)
			}
		})
	}
}

// TestHandleFailbackNotify_MapsTransitionError verifies that the release
// handler maps a failed ReleaseForFailback to the carried status and code.
func TestHandleFailbackNotify_MapsTransitionError(t *testing.T) {
	for _, tc := range transitionCases {
		t.Run(tc.name, func(t *testing.T) {
			roles := &fakeRoles{releaseErr: tc.err}
			s := newNotifyTestServer(roles)

			rec := httptest.NewRecorder()
			s.handleFailbackNotify(rec, signedPeerRequest(http.MethodPost, "/failback_notify", constants.AuthPayloadFailbackNotify, notifySecret, nil))

			requireAnswer(t, rec, tc)
		})
	}
}

// TestHandleFailbackNotify_Success verifies that a completed release answers
// 200 after exactly one ReleaseForFailback and no takeover.
func TestHandleFailbackNotify_Success(t *testing.T) {
	roles := &fakeRoles{}
	s := newNotifyTestServer(roles)

	rec := httptest.NewRecorder()
	s.handleFailbackNotify(rec, signedPeerRequest(http.MethodPost, "/failback_notify", constants.AuthPayloadFailbackNotify, notifySecret, nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if roles.releases != 1 || roles.takeOvers != 0 {
		t.Fatalf("calls = %d releases, %d takeovers; want 1, 0", roles.releases, roles.takeOvers)
	}
}

// requireNotifyRejected fails unless handler answers status to req without
// calling any role transition.
func requireNotifyRejected(t *testing.T, handler func(http.ResponseWriter, *http.Request), req *http.Request, roles *fakeRoles, status int) {
	t.Helper()
	rec := httptest.NewRecorder()
	handler(rec, req)
	if rec.Code != status {
		t.Fatalf("status = %d, want %d", rec.Code, status)
	}
	if roles.takeOvers != 0 || roles.releases != 0 {
		t.Fatalf("calls = %d takeovers, %d releases; want none", roles.takeOvers, roles.releases)
	}
}

// TestHandleFailoverNotify_RejectsUnsigned verifies that an unsigned takeover
// request gets 401 and never reaches TakeOver.
func TestHandleFailoverNotify_RejectsUnsigned(t *testing.T) {
	roles := &fakeRoles{}
	s := newNotifyTestServer(roles)
	req := httptest.NewRequest(http.MethodPost, "/failover_notify", nil)
	requireNotifyRejected(t, s.handleFailoverNotify, req, roles, http.StatusUnauthorized)
}

// TestHandleFailoverNotify_RejectsValidatorKeyPayload verifies that a request
// signed with the right secret but the key-transfer payload gets 401, so a
// captured key-transfer signature cannot trigger a takeover.
func TestHandleFailoverNotify_RejectsValidatorKeyPayload(t *testing.T) {
	roles := &fakeRoles{}
	s := newNotifyTestServer(roles)
	req := signedPeerRequest(http.MethodPost, "/failover_notify", constants.AuthPayloadValidatorKey, notifySecret, nil)
	requireNotifyRejected(t, s.handleFailoverNotify, req, roles, http.StatusUnauthorized)
}

// TestHandleFailoverNotify_RejectsGet verifies that a non-POST request gets
// 405 and never reaches TakeOver, even when correctly signed.
func TestHandleFailoverNotify_RejectsGet(t *testing.T) {
	roles := &fakeRoles{}
	s := newNotifyTestServer(roles)
	req := signedPeerRequest(http.MethodGet, "/failover_notify", constants.AuthPayloadFailoverNotify, notifySecret, nil)
	requireNotifyRejected(t, s.handleFailoverNotify, req, roles, http.StatusMethodNotAllowed)
}

// TestHandleFailbackNotify_RejectsUnsigned verifies that an unsigned release
// request gets 401 and never reaches ReleaseForFailback.
func TestHandleFailbackNotify_RejectsUnsigned(t *testing.T) {
	roles := &fakeRoles{}
	s := newNotifyTestServer(roles)
	req := httptest.NewRequest(http.MethodPost, "/failback_notify", nil)
	requireNotifyRejected(t, s.handleFailbackNotify, req, roles, http.StatusUnauthorized)
}
