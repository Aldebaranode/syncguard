# Failover Lifecycle

> Peer-to-peer key handoff during failover and failback.

## Failover (Active → Passive)

Triggered when health check failures exceed `retry_attempts` threshold.

```mermaid
sequenceDiagram
    participant A as Active Node
    participant P as Passive Node

    Note over A: Health failures >= threshold

    A->>P: POST /validator_key (transfer key)
    P-->>A: 200 OK

    A->>A: Delete local key (backup + mock)
    A->>A: Restart validator (loads mock key)

    A->>P: POST /failover_notify
    Note over P: Acquires lock, restarts with real key

    Note over A: Now Passive
    Note over P: Now Active
```

---

## Failback (Passive → Active)

Triggered when primary node recovers after `grace_period`.

```mermaid
sequenceDiagram
    participant P as Passive Node (Primary)
    participant A as Active Node (Secondary)

    Note over P: isPrimary && healthy after grace_period

    P->>A: GET /validator_key
    A-->>P: keyData
    P->>P: Save received key

    P->>A: GET /validator_state
    A-->>P: stateData
    P->>P: Sync state, acquire lock

    P->>P: Restart validator (loads real key)

    P->>A: POST /failback_notify
    Note over A: Deletes key, restarts, releases lock

    Note over P: Now Active
    Note over A: Now Passive
```
