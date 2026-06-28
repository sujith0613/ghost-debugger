package shared

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

var (
	httpRequestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "http_requests_total",
		Help: "Total HTTP requests by service, method, path, and status code.",
	}, []string{"service", "method", "path", "status"})

	httpRequestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "http_request_duration_seconds",
		Help:    "HTTP request duration in seconds.",
		Buckets: []float64{0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10},
	}, []string{"service", "method", "path"})

	httpRequestsInFlight = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "http_requests_in_flight",
		Help: "Current number of HTTP requests being processed.",
	}, []string{"service"})
)

type Telemetry struct {
	ServiceName    string
	TracerProvider *sdktrace.TracerProvider
	Logger         *slog.Logger
	MetricsHandler http.Handler
	Shutdown       func(context.Context) error
}

type Config struct {
	ServiceName      string
	ServiceVersion   string
	OtlpEndpoint     string
	LogLevel         slog.Level
	EnablePrometheus bool
}

func InitTelemetry(cfg Config) (*Telemetry, error) {
	ctx := context.Background()

	otlpEndpoint := cfg.OtlpEndpoint
	if otlpEndpoint == "" {
		otlpEndpoint = "localhost:4317"
	}

	conn, err := grpc.NewClient(
		otlpEndpoint,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithTimeout(5*time.Second),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create OTLP gRPC connection: %w", err)
	}

	exporter, err := otlptracegrpc.New(ctx, otlptracegrpc.WithGRPCConn(conn))
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("failed to create OTLP exporter: %w", err)
	}

	res := resource.NewWithAttributes(
		semconv.SchemaURL,
		semconv.ServiceName(cfg.ServiceName),
		semconv.ServiceVersion(cfg.ServiceVersion),
		semconv.DeploymentEnvironment("development"),
	)

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter,
			sdktrace.WithBatchTimeout(5*time.Second),
			sdktrace.WithMaxExportBatchSize(512),
		),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
	)

	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: cfg.LogLevel,
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if a.Key == slog.SourceKey {
				return slog.Attr{}
			}
			return a
		},
	}))

	var metricsHandler http.Handler
	if cfg.EnablePrometheus {
		metricsHandler = promhttp.Handler()
	}

	t := &Telemetry{
		ServiceName:    cfg.ServiceName,
		TracerProvider: tp,
		Logger:         logger,
		MetricsHandler: metricsHandler,
		Shutdown: func(ctx context.Context) error {
			conn.Close()
			return tp.Shutdown(ctx)
		},
	}

	logger.Info("telemetry initialized",
		"service", cfg.ServiceName,
		"version", cfg.ServiceVersion,
		"otlp_endpoint", otlpEndpoint,
	)

	return t, nil
}

func (t *Telemetry) RecordHTTPMetrics(method, path string, status int, duration time.Duration) {
	httpRequestsTotal.WithLabelValues(t.ServiceName, method, path, fmt.Sprintf("%d", status)).Inc()
	httpRequestDuration.WithLabelValues(t.ServiceName, method, path).Observe(duration.Seconds())
}

func (t *Telemetry) IncRequestsInFlight() {
	httpRequestsInFlight.WithLabelValues(t.ServiceName).Inc()
}

func (t *Telemetry) DecRequestsInFlight() {
	httpRequestsInFlight.WithLabelValues(t.ServiceName).Dec()
}
