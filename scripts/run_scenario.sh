#!/bin/bash
# scripts/run_scenario.sh
# Run a complete failure scenario against live instrumented services.
# Usage: ./scripts/run_scenario.sh [cascade|resource|traffic|all]

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

SCENARIO="${1:-cascade}"
TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
OUTPUT_DIR="docs/postmortem-examples"
mkdir -p "$OUTPUT_DIR"

AGENT_URL="http://localhost:8090"
INJECTOR_URL="http://localhost:8099"
SERVICE_A="http://localhost:8081"

log()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"; }
ok()   { echo -e "${GREEN}  ✓${NC} $1"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $1"; }
fail() { echo -e "${RED}  ✗${NC} $1"; }

generate_traffic() {
  local count=$1 interval=${2:-0.3} label=${3:-"baseline"}
  log "Generating $count requests ($label)"
  local success=0 errors=0
  for i in $(seq 1 "$count"); do
    status=$(curl -s -o /dev/null -w "%{http_code}" \
      --max-time 15 "$SERVICE_A/api/process" 2>/dev/null || echo "000")
    if [ "$status" = "200" ]; then ((success++)); else ((errors++)); fi
    sleep "$interval"
  done
  ok "Traffic complete: $success OK, $errors errors"
}

wait_for_analysis() {
  local incident_id=$1 max_wait=${2:-180}
  log "Waiting for analysis to complete (max ${max_wait}s)..."
  local elapsed=0
  while [ $elapsed -lt $max_wait ]; do
    local status
    status=$(curl -s "$AGENT_URL/incidents/$incident_id" 2>/dev/null | \
      python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','unknown'))" 2>/dev/null || echo "error")
    case "$status" in
      complete) ok "Analysis complete after ${elapsed}s"; return 0 ;;
      failed) fail "Analysis failed after ${elapsed}s"; return 1 ;;
      *) ;;
    esac
    sleep 5; ((elapsed+=5))
  done
  fail "Analysis timed out after ${max_wait}s"
  return 1
}

save_results() {
  local incident_id=$1 scenario_name=$2
  log "Saving results for $incident_id"
  curl -s "$AGENT_URL/incidents/$incident_id" > "$OUTPUT_DIR/${scenario_name}_state.json"
  python3 -c "
import json
with open('$OUTPUT_DIR/${scenario_name}_state.json') as f:
    d = json.load(f)
report = d.get('postmortem_report', '')
if report:
    with open('$OUTPUT_DIR/${scenario_name}_postmortem.md', 'w') as f:
        f.write(report)
    print(f'Postmortem saved: {len(report)} chars')
print(f'Severity:    {d.get(\"triage_severity\", \"UNKNOWN\")}')
print(f'Root Cause:  {d.get(\"root_cause\", \"\")[:100]}')
print(f'Confidence:  {d.get(\"root_cause_confidence\", 0):.0%}')
print(f'Completed:   {list(set(d.get(\"completed_agents\", [])))}')
print(f'Failed:      {list(set(d.get(\"failed_agents\", [])))}')
"
  ok "Results saved to $OUTPUT_DIR/"
}

inject_failure() {
  local service=$1 type=$2 duration=$3
  shift 3
  local extra_args="$@"
  curl -s -X POST "$INJECTOR_URL/inject" \
    -H "Content-Type: application/json" \
    -d "{\"service\": \"$service\", \"type\": \"$type\", \"duration_seconds\": $duration $extra_args}" > /dev/null
  ok "Failure injected: $service -> $type (${duration}s)"
}

reset_failures() {
  curl -s -X POST "$INJECTOR_URL/reset" > /dev/null
  ok "All failures reset"
}

trigger_analysis() {
  local description=$1 services=$2 trigger_type=${3:-"error_rate"}
  curl -s -X POST "$AGENT_URL/analyze" \
    -H "Content-Type: application/json" \
    -d "{
      \"trigger_type\": \"$trigger_type\",
      \"trigger_description\": \"$description\",
      \"affected_services\": $services,
      \"analysis_window_seconds\": 600
    }" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('incident_id','ERROR'))"
}

run_cascade() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  SCENARIO 1: CASCADE FAILURE"
  echo "  service_b latency spike -> service_a timeout -> cascade"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  log "Baseline traffic"; generate_traffic 20 0.5 "baseline"
  reset_failures
  log "Injecting 2500ms latency into service_b"
  inject_failure "service_b" "latency" 180 ", \"duration_ms\": 2500"
  log "Traffic during failure"; generate_traffic 30 1.0 "during-failure"
  local incident_id
  incident_id=$(trigger_analysis "error_rate and latency spike detected - service_b p99 latency 2500ms causing service_a timeouts" '["service_a", "service_b"]' "latency_p99")
  ok "Incident: $incident_id"
  wait_for_analysis "$incident_id" 180; local exit_code=$?
  save_results "$incident_id" "scenario1_cascade_${TIMESTAMP}"
  reset_failures
  return $exit_code
}

run_resource() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  SCENARIO 2: RESOURCE EXHAUSTION"
  echo "  service_b error rate spike - simulated DB pool exhaustion"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  log "Baseline traffic"; generate_traffic 20 0.5 "baseline"
  reset_failures
  log "Injecting 40% error rate into service_b"
  inject_failure "service_b" "error_rate" 180 ", \"error_rate\": 0.40"
  log "Traffic during failure"; generate_traffic 40 0.8 "during-failure"
  local incident_id
  incident_id=$(trigger_analysis "error_rate 40.2% exceeded 5% threshold on service_b - multiple ResourceExhausted errors in logs" '["service_a", "service_b"]' "error_rate")
  ok "Incident: $incident_id"
  wait_for_analysis "$incident_id" 180; local exit_code=$?
  save_results "$incident_id" "scenario2_resource_${TIMESTAMP}"
  reset_failures
  return $exit_code
}

run_traffic() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  SCENARIO 3: TRAFFIC SPIKE"
  echo "  10x normal traffic -> downstream services degrade"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  log "Baseline traffic"; generate_traffic 15 0.5 "baseline"
  reset_failures
  log "Sending 100 rapid requests to service_a"
  local success=0 errors=0
  for i in $(seq 1 100); do
    status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$SERVICE_A/api/process" 2>/dev/null || echo "000")
    if [ "$status" = "200" ]; then ((success++)); else ((errors++)); fi
  done
  ok "Spike sent: $success OK, $errors errors"
  log "Elevated traffic"; generate_traffic 60 1.0 "elevated"
  local incident_id
  incident_id=$(trigger_analysis "request_rate spike detected - service_a receiving 10x normal traffic, downstream error rate elevated" '["service_a", "service_b", "service_c"]' "error_rate")
  ok "Incident: $incident_id"
  wait_for_analysis "$incident_id" 180; local exit_code=$?
  save_results "$incident_id" "scenario3_traffic_${TIMESTAMP}"
  return $exit_code
}

run_all() {
  run_cascade  || warn "Cascade scenario had issues"; sleep 10
  run_resource || warn "Resource scenario had issues"; sleep 10
  run_traffic  || warn "Traffic scenario had issues"
  echo ""; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  ALL SCENARIOS COMPLETE. Results in $OUTPUT_DIR/"
  ls -la "$OUTPUT_DIR/"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

case "$SCENARIO" in
  cascade)  run_cascade  ;;
  resource) run_resource ;;
  traffic)  run_traffic  ;;
  all)      run_all      ;;
  *)
    echo "Usage: $0 [cascade|resource|traffic|all]"
    echo "Scenarios:"
    echo "  cascade  - service_b latency spike causes service_a cascade failure"
    echo "  resource - service_b high error rate (simulates DB pool exhaustion)"
    echo "  traffic  - 10x traffic spike causes downstream degradation"
    echo "  all      - run all three scenarios in sequence"
    exit 1
    ;;
esac
