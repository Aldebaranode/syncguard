package manager

import (
	"fmt"
	"net/http"

	"github.com/aldebaranode/syncguard/internal/server"
)

// Reason codes of a refused or failed role transition. The peer server sends
// the code in the answer body.
const (
	CodeNotHealthy      = "not_healthy"
	CodeNoValidatorKey  = "no_validator_key"
	CodeLockUnavailable = "lock_unavailable"
	CodeRestartFailed   = "restart_failed"
	CodeDisarmFailed    = "disarm_failed"
)

// transitionStatus maps each reason code to the HTTP status of the answer: a
// refusal the node made on purpose is a conflict, a failed action is a server
// error.
var transitionStatus = map[string]int{
	CodeNotHealthy:      http.StatusConflict,
	CodeNoValidatorKey:  http.StatusConflict,
	CodeLockUnavailable: http.StatusConflict,
	CodeRestartFailed:   http.StatusInternalServerError,
	CodeDisarmFailed:    http.StatusInternalServerError,
}

// TransitionError is a refused or failed role transition: Code names the
// reason, Status is the HTTP status the peer server answers with, and Err is
// the cause.
type TransitionError struct {
	Code   string
	Status int
	Err    error
}

// The peer server maps a TransitionError through this interface.
var _ server.TransitionFailure = (*TransitionError)(nil)

// newTransitionError wraps cause with code and the status that code maps to.
func newTransitionError(code string, cause error) *TransitionError {
	status, known := transitionStatus[code]
	if !known {
		status = http.StatusInternalServerError
	}
	return &TransitionError{Code: code, Status: status, Err: cause}
}

// Error returns the code followed by the cause.
func (e *TransitionError) Error() string {
	return fmt.Sprintf("%s: %v", e.Code, e.Err)
}

// Unwrap returns the cause.
func (e *TransitionError) Unwrap() error {
	return e.Err
}

// HTTPStatus returns the status the peer server answers with.
func (e *TransitionError) HTTPStatus() int {
	return e.Status
}

// ReasonCode returns the code the peer server puts in the answer body.
func (e *TransitionError) ReasonCode() string {
	return e.Code
}
