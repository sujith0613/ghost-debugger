#!/bin/bash
# scripts/inject_failure.sh
#
# Inject failure scenarios into test services.
#
# Usage:
#   ./scripts/inject_failure.sh cascade
#   ./scripts/inject_failure.sh resource_exhaustion
#   ./scripts/inject_failure.sh traffic_spike
#   ./scripts/inject_failure.sh reset

INJECTOR_URL="http://localhost:8099"

case "$1" in
  cascade)
    echo "Injecting CASCADE FAILURE scenario..."
    echo "  service_a → service_b → service_c call chain"
    curl -s -X POST "$INJECTOR_URL/inject" \
      -H "Content-Type: application/json" \
      -d '{"service": "service_b", "type": "latency", "value_ms": 2000, "duration_seconds": 120}'
    echo ""
    echo "Cascade failure active for 120 seconds."
    ;;

  resource_exhaustion)
    echo "Injecting RESOURCE EXHAUSTION scenario..."
    curl -s -X POST "$INJECTOR_URL/inject" \
      -H "Content-Type: application/json" \
      -d '{"service": "service_b", "type": "db_connection_exhaustion", "duration_seconds": 120}'
    echo ""
    ;;

  traffic_spike)
    echo "Injecting TRAFFIC SPIKE scenario..."
    curl -s -X POST "$INJECTOR_URL/inject" \
      -H "Content-Type: application/json" \
      -d '{"service": "service_a", "type": "traffic_spike", "multiplier": 10, "duration_seconds": 120}'
    echo ""
    ;;

  reset)
    echo "Resetting all failure injections..."
    curl -s -X POST "$INJECTOR_URL/reset"
    echo ""
    ;;

  *)
    echo "Usage: $0 [cascade|resource_exhaustion|traffic_spike|reset]"
    exit 1
    ;;
esac
