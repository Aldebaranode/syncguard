package config_test

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"testing"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/constants"
	log "github.com/sirupsen/logrus"
)

func TestConfig_Load(t *testing.T) {
	tmpDir := t.TempDir()
	configPath := filepath.Join(tmpDir, "config.yaml")

	validConfig := fmt.Sprintf(`
secret: "test-secret"
node:
  id: "test-validator"
  role: "active"
  is_primary: true
  port: 8080

peers:
  - id: "peer-1"
    address: "192.168.1.2:8080"

cometbft:
  rpc_url: "http://localhost:26657"
  validator_address: "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
  state_path: "/tmp/state.json"
  backup_path: %q

health:
  interval: 5
  min_peers: 3
  timeout: 5

failover:
  retry_attempts: 3
  grace_period: 60
  state_sync_interval: 5

logging:
  level: "info"
  file: "/tmp/test.log"
  verbose: false
`, tmpDir)

	if err := os.WriteFile(configPath, []byte(validConfig), 0644); err != nil {
		t.Fatalf("Failed to write test config: %v", err)
	}

	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatalf("Failed to load config: %v", err)
	}

	// Verify loaded values
	if cfg.Node.ID != "test-validator" {
		t.Errorf("Node.ID = %s, want test-validator", cfg.Node.ID)
	}
	if cfg.Node.Role != constants.NodeStatusActive {
		t.Errorf("Node.Role = %s, want active", cfg.Node.Role)
	}
	if !cfg.Node.IsPrimary {
		t.Error("Node.IsPrimary should be true")
	}
	if cfg.CometBFT.RPCURL != "http://localhost:26657" {
		t.Errorf("CometBFT.RPCURL = %s, want http://localhost:26657", cfg.CometBFT.RPCURL)
	}
	if len(cfg.Peers) != 1 {
		t.Errorf("Expected 1 peer, got %d", len(cfg.Peers))
	}
}

func TestConfig_LoadInvalid(t *testing.T) {
	tmpDir := t.TempDir()

	tests := []struct {
		name    string
		content string
		wantErr string
	}{
		{
			name: "missing node id",
			content: `
secret: "test-secret"
node:
  role: "active"
cometbft:
  rpc_url: "http://localhost:26657"
  state_path: "/tmp/state.json"
`,
			wantErr: "node.id is required",
		},
		{
			name: "invalid role",
			content: `
secret: "test-secret"
node:
  id: "test"
  role: "primary"
cometbft:
  rpc_url: "http://localhost:26657"
  state_path: "/tmp/state.json"
`,
			wantErr: "node.role must be 'active' or 'passive'",
		},
		{
			name: "missing cometbft rpc_url",
			content: `
secret: "test-secret"
node:
  id: "test"
  role: "active"
cometbft:
  state_path: "/tmp/state.json"
`,
			wantErr: "cometbft.rpc_url is required",
		},
		{
			name: "missing state_path",
			content: `
secret: "test-secret"
node:
  id: "test"
  role: "active"
cometbft:
  rpc_url: "http://localhost:26657"
`,
			wantErr: "cometbft.state_path is required",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			configPath := filepath.Join(tmpDir, tt.name+".yaml")
			if err := os.WriteFile(configPath, []byte(tt.content), 0644); err != nil {
				t.Fatalf("Failed to write test config: %v", err)
			}

			_, err := config.Load(configPath)
			if err == nil {
				t.Errorf("Expected error containing %q, got nil", tt.wantErr)
				return
			}
			if !containsString(err.Error(), tt.wantErr) {
				t.Errorf("Error = %v, want containing %q", err, tt.wantErr)
			}
		})
	}
}

func TestConfig_Defaults(t *testing.T) {
	tmpDir := t.TempDir()
	configPath := filepath.Join(tmpDir, "minimal.yaml")

	minimalConfig := `
secret: "test-secret"
node:
  id: "test"
cometbft:
  rpc_url: "http://localhost:26657"
  validator_address: "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
  state_path: "/tmp/state.json"
`

	if err := os.WriteFile(configPath, []byte(minimalConfig), 0644); err != nil {
		t.Fatalf("Failed to write test config: %v", err)
	}

	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatalf("Failed to load config: %v", err)
	}

	// Check defaults
	if cfg.Node.Role != constants.NodeStatusPassive {
		t.Errorf("Default role should be passive, got %s", cfg.Node.Role)
	}
	if cfg.Node.Port != 8080 {
		t.Errorf("Default port should be 8080, got %d", cfg.Node.Port)
	}
	if cfg.Health.Interval != 5 {
		t.Errorf("Default health interval should be 5, got %v", cfg.Health.Interval)
	}
	if cfg.Failover.RetryAttempts != 3 {
		t.Errorf("Default retry attempts should be 3, got %d", cfg.Failover.RetryAttempts)
	}
}

func TestConfig_IsActive(t *testing.T) {
	cfg := &config.Config{
		Node: config.NodeConfig{Role: "active"},
	}
	if !cfg.IsActive() {
		t.Error("IsActive() should return true for active role")
	}

	cfg.Node.Role = constants.NodeStatusPassive
	if cfg.IsActive() {
		t.Error("IsActive() should return false for passive role")
	}
}

func containsString(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(s) > 0 && containsString(s[1:], substr) || s[:len(substr)] == substr)
}

// TestConfig_RequiresValidatorAddress verifies that a config without the
// validator address is rejected, so syncguard can tell the real key from a mock.
func TestConfig_RequiresValidatorAddress(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "no-address.yaml")
	content := `
secret: "test-secret"
node:
  id: "test"
  role: "active"
cometbft:
  rpc_url: "http://localhost:26657"
  state_path: "/tmp/state.json"
`
	if err := os.WriteFile(configPath, []byte(content), 0644); err != nil {
		t.Fatalf("Failed to write test config: %v", err)
	}

	_, err := config.Load(configPath)
	if err == nil {
		t.Fatal("expected an error for a missing validator_address, got nil")
	}
	if !containsString(err.Error(), "validator_address") {
		t.Errorf("Error = %v, want containing validator_address", err)
	}
}

// TestConfig_LogTimestampsHaveMilliseconds verifies that log lines carry
// millisecond timestamps, which the failover timing measurements need.
func TestConfig_LogTimestampsHaveMilliseconds(t *testing.T) {
	tmpDir := t.TempDir()
	logPath := filepath.Join(tmpDir, "syncguard.log")
	configPath := filepath.Join(tmpDir, "config.yaml")

	configYAML := fmt.Sprintf(`
secret: "test-secret"
node:
  id: "test-validator"
  role: "active"
  is_primary: true
  port: 8080

peers:
  - id: "peer-1"
    address: "192.168.1.2:8080"

cometbft:
  rpc_url: "http://localhost:26657"
  validator_address: "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
  state_path: "/tmp/state.json"
  backup_path: %q

logging:
  level: "info"
  file: %q
`, tmpDir, logPath)

	if err := os.WriteFile(configPath, []byte(configYAML), 0644); err != nil {
		t.Fatalf("Failed to write test config: %v", err)
	}
	if _, err := config.Load(configPath); err != nil {
		t.Fatalf("Failed to load config: %v", err)
	}

	log.Info("timestamp probe")

	content, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatalf("Failed to read log file: %v", err)
	}
	millis := regexp.MustCompile(`time="\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}"`)
	if !millis.Match(content) {
		t.Fatalf("log line lacks a millisecond timestamp:\n%s", content)
	}
}

// TestConfig_RejectsBackupPathThatIsNotADirectory verifies that a backup_path
// that is a file or does not exist is rejected, because key and state backups
// are written into it as a directory.
func TestConfig_RejectsBackupPathThatIsNotADirectory(t *testing.T) {
	tmpDir := t.TempDir()
	regularFile := filepath.Join(tmpDir, "backup.json")
	if err := os.WriteFile(regularFile, []byte("{}"), 0600); err != nil {
		t.Fatalf("Failed to write backup file: %v", err)
	}

	for name, backupPath := range map[string]string{
		"regular file": regularFile,
		"missing path": filepath.Join(tmpDir, "missing"),
	} {
		t.Run(name, func(t *testing.T) {
			configPath := filepath.Join(t.TempDir(), "config.yaml")
			content := fmt.Sprintf(`
secret: "test-secret"
node:
  id: "test"
cometbft:
  rpc_url: "http://localhost:26657"
  validator_address: "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
  state_path: "/tmp/state.json"
  backup_path: %q
`, backupPath)
			if err := os.WriteFile(configPath, []byte(content), 0644); err != nil {
				t.Fatalf("Failed to write test config: %v", err)
			}

			_, err := config.Load(configPath)
			if err == nil {
				t.Fatal("expected an error for a backup_path that is not a directory, got nil")
			}
			if !containsString(err.Error(), "cometbft.backup_path must be an existing directory") {
				t.Errorf("Error = %v, want containing %q", err, "cometbft.backup_path must be an existing directory")
			}
		})
	}
}
