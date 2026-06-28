package main

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	agentpb "github.com/sujithm/ghost-debugger/proto/agent"
	telemetrypb "github.com/sujithm/ghost-debugger/proto/telemetry"
	"github.com/sujithm/ghost-debugger/gateway/circuitbreaker"
	"github.com/sujithm/ghost-debugger/gateway/incident"
	"github.com/sujithm/ghost-debugger/gateway/metrics"
	"github.com/sujithm/ghost-debugger/gateway/ratelimiter"
	"github.com/sujithm/ghost-debugger/gateway/router"
	"github.com/sujithm/ghost-debugger/gateway/telemetry"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	oteltrace "go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type GatewayServer struct {
	telemetrypb.UnimplementedTelemetryServiceServer

	ratelimiter    *ratelimiter.PerServiceLimiter
	circuitbreaker *circuitbreaker.CircuitBreaker
	detector       *incident.Detector
	agentRouter    *router.AgentRouter
}

func NewGatewayServer() *GatewayServer {
	return &GatewayServer{
		ratelimiter:    ratelimiter.NewPerServiceLimiter(10000, 1000, time.Second),
		circuitbreaker: circuitbreaker.NewCircuitBreaker(5, 30*time.Second),
		detector:       incident.NewDetector(60*time.Second, 10),
		agentRouter:    router.NewAgentRouter("localhost:9001"),
	}
}

func (s *GatewayServer) IngestTrace(ctx context.Context, req *telemetrypb.TraceIngestionRequest) (*telemetrypb.IngestionResponse, error) {
	start := time.Now()
	service := req.SourceService

	tracer := telemetry.Tracer("server")
	ctx, span := tracer.Start(ctx, "gateway.ingest_trace",
		oteltrace.WithAttributes(
			attribute.String("source_service", service),
			attribute.String("trace_id", req.TraceId),
			attribute.Int("span_count", len(req.Spans)),
		),
	)
	defer span.End()

	if !s.ratelimiter.Allow(service) {
		metrics.RecordIngestion(service, "trace", "rate_limited", time.Since(start))
		metrics.RecordRateLimit(service, "trace")
		span.SetAttributes(attribute.String("rate_limit.status", "rejected"))
		span.SetStatus(otelcodes.Error, "rate limited")
		slog.Warn("trace ingestion rate limited", "service", service)
		return nil, status.Errorf(codes.ResourceExhausted,
			"rate limit exceeded for service: %s", service)
	}

	id := fmt.Sprintf("trace-%s-%d", req.TraceId, time.Now().UnixNano())
	span.SetAttributes(attribute.String("ingestion_id", id))

	for _, sp := range req.Spans {
		if sp.IsError {
			s.detector.RecordError(sp.ServiceName)
		}
	}

	if inc, detected := s.detector.Check(service); detected {
		slog.Warn("incident detected",
			"incident_id", inc.ID,
			"service", inc.Description,
			"severity", inc.Severity,
		)
		metrics.RecordIncidentDetected(inc.TriggerType)
		s.maybeTriggerAnalysis(inc)
	}

	metrics.RecordIngestion(service, "trace", "accepted", time.Since(start))
	metrics.UpdateActiveServices(s.ratelimiter.ActiveServices())
	span.SetStatus(otelcodes.Ok, "accepted")

	return &telemetrypb.IngestionResponse{
		Status:       "ok",
		IngestionId:  id,
		Message:      fmt.Sprintf("ingested %d spans", len(req.Spans)),
	}, nil
}

func (s *GatewayServer) IngestLog(ctx context.Context, req *telemetrypb.LogIngestionRequest) (*telemetrypb.IngestionResponse, error) {
	start := time.Now()
	service := req.ServiceName

	tracer := telemetry.Tracer("server")
	ctx, span := tracer.Start(ctx, "gateway.ingest_log",
		oteltrace.WithAttributes(
			attribute.String("source_service", service),
			attribute.String("log_id", req.LogId),
			attribute.String("level", req.Level),
		),
	)
	defer span.End()

	if !s.ratelimiter.Allow(service) {
		metrics.RecordIngestion(service, "log", "rate_limited", time.Since(start))
		metrics.RecordRateLimit(service, "log")
		span.SetAttributes(attribute.String("rate_limit.status", "rejected"))
		span.SetStatus(otelcodes.Error, "rate limited")
		slog.Warn("log ingestion rate limited", "service", service)
		return nil, status.Errorf(codes.ResourceExhausted,
			"rate limit exceeded for service: %s", service)
	}

	id := fmt.Sprintf("log-%d", time.Now().UnixNano())
	span.SetAttributes(attribute.String("ingestion_id", id))

	if req.Level == "ERROR" || req.Level == "FATAL" {
		s.detector.RecordError(service)
	}

	if inc, detected := s.detector.Check(service); detected {
		slog.Warn("incident detected via logs",
			"incident_id", inc.ID,
			"service", inc.Description,
		)
		metrics.RecordIncidentDetected(inc.TriggerType)
		s.maybeTriggerAnalysis(inc)
	}

	metrics.RecordIngestion(service, "log", "accepted", time.Since(start))
	metrics.UpdateActiveServices(s.ratelimiter.ActiveServices())
	span.SetStatus(otelcodes.Ok, "accepted")

	return &telemetrypb.IngestionResponse{
		Status:      "ok",
		IngestionId: id,
		Message:     fmt.Sprintf("ingested log entry: %s", req.LogId),
	}, nil
}

func (s *GatewayServer) IngestMetric(ctx context.Context, req *telemetrypb.MetricIngestionRequest) (*telemetrypb.IngestionResponse, error) {
	start := time.Now()
	service := req.SourceService

	tracer := telemetry.Tracer("server")
	ctx, span := tracer.Start(ctx, "gateway.ingest_metric",
		oteltrace.WithAttributes(
			attribute.String("source_service", service),
			attribute.Int("point_count", len(req.Points)),
		),
	)
	defer span.End()

	if !s.ratelimiter.Allow(service) {
		metrics.RecordIngestion(service, "metric", "rate_limited", time.Since(start))
		metrics.RecordRateLimit(service, "metric")
		span.SetAttributes(attribute.String("rate_limit.status", "rejected"))
		span.SetStatus(otelcodes.Error, "rate limited")
		slog.Warn("metric ingestion rate limited", "service", service)
		return nil, status.Errorf(codes.ResourceExhausted,
			"rate limit exceeded for service: %s", service)
	}

	id := fmt.Sprintf("metric-%d", time.Now().UnixNano())
	span.SetAttributes(attribute.String("ingestion_id", id))

	metrics.RecordIngestion(service, "metric", "accepted", time.Since(start))
	metrics.UpdateActiveServices(s.ratelimiter.ActiveServices())
	span.SetStatus(otelcodes.Ok, "accepted")

	return &telemetrypb.IngestionResponse{
		Status:      "ok",
		IngestionId: id,
		Message:     fmt.Sprintf("ingested %d data points", len(req.Points)),
	}, nil
}

func (s *GatewayServer) maybeTriggerAnalysis(inc *incident.Incident) {
	if !s.circuitbreaker.Allow() {
		metrics.RecordCircuitBreakerBlock()
		slog.Warn("circuit breaker open, skipping agent invocation",
			"incident_id", inc.ID)
		return
	}

	metrics.RecordIncidentDispatched()

	go func() {
		tracer := telemetry.Tracer("incident")
		_, span := tracer.Start(context.Background(), "gateway.trigger_analysis",
			oteltrace.WithAttributes(
				attribute.String("incident_id", inc.ID),
				attribute.String("trigger_type", inc.TriggerType),
				attribute.StringSlice("services", inc.Services),
				attribute.Int64("detected_at_ns", inc.DetectedAt.UnixNano()),
			),
		)

		analysisStart := time.Now()

		resp, err := s.agentRouter.Analyze(context.Background(), &agentpb.AnalysisRequest{
			IncidentId:         inc.ID,
			TriggerType:        inc.TriggerType,
			TriggerDescription: inc.Description,
			Services:           inc.Services,
			DetectedAtNs:       inc.DetectedAt.UnixNano(),
			AnalysisWindowNs:   5 * time.Minute.Nanoseconds(),
		})
		if err != nil {
			s.circuitbreaker.Failure()
			duration := time.Since(analysisStart)
			metrics.RecordIncidentComplete(false, duration)
			span.SetAttributes(attribute.String("error", err.Error()))
			span.SetStatus(otelcodes.Error, "analysis failed")
			span.End()
			slog.Error("agent analysis failed", "incident_id", inc.ID, "error", err)
			return
		}
		s.circuitbreaker.Success()
		duration := time.Since(analysisStart)
		metrics.RecordIncidentComplete(true, duration)
		span.SetAttributes(
			attribute.String("root_cause", resp.RootCause),
			attribute.Int64("duration_ms", resp.AnalysisDurationMs),
		)
		span.SetStatus(otelcodes.Ok, "completed")
		span.End()
		slog.Info("agent analysis complete",
			"incident_id", inc.ID,
			"root_cause", resp.RootCause,
			"duration_ms", resp.AnalysisDurationMs,
		)
	}()
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, `{"status":"ok","service":"ghost-debugger-gateway"}`)
}

func metricsHandler(w http.ResponseWriter, r *http.Request) {
	promhttp.Handler().ServeHTTP(w, r)
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))

	ctx := context.Background()
	otelShutdown, err := telemetry.Init(ctx, slog.Default())
	if err != nil {
		slog.Error("failed to initialize OpenTelemetry", "error", err)
		os.Exit(1)
	}
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		if err := otelShutdown(shutdownCtx); err != nil {
			slog.Error("failed to shutdown OTel", "error", err)
		}
	}()

	server := NewGatewayServer()
	if err := server.agentRouter.Connect(); err != nil {
		slog.Warn("agent service not available at startup, will retry on incidents", "error", err)
	}
	defer server.agentRouter.Close()

	grpcPort := "9000"
	httpPort := "9090"

	grpcListener, err := net.Listen("tcp", ":"+grpcPort)
	if err != nil {
		slog.Error("failed to listen", "port", grpcPort, "error", err)
		os.Exit(1)
	}

	grpcServer := grpc.NewServer(
		grpc.UnaryInterceptor(func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
			start := time.Now()
			tracer := telemetry.Tracer("grpc")
			ctx, span := tracer.Start(ctx, "gateway.grpc."+info.FullMethod,
				oteltrace.WithAttributes(
					attribute.String("rpc.method", info.FullMethod),
				),
			)
			defer span.End()

			resp, err := handler(ctx, req)
			duration := time.Since(start)

			if err != nil {
				span.SetAttributes(attribute.String("error", err.Error()))
				span.SetStatus(otelcodes.Error, err.Error())
				slog.Error("grpc request failed",
					"method", info.FullMethod,
					"duration_ms", duration.Milliseconds(),
					"error", err,
				)
			} else {
				span.SetStatus(otelcodes.Ok, "ok")
				slog.Debug("grpc request completed",
					"method", info.FullMethod,
					"duration_ms", duration.Milliseconds(),
				)
			}
			return resp, err
		}),
	)

	telemetrypb.RegisterTelemetryServiceServer(grpcServer, server)

	mux := http.NewServeMux()
	mux.HandleFunc("/health", healthHandler)
	mux.HandleFunc("/metrics", metricsHandler)

	httpServer := &http.Server{
		Addr:    ":" + httpPort,
		Handler: mux,
	}

	slog.Info("starting ghost-debugger gateway",
		"grpc_port", grpcPort,
		"http_port", httpPort,
	)

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("http server failed", "error", err)
		}
	}()

	go func() {
		if err := grpcServer.Serve(grpcListener); err != nil {
			slog.Error("grpc server failed", "error", err)
		}
	}()

	<-sigCh
	slog.Info("shutting down...")

	grpcServer.GracefulStop()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	httpServer.Shutdown(shutdownCtx)
}


