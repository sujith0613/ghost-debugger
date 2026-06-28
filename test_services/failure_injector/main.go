package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/sujithm/ghost-debugger/test_services/shared"
)

var (
	logger        *slog.Logger
	mu            sync.RWMutex
	failureStates = make(map[string]shared.FailureConfig)
)

func main() {
	servicePort := getEnv("INJECTOR_PORT", "8099")

	logger = slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))

	mux := http.NewServeMux()

	mux.HandleFunc("/inject", handleInject)
	mux.HandleFunc("/reset", handleReset)
	mux.HandleFunc("/state/", handleGetState)
	mux.HandleFunc("/health", handleHealth)

	server := &http.Server{
		Addr:         ":" + servicePort,
		Handler:      withLogging(mux),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 30 * time.Second,
	}

	go func() {
		logger.Info("failure_injector starting", "port", servicePort)
		if err := server.ListenAndServe(); err != http.ErrServerClosed {
			logger.Error("server error", "error", err)
			os.Exit(1)
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	logger.Info("shutdown signal received")

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	server.Shutdown(shutdownCtx)
	logger.Info("failure_injector stopped")
}

type InjectRequest struct {
	Service      string  `json:"service"`
	Type         string  `json:"type"`
	DurationMs   int     `json:"duration_ms,omitempty"`
	ErrorRate    float64 `json:"error_rate,omitempty"`
	MemoryMB     int     `json:"memory_mb,omitempty"`
	DurationSec  int     `json:"duration_seconds"`
}

func handleInject(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req InjectRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}

	if req.Service == "" {
		http.Error(w, "service is required", http.StatusBadRequest)
		return
	}

	config := shared.FailureConfig{
		Type:      shared.FailureType(req.Type),
		ExpiresAt: time.Now().Add(time.Duration(req.DurationSec) * time.Second),
	}

	switch config.Type {
	case shared.FailureLatency:
		config.DurationMs = req.DurationMs
	case shared.FailureErrorRate:
		config.ErrorRate = req.ErrorRate
	case shared.FailureMemoryPressure:
		config.MemoryMB = req.MemoryMB
	}

	mu.Lock()
	failureStates[req.Service] = config
	mu.Unlock()

	logger.Info("failure injected",
		"service", req.Service,
		"type", config.Type,
		"expires_in_seconds", req.DurationSec,
	)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":     "ok",
		"service":    req.Service,
		"type":       config.Type,
		"expires_at": config.ExpiresAt.Format(time.RFC3339),
	})
}

func handleReset(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	mu.Lock()
	failureStates = make(map[string]shared.FailureConfig)
	mu.Unlock()

	logger.Info("all failures reset")

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":  "ok",
		"message": "all failures cleared",
	})
}

func handleGetState(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	service := strings.TrimPrefix(r.URL.Path, "/state/")
	if service == "" {
		http.Error(w, "service is required", http.StatusBadRequest)
		return
	}

	mu.RLock()
	config, exists := failureStates[service]
	mu.RUnlock()

	if !exists || time.Now().After(config.ExpiresAt) {
		config = shared.FailureConfig{Type: shared.FailureNone}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(config)
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":  "ok",
		"service": "failure_injector",
	})
}

func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		logger.Info("http_request",
			"method", r.Method,
			"path", r.URL.Path,
			"duration_ms", time.Since(start).Milliseconds(),
		)
	})
}

func getEnv(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}
