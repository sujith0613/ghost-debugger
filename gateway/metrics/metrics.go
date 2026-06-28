package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	IngestionTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ghost_debugger_gateway_ingestion_total",
			Help: "Total telemetry ingestion RPC calls. " +
				"Alert on rate_limited > 100/min (flooding) or error > 10/min (backend failure).",
		},
		[]string{"service", "signal_type", "status"},
	)

	IngestionDurationSeconds = promauto.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "ghost_debugger_gateway_ingestion_duration_seconds",
			Help:    "Telemetry ingestion RPC duration. Alert on p99 > 10ms.",
			Buckets: []float64{0.0001, 0.0005, 0.001, 0.005, 0.010, 0.050, 0.100},
		},
		[]string{"service", "signal_type"},
	)

	RateLimitedTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ghost_debugger_gateway_rate_limited_total",
			Help: "Telemetry requests rejected by rate limiter. " +
				"Alert if any service > 500/minute (telemetry bug).",
		},
		[]string{"service", "signal_type"},
	)

	WorkerPoolQueueDepth = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "ghost_debugger_gateway_worker_pool_queue_depth",
			Help: "Current storage worker pool queue depth. " +
				"Alert on > 5000 (workers falling behind).",
		},
	)

	WorkerPoolDropped = promauto.NewCounter(
		prometheus.CounterOpts{
			Name: "ghost_debugger_gateway_worker_pool_dropped_total",
			Help: "Storage jobs dropped due to full worker queue. " +
				"Any non-zero value = telemetry loss. Investigate immediately.",
		},
	)
)

var (
	CircuitBreakerState = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "ghost_debugger_gateway_circuit_breaker_state",
			Help: "Circuit breaker state: 0=CLOSED, 1=OPEN, 2=HALF-OPEN. " +
				"Alert if OPEN for > 60 seconds (agent service down).",
		},
	)

	CircuitBreakerTransitionsTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ghost_debugger_gateway_circuit_breaker_transitions_total",
			Help: "Circuit breaker state transitions. " +
				"Frequent CLOSED->OPEN transitions indicate agent instability.",
		},
		[]string{"from_state", "to_state"},
	)
)

var (
	IncidentDetectedTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ghost_debugger_gateway_incident_detected_total",
			Help: "Incidents detected by the gateway incident detector. " +
				"High rate may indicate recurring underlying issue.",
		},
		[]string{"trigger_type"},
	)

	ActiveIncidents = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "ghost_debugger_gateway_active_incidents",
			Help: "Number of incidents currently being analyzed by the agent pipeline. " +
				"Alert on > 10 (agent service overwhelmed).",
		},
	)

	IncidentQueueDepth = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "ghost_debugger_gateway_incident_queue_depth",
			Help: "Incidents waiting for agent analysis (circuit open or agents busy).",
		},
	)
)

var (
	AgentInvocationTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ghost_debugger_gateway_agent_invocation_total",
			Help: "Agent service invocations from the gateway. " +
				"Alert on circuit_open > 10/min or timeout > 5/min.",
		},
		[]string{"status"},
	)

	AgentInvocationDurationSeconds = promauto.NewHistogram(
		prometheus.HistogramOpts{
			Name:    "ghost_debugger_gateway_agent_invocation_duration_seconds",
			Help:    "Full incident analysis pipeline duration (incident detected -> postmortem ready). " +
				"Alert on p99 > 120 seconds.",
			Buckets: []float64{10, 20, 30, 45, 60, 90, 120, 180, 300},
		},
	)
)

var (
	ActiveServices = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "ghost_debugger_gateway_active_services",
			Help: "Number of distinct services currently sending telemetry. " +
				"Sudden drop may indicate a service has crashed.",
		},
	)

	ServiceLastSeenTimestamp = promauto.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ghost_debugger_gateway_service_last_seen_timestamp",
			Help: "Unix timestamp when each service last sent telemetry. " +
				"Alert if now() - value > 60s for any critical service.",
		},
		[]string{"service"},
	)
)
