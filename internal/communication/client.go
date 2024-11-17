package communication

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/aldebaranode/syncguard/internal/config"
	"github.com/aldebaranode/syncguard/internal/logger"
)

// Client handles communication with peer nodes
type Client struct {
	Timeout time.Duration
	logger  *logger.Logger
}

// NewClient initializes a new Client with a specified timeout
func NewClient(cfg *config.Config, timeout time.Duration) *Client {
	newLogger := logger.NewLogger(cfg)
	newLogger.WithModule("communication client")
	return &Client{
		Timeout: timeout,
		logger:  newLogger,
	}
}

// SendHealthUpdate sends a health status update to a peer node
func (c *Client) SendHealthUpdate(peerAddress string, status HealthStatus) error {
	data, err := json.Marshal(status)
	if err != nil {
		return fmt.Errorf("failed to marshal health status: %w", err)
	}

	url := fmt.Sprintf("http://%s/health_update", peerAddress)
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewBuffer(data))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: c.Timeout}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send health update to %s: %w", peerAddress, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("failed to send health update to %s: received status %d", peerAddress, resp.StatusCode)
	}

	return nil
}

// TriggerFailover sends a failover trigger to a peer node
func (c *Client) TriggerFailover(peerAddress, nodeID string) error {
	data, err := json.Marshal(map[string]string{
		"node_id": nodeID,
	})
	if err != nil {
		return fmt.Errorf("failed to marshal failover trigger data: %w", err)
	}

	url := fmt.Sprintf("http://%s/trigger_failover", peerAddress)
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewBuffer(data))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: c.Timeout}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send failover trigger to %s: %w", peerAddress, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("failed to send failover trigger to %s: received status %d", peerAddress, resp.StatusCode)
	}

	return nil
}
