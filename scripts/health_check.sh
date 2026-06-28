#!/bin/bash
# scripts/health_check.sh
#
# Verify all Ghost Debugger services are running and reachable.

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

check_http() {
  local name=$1
  local url=$2
  local expected_status=${3:-200}

  status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || echo "000")

  if [ "$status" = "$expected_status" ]; then
    echo -e "${GREEN}✓${NC} $name ($url) — HTTP $status"
    return 0
  else
    echo -e "${RED}✗${NC} $name ($url) — HTTP $status (expected $expected_status)"
    return 1
  fi
}

check_grpc() {
  local name=$1
  local host=$2
  local port=$3

  if nc -z -w 3 "$host" "$port" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} $name ($host:$port) — TCP reachable"
    return 0
  else
    echo -e "${RED}✗${NC} $name ($host:$port) — TCP unreachable"
    return 1
  fi
}

failures=0

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Ghost Debugger — Health Check"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

echo "── Infrastructure ──────────────────────"
check_http "Jaeger UI"          "http://localhost:16686"              || ((failures++))
check_http "Prometheus"         "http://localhost:9090/-/healthy"     || ((failures++))
check_http "Grafana"            "http://localhost:3001/api/health"    || ((failures++))
check_http "ChromaDB"           "http://localhost:8000/api/v1/heartbeat" || ((failures++))

echo ""
echo "── Gateway ─────────────────────────────"
check_http "Gateway HTTP"       "http://localhost:8080/health"        || ((failures++))
check_grpc "Gateway gRPC"       "localhost" "50051"                   || ((failures++))

echo ""
echo "── Test Services ───────────────────────"
check_http "service_a"          "http://localhost:8081/health"        || ((failures++))
check_http "service_b"          "http://localhost:8082/health"        || ((failures++))
check_http "service_c"          "http://localhost:8083/health"        || ((failures++))
check_http "failure_injector"   "http://localhost:8084/health"        || ((failures++))

echo ""
echo "── Agent Service ───────────────────────"
check_http "Agent HTTP"         "http://localhost:8090/health"        || ((failures++))
check_grpc "Agent gRPC"         "localhost" "50052"                   || ((failures++))

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ $failures -eq 0 ]; then
  echo -e "${GREEN}All services healthy. Ghost Debugger is ready.${NC}"
else
  echo -e "${RED}$failures service(s) failed health check.${NC}"
  exit 1
fi
