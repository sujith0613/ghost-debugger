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
	"github.com/sujithm/ghost-debugger/gateway/ratelimiter"
	"github.com/sujithm/ghost-debugger/gateway/router"
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
	if !s.ratelimiter.Allow(req.SourceService) {
		return nil, status.Errorf(codes.ResourceExhausted,
			"rate limit exceeded for service: %s", req.SourceService)
	}

	id := fmt.Sprintf("trace-%s-%d", req.TraceId, time.Now().UnixNano())

	for _, span := range req.Spans {
		if span.IsError {
			s.detector.RecordError(span.ServiceName)
		}
	}

	if inc, detected := s.detector.Check(req.SourceService); detected {
		slog.Warn("incident detected",
			"incident_id", inc.ID,
			"service", inc.Description,
			"severity", inc.Severity,
		)
		s.maybeTriggerAnalysis(inc)
	}

	return &telemetrypb.IngestionResponse{
		Status:       "ok",
		IngestionId:  id,
		Message:      fmt.Sprintf("ingested %d spans", len(req.Spans)),
	}, nil
}

func (s *GatewayServer) IngestLog(ctx context.Context, req *telemetrypb.LogIngestionRequest) (*telemetrypb.IngestionResponse, error) {
	if !s.ratelimiter.Allow(req.ServiceName) {
		return nil, status.Errorf(codes.ResourceExhausted,
			"rate limit exceeded for service: %s", req.ServiceName)
	}

	id := fmt.Sprintf("log-%d", time.Now().UnixNano())

	if req.Level == "ERROR" || req.Level == "FATAL" {
		s.detector.RecordError(req.ServiceName)
	}

	if inc, detected := s.detector.Check(req.ServiceName); detected {
		slog.Warn("incident detected via logs",
			"incident_id", inc.ID,
			"service", inc.Description,
		)
		s.maybeTriggerAnalysis(inc)
	}

	return &telemetrypb.IngestionResponse{
		Status:      "ok",
		IngestionId: id,
		Message:     fmt.Sprintf("ingested log entry: %s", req.LogId),
	}, nil
}

func (s *GatewayServer) IngestMetric(ctx context.Context, req *telemetrypb.MetricIngestionRequest) (*telemetrypb.IngestionResponse, error) {
	if !s.ratelimiter.Allow(req.SourceService) {
		return nil, status.Errorf(codes.ResourceExhausted,
			"rate limit exceeded for service: %s", req.SourceService)
	}

	id := fmt.Sprintf("metric-%d", time.Now().UnixNano())

	return &telemetrypb.IngestionResponse{
		Status:      "ok",
		IngestionId: id,
		Message:     fmt.Sprintf("ingested %d data points", len(req.Points)),
	}, nil
}

func (s *GatewayServer) maybeTriggerAnalysis(inc *incident.Incident) {
	if !s.circuitbreaker.Allow() {
		slog.Warn("circuit breaker open, skipping agent invocation",
			"incident_id", inc.ID)
		return
	}

	go func() {
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
			slog.Error("agent analysis failed", "incident_id", inc.ID, "error", err)
			return
		}
		s.circuitbreaker.Success()
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
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprint(w, `# HELP ghost_debugger_ingestion_total Total telemetry ingestion count
# TYPE ghost_debugger_ingestion_total counter
ghost_debugger_ingestion_total 0
`)
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))

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
			resp, err := handler(ctx, req)
			duration := time.Since(start)
			// Simulated metric — in production, use OpenTelemetry SDK
			_ = duration
			if err != nil {
				slog.Error("grpc request failed",
					"method", info.FullMethod,
					"duration_ms", duration.Milliseconds(),
					"error", err,
				)
			} else {
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


