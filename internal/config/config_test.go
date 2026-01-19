package config_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/constants"
)

func TestConfig_Load(t *testing.T) {
	tmpDir := t.TempDir()
	configPath := filepath.Join(tmpDir, "config.yaml")

	validConfig := `
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
  state_path: "/tmp/state.json"
  backup_path: "/tmp/backup.json"

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
`

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
node:
  id: "test"
cometbft:
  rpc_url: "http://localhost:26657"
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
