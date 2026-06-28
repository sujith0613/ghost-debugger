package shared

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"time"
)

type FailureType string

const (
	FailureNone          FailureType = "none"
	FailureLatency       FailureType = "latency"
	FailureErrorRate     FailureType = "error_rate"
	FailureKill          FailureType = "kill"
	FailureMemoryPressure FailureType = "memory_pressure"
)

type FailureConfig struct {
	Type       FailureType `json:"type"`
	DurationMs int         `json:"duration_ms,omitempty"`
	ErrorRate  float64     `json:"error_rate,omitempty"`
	MemoryMB   int         `json:"memory_mb,omitempty"`
	ExpiresAt  time.Time   `json:"expires_at"`
}

type FailureState struct {
	mu     sync.RWMutex
	config FailureConfig
}

func NewFailureState() *FailureState {
	return &FailureState{
		config: FailureConfig{Type: FailureNone},
	}
}

func (fs *FailureState) Update(config FailureConfig) {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	fs.config = config
}

func (fs *FailureState) Get() FailureConfig {
	fs.mu.RLock()
	defer fs.mu.RUnlock()
	return fs.config
}

func (fs *FailureState) ShouldFail() bool {
	config := fs.Get()
	if config.Type != FailureErrorRate {
		return false
	}
	return float64(time.Now().UnixNano()%1000)/1000.0 < config.ErrorRate
}

func (fs *FailureState) GetLatency() time.Duration {
	config := fs.Get()
	if config.Type != FailureLatency {
		return 0
	}
	return time.Duration(config.DurationMs) * time.Millisecond
}

func (fs *FailureState) IsExpired() bool {
	config := fs.Get()
	return config.Type != FailureNone && time.Now().After(config.ExpiresAt)
}

type FailureInjectorClient struct {
	injectorURL string
	serviceName string
	state       *FailureState
	logger      *slog.Logger
}

func NewFailureInjectorClient(injectorURL, serviceName string, logger *slog.Logger) *FailureInjectorClient {
	return &FailureInjectorClient{
		injectorURL: injectorURL,
		serviceName: serviceName,
		state:       NewFailureState(),
		logger:      logger,
	}
}

func (c *FailureInjectorClient) State() *FailureState {
	return c.state
}

func (c *FailureInjectorClient) StartPolling(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	c.fetchOnce(ctx)

	for {
		select {
		case <-ctx.Done():
			c.logger.Info("stopping failure state polling", "service", c.serviceName)
			return
		case <-ticker.C:
			c.fetchOnce(ctx)
		}
	}
}

func (c *FailureInjectorClient) fetchOnce(ctx context.Context) {
	url := fmt.Sprintf("%s/state/%s", c.injectorURL, c.serviceName)

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		c.logger.Debug("failed to create failure state request", "error", err)
		return
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		c.logger.Debug("failed to fetch failure state", "error", err)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		c.logger.Debug("failure state fetch returned non-200", "status", resp.StatusCode)
		return
	}

	var config FailureConfig
	if err := json.NewDecoder(resp.Body).Decode(&config); err != nil {
		c.logger.Debug("failed to decode failure state", "error", err)
		return
	}

	if config.Type != FailureNone && time.Now().After(config.ExpiresAt) {
		c.state.Update(FailureConfig{Type: FailureNone})
		c.logger.Debug("failure expired — resetting to none", "service", c.serviceName)
		return
	}

	oldConfig := c.state.Get()
	c.state.Update(config)

	if config.Type != oldConfig.Type {
		c.logger.Info("failure state updated",
			"service", c.serviceName,
			"old_type", oldConfig.Type,
			"new_type", config.Type,
		)
	}
}
