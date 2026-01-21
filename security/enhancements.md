# SyncGuard Security Enhancements & Roadmap

This document outlines known security improvements and potential enhancements for the SyncGuard key transfer system.

## 1. Current Security Posture (v1)
- **Encryption**: AES-256-GCM (Authenticated Encryption).
- **Authentication**: HMAC-SHA256 (Shared Cluster Secret).
- **Protection**: Replay Protection (via Timestamp), Integrity (via GCM Tag).

## 2. Immediate Enhancements (Low Effort, High Value)
- [ ] **Memory Zeroing**: Explicitly overwrite sensitive byte slices (`key`, `salt`, `secret`) with zeros after use to prevent memory dumps from leaking secrets.
- [ ] **Environment Variable Scrubbing**: Ensure the `CLUSTER_SECRET` env var is unset after initial config load.
- [ ] **Key Rotation Support**: Allow the Cluster Secret to be rotated without downtime (e.g., support a list of valid secrets: `[current, previous]`).

## 3. Recommended Upgrades (Medium Effort)
- [ ] **mTLS (Mutual TLS)**: 
    - *Why*: Adds a layer of transport security on top of application-layer encryption. Prevents passive network observers from even seeing the metadata (headers, timestamps).
    - *Plan*: Use a private CA to issue certs for Active/Passive nodes.
- [ ] **Rate Limiting**: 
    - *Why*: Prevent brute-force HMAC guessing or DoS attacks on the `/validator_key` endpoint.
    - *Plan*: Limit requests to 1 per second per IP.

## 4. Advanced Security (High Effort / Enterprise)
- [ ] **HSM / KMS Integration**: 
    - *Why*: Never hold the `ClusterSecret` in RAM. Use AWS KMS, HashiCorp Vault, or YubiHSM to perform the derivation/signing.
- [ ] **Audit Logging**: 
    - *Why*: Tamper-proof logs of exactly *who* requested the key and *when*, sent to a remote SIEM.
- [ ] **Secure Enclaves (SGX)**: 
    - *Why*: Perform the encryption/decryption inside a hardware enclave where even the OS kernel looks like an attacker.
