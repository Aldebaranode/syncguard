package constants

const AuthPayloadValidatorKey = "SYNCGUARD_VALIDATOR_KEY"

// HTTP headers used to authenticate peer key-transfer requests.
const (
	HeaderSignature = "X-Syncguard-Signature"
	HeaderTimestamp = "X-Syncguard-Timestamp"
)

// KeyTransferTimeoutMs bounds how long a signed key-transfer request stays
// valid, preventing replay of captured requests.
const KeyTransferTimeoutMs = int64(30000)
