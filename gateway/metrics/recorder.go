package metrics

import (
	"time"
)

func RecordIngestion(service, signalType, status string, duration time.Duration) {
	IngestionTotal.WithLabelValues(service, signalType, status).Inc()
	IngestionDurationSeconds.WithLabelValues(service, signalType).
		Observe(duration.Seconds())
	ServiceLastSeenTimestamp.WithLabelValues(service).SetToCurrentTime()
}

func RecordRateLimit(service, signalType string) {
	RateLimitedTotal.WithLabelValues(service, signalType).Inc()
}

func RecordWorkerJob(queued bool) {
	if queued {
		WorkerPoolQueueDepth.Inc()
	} else {
		WorkerPoolDropped.Inc()
	}
}

func RecordWorkerJobComplete() {
	WorkerPoolQueueDepth.Dec()
}

func RecordCircuitBreakerTransition(fromState, toState string, stateValue float64) {
	CircuitBreakerState.Set(stateValue)
	CircuitBreakerTransitionsTotal.WithLabelValues(fromState, toState).Inc()
}

func RecordIncidentDetected(triggerType string) {
	IncidentDetectedTotal.WithLabelValues(triggerType).Inc()
	IncidentQueueDepth.Inc()
}

func RecordIncidentDispatched() {
	IncidentQueueDepth.Dec()
	ActiveIncidents.Inc()
}

func RecordIncidentComplete(success bool, duration time.Duration) {
	ActiveIncidents.Dec()
	status := "success"
	if !success {
		status = "failed"
	}
	AgentInvocationTotal.WithLabelValues(status).Inc()
	AgentInvocationDurationSeconds.Observe(duration.Seconds())
}

func RecordCircuitBreakerBlock() {
	AgentInvocationTotal.WithLabelValues("circuit_open").Inc()
}

func UpdateActiveServices(count int) {
	ActiveServices.Set(float64(count))
}
