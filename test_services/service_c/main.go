package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/sujithm/ghost-debugger/test_services/shared"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

var (
	telemetry     *shared.Telemetry
	failureClient *shared.FailureInjectorClient
	tracer        trace.Tracer
)

func main() {
	serviceName := getEnv("SERVICE_NAME", "service_c")
	servicePort := getEnv("SERVICE_PORT", "8083")
	otlpEndpoint := getEnv("JAEGER_ENDPOINT", "gateway:4317")
	injectorURL := getEnv("FAILURE_INJECTOR_URL", "http://failure_injector:8099")

	var err error
	telemetry, err = shared.InitTelemetry(shared.Config{
		ServiceName:      serviceName,
		ServiceVersion:   "1.0.0",
		OtlpEndpoint:     otlpEndpoint,
		LogLevel:         slog.LevelInfo,
		EnablePrometheus: true,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "failed to initialize telemetry: %v\n", err)
		os.Exit(1)
	}
	defer telemetry.Shutdown(context.Background())

	tracer = otel.Tracer(serviceName)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	failureClient = shared.NewFailureInjectorClient(injectorURL, serviceName, telemetry.Logger)
	go failureClient.StartPolling(ctx)

	mux := http.NewServeMux()

	mux.Handle("/api/process",
		otelhttp.NewHandler(http.HandlerFunc(handleProcess), "service_c.process"),
	)

	mux.HandleFunc("/health", handleHealth)
	mux.Handle("/metrics", telemetry.MetricsHandler)

	server := &http.Server{
		Addr:         ":" + servicePort,
		Handler:      withLogging(mux),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 30 * time.Second,
	}

	go func() {
		telemetry.Logger.Info("service_c starting", "port", servicePort)
		if err := server.ListenAndServe(); err != http.ErrServerClosed {
			telemetry.Logger.Error("server error", "error", err)
			os.Exit(1)
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	telemetry.Logger.Info("shutdown signal received")
	cancel()

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	server.Shutdown(shutdownCtx)
	telemetry.Logger.Info("service_c stopped")
}

func handleProcess(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	ctx := r.Context()
	span := trace.SpanFromContext(ctx)

	telemetry.IncRequestsInFlight()
	defer func() {
		telemetry.DecRequestsInFlight()
		telemetry.RecordHTTPMetrics(r.Method, r.URL.Path, 200, time.Since(start))
	}()

	if latency := failureClient.State().GetLatency(); latency > 0 {
		span.AddEvent("failure_injection: latency",
			trace.WithAttributes(attribute.Int64("duration_ms", latency.Milliseconds())),
		)
		time.Sleep(latency)
	}

	if failureClient.State().ShouldFail() {
		span.SetStatus(codes.Error, "injected failure")
		span.RecordError(fmt.Errorf("injected error"))
		telemetry.RecordHTTPMetrics(r.Method, r.URL.Path, 500, time.Since(start))
		http.Error(w, "injected failure", http.StatusInternalServerError)
		return
	}

	time.Sleep(5 * time.Millisecond)

	span.SetAttributes(
		attribute.String("service_c.result", "success"),
		attribute.Int("http.status_code", 200),
	)
	span.SetStatus(codes.Ok, "")

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"processed_by": "service_c",
		"message":      "Hello from service_c",
		"trace_id":     span.SpanContext().TraceID().String(),
		"timestamp":    time.Now().Format(time.RFC3339),
	})
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":  "ok",
		"service": "service_c",
	})
}

func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		duration := time.Since(start)
		span := trace.SpanFromContext(r.Context())
		slog.Info("http_request",
			"method", r.Method,
			"path", r.URL.Path,
			"duration_ms", duration.Milliseconds(),
			"trace_id", span.SpanContext().TraceID().String(),
		)
	})
}

func getEnv(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}
