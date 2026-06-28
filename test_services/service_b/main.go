package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
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
	serviceCURL   string
	tracer        trace.Tracer
)

func main() {
	serviceName := getEnv("SERVICE_NAME", "service_b")
	servicePort := getEnv("SERVICE_PORT", "8082")
	serviceCURL = getEnv("DOWNSTREAM_SERVICE", "http://service_c:8083")
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
		otelhttp.NewHandler(http.HandlerFunc(handleProcess), "service_b.process"),
	)

	mux.Handle("/api/db/query",
		otelhttp.NewHandler(http.HandlerFunc(handleDBQuery), "service_b.db_query"),
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
		telemetry.Logger.Info("service_b starting",
			"port", servicePort,
			"downstream", serviceCURL,
		)
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
	telemetry.Logger.Info("service_b stopped")
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
			trace.WithAttributes(
				attribute.String("injection.type", "latency"),
				attribute.Int64("injection.duration_ms", latency.Milliseconds()),
			),
		)
		time.Sleep(latency)
	}

	if failureClient.State().ShouldFail() {
		span.SetStatus(codes.Error, "injected failure")
		span.RecordError(fmt.Errorf("injected error rate failure"))
		telemetry.RecordHTTPMetrics(r.Method, r.URL.Path, 500, time.Since(start))
		http.Error(w, "injected failure", http.StatusInternalServerError)
		return
	}

	req, err := http.NewRequestWithContext(ctx, "GET", serviceCURL+"/api/process", nil)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "failed to create downstream request")
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	client := &http.Client{
		Transport: otelhttp.NewTransport(http.DefaultTransport),
		Timeout:   10 * time.Second,
	}

	resp, err := client.Do(req)
	if err != nil {
		span.RecordError(fmt.Errorf("service_c call failed: %w", err))
		span.SetStatus(codes.Error, "downstream service_c failed")
		http.Error(w, "service_c unavailable", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)

	var cResult map[string]interface{}
	json.Unmarshal(body, &cResult)

	result := map[string]interface{}{
		"processed_by":      "service_b",
		"service_c_result":  cResult,
		"trace_id":          span.SpanContext().TraceID().String(),
		"db_query_duration": simulateDBQuery(span),
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)

	span.SetStatus(codes.Ok, "")
}

func handleDBQuery(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	ctx := r.Context()
	span := trace.SpanFromContext(ctx)

	telemetry.IncRequestsInFlight()
	defer telemetry.DecRequestsInFlight()

	_, dbSpan := tracer.Start(ctx, "service_b.database_query",
		trace.WithAttributes(
			attribute.String("db.system", "postgresql"),
			attribute.String("db.operation", "SELECT"),
			attribute.String("db.table", "orders"),
		),
	)

	if latency := failureClient.State().GetLatency(); latency > 0 {
		dbSpan.AddEvent("failure_injection: db_latency",
			trace.WithAttributes(attribute.Int64("duration_ms", latency.Milliseconds())),
		)
		time.Sleep(latency)
	}

	time.Sleep(10 * time.Millisecond)

	dbSpan.SetStatus(codes.Ok, "")
	dbSpan.End()

	telemetry.RecordHTTPMetrics(r.Method, r.URL.Path, 200, time.Since(start))

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"query":         "SELECT * FROM orders",
		"rows_returned": 42,
		"duration_ms":   time.Since(start).Milliseconds(),
		"trace_id":      span.SpanContext().TraceID().String(),
	})
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":  "ok",
		"service": "service_b",
	})
}

func simulateDBQuery(span trace.Span) float64 {
	start := time.Now()
	time.Sleep(time.Duration(5+time.Now().UnixMilli()%20) * time.Millisecond)
	return float64(time.Since(start).Milliseconds())
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
