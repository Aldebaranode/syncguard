package constants

// AuthPayloadValidatorKey is the signed payload of key-transfer requests.
const AuthPayloadValidatorKey = "SYNCGUARD_VALIDATOR_KEY"

// AuthPayloadFailoverNotify is the signed payload of takeover requests.
const AuthPayloadFailoverNotify = "SYNCGUARD_FAILOVER_NOTIFY"

// AuthPayloadFailbackNotify is the signed payload of failback release requests.
const AuthPayloadFailbackNotify = "SYNCGUARD_FAILBACK_NOTIFY"

// HTTP headers used to authenticate peer key-transfer requests.
const (
	HeaderSignature = "X-Syncguard-Signature"
	HeaderTimestamp = "X-Syncguard-Timestamp"
)

// KeyTransferTimeoutMs bounds how long a signed key-transfer request stays
// valid, preventing replay of captured requests.
const KeyTransferTimeoutMs = int64(30000)
