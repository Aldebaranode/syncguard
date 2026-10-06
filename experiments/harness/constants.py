"""Fixed facts of the evaluation environment (spec section Environment facts, checked 2026-10-05)."""

from pathlib import Path

TARGET_VALIDATOR = "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
TARGET_PUBKEY_B64 = "AvLo+lkg0UWozoI+pJzv1a7upt+HaMxZCdWgRxvZ8Cb1"

RPC1_URL = "http://localhost:26657"
PRIMARY_RPC_URL = "http://localhost:21657"
BACKUP_RPC_URL = "http://localhost:22657"
STORY_API_URL = "http://localhost:1317"
PROMETHEUS_URL = "http://localhost:9090"
ACTIVE_PEER_URL = "http://localhost:8090"
PASSIVE_PEER_URL = "http://localhost:8081"

PRIMARY_CONTAINER = "validator1-node"
BACKUP_CONTAINER = "validator2-node"
CLOCK_CONTAINER = "validator3-node"
PRIMARY_IP = "10.0.0.21"  # compose address of validator1-node on the localnet network
PRIMARY_NODE = "validator-1"
BACKUP_NODE = "validator-2"
IMAGES = ("story-node:localnet", "story-geth:localnet")

BLOCK_ID_FLAG_ABSENT = 1
BLOCK_ID_FLAG_COMMIT = 2
BLOCK_ID_FLAG_NIL = 3
MIN_PEERS = 3  # health.min_peers of the evaluation config
TIP_MARGIN = 2
ROLE_CHANGE_SPACING_S = 90

EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
SYNCGUARD_DIR = EXPERIMENTS_DIR.parent
DATA_DIR = EXPERIMENTS_DIR / "data"
RESULTS_DIR = EXPERIMENTS_DIR / "results"
FIXTURES_DIR = EXPERIMENTS_DIR / "tests" / "fixtures"
RUNBOOK = EXPERIMENTS_DIR / "runbooks" / "manual-recovery.md"
ARCHIVED_SESSIONS_CSV = EXPERIMENTS_DIR / "archived-sessions.csv"
LOCALNET_DIR = Path("/Users/heed/Projects/story/story-localnet")
STORY_CONFIG_DIR = LOCALNET_DIR / "config" / "story"
